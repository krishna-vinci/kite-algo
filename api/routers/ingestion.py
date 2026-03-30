import csv
import logging

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from psycopg2 import extras

from database import get_db_connection


router = APIRouter(tags=["Ingestion"])

CSV_FILES = {
    "ind_nifty50list.csv": "Nifty50",
    "ind_niftylargemidcap250list.csv": "NiftyLargeMidcap250",
    "ind_nifty500list.csv": "Nifty500",
}


def process_csv_data(csv_file_path: str, source_list_name: str):
    data = []
    try:
        df = pd.read_csv(csv_file_path)
        for _, row in df.iterrows():
            data.append(
                {
                    "symbol": row["Symbol"],
                    "company_name": row["Company Name"],
                    "sector": row["Industry"],
                    "source_list": source_list_name,
                }
            )
        logging.info("Successfully processed %s entries from %s.", len(data), csv_file_path)
    except FileNotFoundError:
        logging.error("CSV file not found: %s", csv_file_path)
    except KeyError as e:
        logging.error("Missing expected column in %s: %s", csv_file_path, e)
    except Exception as e:
        logging.error("Error processing %s: %s", csv_file_path, e)
    return data


def clean_value(value_str):
    if not value_str:
        return None
    try:
        return float(str(value_str).replace("%", "").replace(",", ""))
    except ValueError:
        logging.warning("Could not convert %r to float.", value_str)
        return None


@router.post("/ingest-stock-data")
async def ingest_stock_data_endpoint():
    logging.info("FastAPI endpoint /api/ingest-stock-data triggered.")

    all_csv_entries = []
    for file_path, source_name in CSV_FILES.items():
        all_csv_entries.extend(process_csv_data(file_path, source_name))

    if not all_csv_entries:
        raise HTTPException(status_code=500, detail="No data processed from CSV files.")

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=extras.DictCursor) as cur:
            cur.execute(
                "SELECT tradingsymbol, instrument_token, instrument_type FROM kite_instruments WHERE instrument_type = 'EQ' AND exchange = 'NSE';"
            )
            kite_instruments_data = {row["tradingsymbol"]: row for row in cur.fetchall()}

        if not kite_instruments_data:
            raise HTTPException(status_code=500, detail="No equity instruments found in kite_instruments table.")

        inserted_count = 0
        unmatched_count = 0

        with conn.cursor() as cur:
            for entry in all_csv_entries:
                symbol = entry["symbol"]
                if symbol in kite_instruments_data:
                    instrument_token = kite_instruments_data[symbol]["instrument_token"]
                    cur.execute(
                        """
                        INSERT INTO kite_ticker_tickers (instrument_token, tradingsymbol, company_name, sector, source_list)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (instrument_token, source_list) DO NOTHING;
                        """,
                        (
                            instrument_token,
                            symbol,
                            entry["company_name"],
                            entry["sector"],
                            entry["source_list"],
                        ),
                    )
                    inserted_count += 1
                else:
                    unmatched_count += 1
                    logging.warning(
                        "Symbol %r from %r not found in kite_instruments (instrument_type='EQ').",
                        symbol,
                        entry["source_list"],
                    )

            conn.commit()

        return JSONResponse(
            content={
                "message": "Data ingestion and synchronization completed successfully.",
                "inserted_records": inserted_count,
                "unmatched_symbols": unmatched_count,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.critical("An unhandled error occurred during ingestion: %s", e)
        raise HTTPException(status_code=500, detail=f"Internal server error during ingestion: {e}")
    finally:
        if conn:
            conn.close()


@router.post("/update-nifty50-data")
async def update_nifty50_data_endpoint():
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        with open("nifty50_data.csv", "r", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                ticker = row.get("Ticker")
                if not ticker or not ticker.strip():
                    continue

                params = {
                    "tradingsymbol": ticker.strip(),
                    "change_1d": clean_value(row.get("1D change")),
                    "return_attribution": clean_value(row.get("Return attribution")),
                    "index_weight": clean_value(row.get("Index weight")),
                    "freefloat_marketcap": clean_value(row.get("Free float marketcap")),
                }

                cur.execute(
                    """
                    UPDATE kite_ticker_tickers
                    SET
                        change_1d = %(change_1d)s,
                        return_attribution = %(return_attribution)s,
                        index_weight = %(index_weight)s,
                        freefloat_marketcap = %(freefloat_marketcap)s,
                        last_updated = NOW()
                    WHERE tradingsymbol = %(tradingsymbol)s AND source_list = 'Nifty50';
                    """,
                    params,
                )

                if cur.rowcount == 0:
                    logging.warning(
                        "No row found for tradingsymbol %r with source_list 'Nifty50'.",
                        params["tradingsymbol"],
                    )

        conn.commit()
        return JSONResponse(content={"message": "Nifty50 data updated successfully."})
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="nifty50_data.csv not found.")
    except Exception as e:
        logging.error("An error occurred during the database update: %s", e)
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=f"An error occurred during the database update: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
