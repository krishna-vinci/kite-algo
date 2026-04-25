from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
from database import get_db_connection
import logging
from kiteconnect import KiteConnect
from sqlalchemy import text
import redis
import json
import os

# Manual mapping for known mismatches (Frontend Name -> DB Tradingsymbol/Name)
NAME_MAPPING = {
    "NIFTY SMALLCAP 250": "NIFTY SMLCAP 250",
    "NIFTY MICROCAP 250": "NIFTY MICROCAP250",
    # Add others if needed
}

def get_sync_redis():
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.from_url(redis_url, decode_responses=True)

def get_instrument_token(db, index_name: str) -> Optional[int]:
    """
    Resolve instrument token from index name or tradingsymbol.
    """
    # 1. Check manual mapping
    search_name = NAME_MAPPING.get(index_name, index_name)

    try:
        with db.cursor() as cur:
            query = """
            SELECT instrument_token, tradingsymbol
            FROM kite_indices
            WHERE tradingsymbol = %s OR name = %s
            LIMIT 1
            """
            cur.execute(query, (search_name, search_name))
            row = cur.fetchone()
            if row:
                return int(row[0])
    except Exception as e:
        logging.error(f"Error resolving token for {index_name}: {e}")
    
    return None

def calculate_performance(indices: List[str], kite: KiteConnect) -> Dict:
    """
    Calculate performance metrics for a list of indices using Redis Cache, Live LTP, and Kite Historical API.
    """
    performance_data = {}
    periods = {
        "1D": 1,
        "1W": 7,
        "1M": 30,
        "3M": 90,
        "6M": 180,
        "1Y": 365
    }

    # 1. Check Redis Cache
    try:
        r = get_sync_redis()
        # Keys: 'perf:INDEX_NAME'
        keys = [f"perf:{index}" for index in indices]
        cached_values = r.mget(keys)
        
        missing_indices = []
        
        for i, val in enumerate(cached_values):
            index_name = indices[i]
            if val:
                try:
                    performance_data[index_name] = json.loads(val)
                except json.JSONDecodeError:
                    missing_indices.append(index_name)
            else:
                missing_indices.append(index_name)
    except Exception as e:
        logging.error(f"Redis connection error: {e}")
        missing_indices = indices[:] # Fallback to full fetch

    if not missing_indices:
        return performance_data

    # 2. Fetch Data for Missing Indices
    index_tokens = {}
    
    with get_db_connection() as db:
        # Resolve tokens for missing indices
        for index in missing_indices:
            token = get_instrument_token(db, index)
            if token:
                index_tokens[index] = token
            else:
                logging.warning(f"Could not resolve instrument token for {index}")
                # Cache empty/error state to prevent repeated failures? Maybe short TTL.
                data = {period: "Data not available" for period in periods}
                data["sparkline"] = []
                performance_data[index] = data
                try:
                     r.setex(f"perf:{index}", 60, json.dumps(data))
                except: pass

        # Fetch Live Quotes
        live_data = {} 
        token_to_kite_symbol = {}
        
        if index_tokens:
            try:
                tokens_list = list(index_tokens.values())
                if tokens_list:
                    with db.cursor() as cur:
                        query_symbols = f"""
                        SELECT instrument_token, tradingsymbol, exchange 
                        FROM kite_indices 
                        WHERE instrument_token IN %s
                        """
                        token_tuple = tuple(tokens_list)
                        cur.execute(query_symbols, (token_tuple,))
                        rows = cur.fetchall()
                        
                        kite_symbols = []
                        for row in rows:
                            token, sym, exch = row
                            exch = exch or 'NSE'
                            kite_sym = f"{exch}:{sym}"
                            kite_symbols.append(kite_sym)
                            token_to_kite_symbol[token] = kite_sym
                    
                    if kite_symbols:
                        quote_response = kite.quote(kite_symbols)
                        for token, kite_sym in token_to_kite_symbol.items():
                            if kite_sym in quote_response:
                                q = quote_response[kite_sym]
                                live_data[token] = {
                                    'last_price': q.get('last_price'),
                                    'ohlc_close': q.get('ohlc', {}).get('close')
                                }
            except Exception as e:
                logging.error(f"Error fetching live Quote: {e}")

        # Calculate Logic
        for index in missing_indices:
            if index not in index_tokens:
                continue
                
            token = index_tokens[index]
            calc_data = {}
            
            try:
                # Current Status
                token_data = live_data.get(token, {})
                ltp = token_data.get('last_price')
                prev_close_live = token_data.get('ohlc_close')

                # Fetch History (1 Year)
                to_date = datetime.now()
                from_date = to_date - timedelta(days=366) 
                
                historical_candles = []
                try:
                    historical_candles = kite.historical_data(
                        token, from_date, to_date, "day", continuous=False, oi=False
                    )
                except Exception as h_err:
                     logging.error(f"Kite historical API error for {index}: {h_err}")
                
                # Fallback LTP
                if ltp is None and historical_candles:
                    ltp = historical_candles[-1]['close']
                
                # 1D Change
                if ltp is not None and prev_close_live and prev_close_live != 0:
                     one_day_return = ((ltp - prev_close_live) / prev_close_live) * 100
                     calc_data["1D"] = f"{one_day_return:.2f}%"
                elif ltp is not None and historical_candles and len(historical_candles) > 1:
                     prev_close_hist = historical_candles[-2]['close']
                     if prev_close_hist != 0:
                        one_day_return = ((ltp - prev_close_hist) / prev_close_hist) * 100
                        calc_data["1D"] = f"{one_day_return:.2f}%"
                     else:
                        calc_data["1D"] = "N/A"
                else:
                     calc_data["1D"] = "N/A"

                if historical_candles:
                    # Sparklines
                    sparkline_data = historical_candles[-30:]
                    calc_data["sparkline"] = [c['close'] for c in sparkline_data]
                    calc_data["volume_sparkline"] = [
                        {'vol': c.get('volume', 0), 'close': c['close'], 'open': c['open']} 
                        for c in sparkline_data
                    ]

                    # Calculate returns for periods
                    hist_map = {c['date'].date(): c['close'] for c in historical_candles}
                    dates = sorted(list(hist_map.keys()))
                    
                    for period_name, days in periods.items():
                        if period_name == "1D": continue 

                        target_date = (to_date - timedelta(days=days)).date()
                        
                        closest_date = None
                        for d in reversed(dates):
                            if d <= target_date:
                                closest_date = d
                                break
                        
                        if closest_date:
                             ref_close = hist_map[closest_date]
                             if ref_close != 0:
                                 p_ret = ((ltp - ref_close) / ref_close) * 100
                                 calc_data[period_name] = f"{p_ret:.2f}%"
                             else:
                                 calc_data[period_name] = "N/A"
                        else:
                             calc_data[period_name] = "N/A"
                             
                else:
                    # No historical data
                    for p in periods:
                        if p != "1D": calc_data[p] = "N/A"
                    calc_data["sparkline"] = []
                    calc_data["volume_sparkline"] = []

                # Add to main dict
                performance_data[index] = calc_data
                
                # Cache result
                # Check if market is open to determine TTL?
                # Market Open (approx 9:15-15:30 IST)
                # For simplicity, use 60s TTL always. It ensures freshness.
                try:
                    r.setex(f"perf:{index}", 60, json.dumps(calc_data))
                except Exception as re:
                    logging.error(f"Redis set error: {re}")

            except Exception as e:
                logging.error(f"Error calculating performance for {index}: {e}", exc_info=True)
                calc_data = {period: "Error" for period in periods}
                performance_data[index] = calc_data
                continue

    return performance_data
