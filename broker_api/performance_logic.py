from datetime import datetime, timedelta
from typing import Dict, List
import pandas as pd
from database import get_db_connection
import logging

def calculate_performance(indices: List[str]) -> Dict:
    """
    Calculate performance metrics for a list of indices.
    """
    performance_data = {}
    end_date = datetime.now().date()
    
    periods = {
        "1D": 1,
        "1W": 7,
        "1M": 30,
        "3M": 90,
        "6M": 180,
        "1Y": 365
    }
    
    with get_db_connection() as db:
        for index in indices:
            try:
                # Fetch latest close price (LTP)
                query_ltp = f"""
                SELECT close
                FROM kite_indices_historical_data
                WHERE tradingsymbol = '{index}'
                ORDER BY timestamp DESC
                LIMIT 1
                """
                ltp_df = pd.read_sql(query_ltp, db)
                ltp = ltp_df['close'].iloc[0] if not ltp_df.empty and not pd.isna(ltp_df['close'].iloc[0]) else None

                if ltp is None:
                    logging.warning(f"Skipping {index} due to missing LTP.")
                    performance_data[index] = {period: "Data not available" for period in periods}
                    continue

                performance_data[index] = {}
                
                # Calculate returns for different periods
                for period_name, days in periods.items():
                    start_date = end_date - timedelta(days=days)
                    
                    query_period_close = f"""
                    SELECT close
                    FROM kite_indices_historical_data
                    WHERE tradingsymbol = '{index}' AND timestamp::date <= '{start_date}'
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """
                    
                    period_close_df = pd.read_sql(query_period_close, db)
                    
                    if not period_close_df.empty and not pd.isna(period_close_df['close'].iloc[0]):
                        period_close = period_close_df['close'].iloc[0]
                        if period_close != 0:
                            period_return = ((ltp - period_close) / period_close) * 100
                            performance_data[index][period_name] = f"{period_return:.2f}%"
                        else:
                            performance_data[index][period_name] = "N/A"
                    else:
                        performance_data[index][period_name] = "N/A"
            except Exception as e:
                logging.error(f"Error calculating performance for {index}: {e}", exc_info=True)
                performance_data[index] = {period: "Error" for period in periods}
                continue

    return performance_data