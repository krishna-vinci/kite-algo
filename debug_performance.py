import logging
import sys
from datetime import datetime, timedelta
from broker_api.kite_auth import login_headless
from database import get_db_connection
from broker_api.performance_logic import calculate_performance, get_instrument_token

# Configure logging to stdout
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

def debug_nifty():
    print("Starting Debug...")
    try:
        kite, access_token = login_headless()
        print("Kite Connected via Headless Login.")
    except Exception as e:
        print(f"Kite Connection Failed: {e}")
        return

    indices = ["NIFTY 50"]
    
    # 1. Resolve Token
    with get_db_connection() as db:
        token = get_instrument_token(db, indices[0])
        print(f"Token for NIFTY 50: {token}")
    
    if not token:
        print("Token not found")
        return

    # 2. Fetch Live Quote (Test)
    print("Fetching Live Quote for NSE:NIFTY 50...")
    try:
        quote = kite.quote(["NSE:NIFTY 50"])
        print(f"Quote Result: {quote}")
    except Exception as e:
        print(f"Quote Failed: {e}")

    # 3. Fetch History Manually
    to_date = datetime.now()
    from_date = to_date - timedelta(days=366)
    print(f"Fetching from {from_date} to {to_date}")
    
    try:
        history = kite.historical_data(token, from_date, to_date, "day", continuous=False, oi=False)
        print(f"History records: {len(history)}")
        if history:
            print(f"First record: {history[0]}")
            print(f"Last record: {history[-1]}")
            
            # Check Sparkline Data
            sparkline_data = history[-30:]
            print(f"Sparkline Len: {len(sparkline_data)}")
            if sparkline_data:
                print(f"Sparkline First Vol: {sparkline_data[0].get('volume')}")
                print(f"Sparkline Data (Close): {[x['close'] for x in sparkline_data]}")
            
            # Check Returns Logic
            hist_map = {c['date'].date(): c['close'] for c in history}
            dates = sorted(list(hist_map.keys()))
            print(f"Available Dates: {len(dates)}")
            if dates:
                print(f"Date Range: {dates[0]} to {dates[-1]}")
            
            target_1w = (to_date - timedelta(days=7)).date()
            print(f"Target 1W: {target_1w}")
            
            closest = None
            for d in reversed(dates):
                if d <= target_1w:
                    closest = d
                    break
            print(f"Closest to 1W: {closest}")
            
            if closest:
                print(f"Close at {closest}: {hist_map[closest]}")
            
    except Exception as e:
        print(f"API Error: {e}")

    print("\n--- Testing calculate_performance function ---")
    try:
        # Test actual function
        result = calculate_performance(indices, kite=kite)
        print(f"Result for NIFTY 50: {result.get('NIFTY 50')}")
    except Exception as e:
        print(f"calculate_performance Failed: {e}")

if __name__ == "__main__":
    debug_nifty()
