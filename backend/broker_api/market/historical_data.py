import logging
from datetime import datetime, timedelta, date
from kiteconnect import KiteConnect
from psycopg2.extras import execute_values

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

import pytz

def get_historical_data(kite: KiteConnect, instrument_token: int, from_date: date, to_date: date, interval: str):
    """
    Fetches historical data for a given instrument token and date range.
    """
    import pandas as pd

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

            # If the timestamps are naive, localize them to IST ('Asia/Kolkata').
            if dates.dt.tz is None:
                dates = dates.dt.tz_localize('Asia/Kolkata')
            else:
                # If they are already timezone-aware, ensure they are in IST.
                dates = dates.dt.tz_convert('Asia/Kolkata')
            
            df['date'] = dates
            
        return df
    except Exception as e:
        logging.error(f"Error fetching historical data for token {instrument_token}: {e}")
        return pd.DataFrame()

def fetch_and_store_historical_data(kite: KiteConnect, conn, instrument_token: int, tradingsymbol: str, from_date: date, to_date: date, interval: str, table_name: str = 'kite_historical_data'):
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
                f"""
                INSERT INTO {table_name} (instrument_token, tradingsymbol, "timestamp", interval, open, high, low, close, volume, oi)
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


def fetch_and_store_indices_historical_data(
    kite: KiteConnect,
    conn,
    instrument_token: int,
    tradingsymbol: str,
    from_date: date,
    to_date: date,
    interval: str,
    table_name: str = 'kite_indices_historical_data'
) -> int:
    """
    Dedicated indices historical data fetch-and-insert to avoid interfering with stock flow.
    - Adds stronger sanitization for index-specific nulls.
    - Ensures transaction rollback on error to prevent 'current transaction is aborted' cascades.
    """
    logging.info(f"[IMPORTANT] Starting indices historical fetch for {tradingsymbol} ({instrument_token}) "
                 f"from {from_date} to {to_date} interval={interval} into table={table_name}")

    df = get_historical_data(kite, instrument_token, from_date, to_date, interval)
    if df.empty:
        logging.info(f"No historical data returned from API for index {tradingsymbol} ({instrument_token}) "
                     f"from {from_date} to {to_date}.")
        return 0

    logging.info(f"Retrieved {len(df)} index records from API for {tradingsymbol}")

    # Data validation
    required_columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        logging.error(f"Missing required columns in API response for index {tradingsymbol}: {missing_columns}")
        return 0

    # Log sample of data for debugging
    if len(df) > 0:
        sample_row = df.iloc[0]
        logging.info(f"Sample row for index {tradingsymbol}: date={sample_row['date']}, "
                     f"open={sample_row['open']}, close={sample_row['close']}")

    # Prepare data for bulk insert with index-specific sanitization
    records_to_insert = []
    for _, row in df.iterrows():
        try:
            # Volume can be 0 or missing for indices; coerce missing/NaN to 0
            vol_val = row.get('volume', 0)
            try:
                # pandas.isna handles None/NaN
                vol_val = 0 if pd.isna(vol_val) else int(vol_val)
            except Exception:
                vol_val = 0

            # OI often not applicable for spot indices; allow NULL
            oi_raw = row.get('oi', None)
            oi_val = None
            if oi_raw is not None and not pd.isna(oi_raw):
                try:
                    oi_val = int(oi_raw)
                except Exception:
                    oi_val = None

            record = (
                instrument_token,
                tradingsymbol,
                row['date'],
                interval,
                float(row['open']),
                float(row['high']),
                float(row['low']),
                float(row['close']),
                vol_val,
                oi_val
            )
            records_to_insert.append(record)
        except (ValueError, TypeError) as e:
            logging.error(f"Error preparing index record for {tradingsymbol} at {row.get('date', 'unknown date')}: {e}")
            continue

    if not records_to_insert:
        logging.error(f"No valid index records to insert for {tradingsymbol}")
        return 0

    logging.info(f"Prepared {len(records_to_insert)} valid index records for insertion")

    with conn.cursor() as cur:
        try:
            logging.debug(f"Executing indices bulk insert for {tradingsymbol} with {len(records_to_insert)} records")
            execute_values(
                cur,
                f"""
                INSERT INTO {table_name} (instrument_token, tradingsymbol, "timestamp", interval, open, high, low, close, volume, oi)
                VALUES %s
                ON CONFLICT (instrument_token, "timestamp", interval) DO NOTHING;
                """,
                records_to_insert
            )
            inserted_rows = cur.rowcount
            logging.info(f"Successfully processed {len(records_to_insert)} index records for {tradingsymbol} "
                         f"({instrument_token}), inserted {inserted_rows} new ones.")
            if inserted_rows == 0:
                logging.warning(f"All {len(records_to_insert)} index records for {tradingsymbol} already exist "
                                f"in database (conflicts)")
            return inserted_rows
        except Exception as e:
            logging.error(f"Error during indices bulk insert for {tradingsymbol} ({instrument_token}): {e}")
            logging.error(f"Sample index record that failed: {records_to_insert[0] if records_to_insert else 'None'}")
            # CRITICAL: reset aborted transaction so caller can continue with same connection
            try:
                conn.rollback()
                logging.info("Rolled back transaction after indices insert error to clear aborted state.")
            except Exception as rb_err:
                logging.error(f"Rollback failed after indices insert error: {rb_err}")
            return 0
