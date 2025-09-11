import logging
from datetime import datetime, timedelta, date
from kiteconnect import KiteConnect
import pandas as pd
from psycopg2.extras import execute_values

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

import pytz

def get_historical_data(kite: KiteConnect, instrument_token: int, from_date: date, to_date: date, interval: str):
    """
    Fetches historical data for a given instrument token and date range.
    """
    try:
        # Convert date objects to naive datetime objects.
        # The Kite API treats naive datetimes as UTC.
        from_datetime = datetime.combine(from_date, datetime.min.time())
        to_datetime = datetime.combine(to_date, datetime.max.time())

        records = kite.historical_data(instrument_token, from_datetime, to_datetime, interval)
        df = pd.DataFrame(records)

        # Convert timezone-naive datetime to timezone-aware (IST)
        if not df.empty:
            # Convert the 'date' column to datetime objects first.
            dates = pd.to_datetime(df['date'])

            # Robustly handle timezone conversion. The Kite API can return both naive (UTC) and aware timestamps.
            # This logic checks if the timestamps are naive and only localizes them if they are.
            if dates.dt.tz is None:
                # If naive, we assume UTC as per the API's standard behavior.
                dates = dates.dt.tz_localize('UTC')
            
            # Now that all timestamps are reliably timezone-aware, we can safely convert them to the desired local timezone.
            df['date'] = dates.dt.tz_convert('Asia/Calcutta')

            # Data Normalization: Correct for the Kite API's off-by-one day timestamp quirk for daily data.
            # This is a deliberate correction to align the timestamp with the actual trading day's data.
            if interval == 'day':
                df['date'] = df['date'] + pd.Timedelta(days=1)
            
        return df
    except Exception as e:
        logging.error(f"Error fetching historical data for token {instrument_token}: {e}")
        return pd.DataFrame()

def fetch_and_store_historical_data(kite: KiteConnect, conn, instrument_token: int, tradingsymbol: str, from_date: date, to_date: date, interval: str):
    """
    Fetches historical data and stores it in the database using a bulk insert.
    Returns the number of records newly inserted.
    """
    logging.info(f"Starting data fetch for {tradingsymbol} ({instrument_token}) from {from_date} to {to_date}")
    
    df = get_historical_data(kite, instrument_token, from_date, to_date, interval)
    if df.empty:
        logging.info(f"No historical data returned from API for {tradingsymbol} ({instrument_token}) from {from_date} to {to_date}.")
        return 0

    logging.info(f"Retrieved {len(df)} records from API for {tradingsymbol}")
    
    # Data validation
    required_columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        logging.error(f"Missing required columns in API response for {tradingsymbol}: {missing_columns}")
        return 0

    # Log sample of data for debugging
    if len(df) > 0:
        sample_row = df.iloc[0]
        logging.info(f"Sample row for {tradingsymbol}: date={sample_row['date']}, open={sample_row['open']}, close={sample_row['close']}")

    # Prepare data for bulk insert
    records_to_insert = []
    for _, row in df.iterrows():
        try:
            record = (
                instrument_token,
                tradingsymbol,
                row['date'],
                interval,
                float(row['open']),
                float(row['high']),
                float(row['low']),
                float(row['close']),
                int(row['volume']),
                int(row.get('oi', 0)) if row.get('oi') is not None else None
            )
            records_to_insert.append(record)
        except (ValueError, TypeError) as e:
            logging.error(f"Error preparing record for {tradingsymbol} at {row.get('date', 'unknown date')}: {e}")
            continue

    if not records_to_insert:
        logging.error(f"No valid records to insert for {tradingsymbol}")
        return 0

    logging.info(f"Prepared {len(records_to_insert)} valid records for insertion")

    with conn.cursor() as cur:
        try:
            # Log the SQL statement for debugging
            logging.debug(f"Executing bulk insert for {tradingsymbol} with {len(records_to_insert)} records")
            
            execute_values(
                cur,
                """
                INSERT INTO kite_historical_data (instrument_token, tradingsymbol, "timestamp", interval, open, high, low, close, volume, oi)
                VALUES %s
                ON CONFLICT (instrument_token, "timestamp", interval) DO NOTHING;
                """,
                records_to_insert
            )
            inserted_rows = cur.rowcount
            logging.info(f"Successfully processed {len(records_to_insert)} records for {tradingsymbol} ({instrument_token}), inserted {inserted_rows} new ones.")
            
            if inserted_rows == 0:
                logging.warning(f"All {len(records_to_insert)} records for {tradingsymbol} already exist in database (conflicts)")
            
            return inserted_rows
        except Exception as e:
            logging.error(f"Error during bulk insert for {tradingsymbol} ({instrument_token}): {e}")
            logging.error(f"Sample record that failed: {records_to_insert[0] if records_to_insert else 'None'}")
            # The transaction will be rolled back by the calling function's error handler.
            return 0
