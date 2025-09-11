import psycopg2
from datetime import datetime, timedelta
import pandas as pd


def fetch_data_in_chunks(fyers, symbol, start_date, end_date, chunk_size=60):
    # Database connection setup
    conn = psycopg2.connect("dbname=finance user=krishna password=1122")
    cur = conn.cursor()

    current_start_date = start_date
    while current_start_date < end_date:
        current_end_date = min(current_start_date + timedelta(days=chunk_size), end_date)


        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": current_start_date.strftime('%Y-%m-%d'),
            "range_to": current_end_date.strftime('%Y-%m-%d'),
            "cont_flag": "1"
        }

        response = fyers.history(data=data)
        if response.get('s') == 'ok':
            df = pd.DataFrame(response['candles'], columns=['date', 'open', 'high', 'low', 'close', 'volume'])
            df['date'] = pd.to_datetime(df['date'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')

            for _, row in df.iterrows():
                # Insert data into the historical_stock_data table
                cur.execute("""
                    INSERT INTO historical_stock_data (ticker_id, date, open, high, low, close, volume)
                    SELECT id, %s, %s, %s, %s, %s, %s FROM tickers WHERE symbol = %s
                    ON CONFLICT (ticker_id, date) DO NOTHING;
                    """,
                            (row['date'], row['open'], row['high'], row['low'], row['close'], row['volume'], symbol)
                            )
            conn.commit()

        current_start_date = current_end_date

    cur.close()
    conn.close()





