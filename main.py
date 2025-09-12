from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from fastapi import FastAPI, Request, Form, HTTPException, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn
import psycopg2
import psycopg2.extras
import pandas as pd
import numpy as np
import yfinance as yf
from scipy.stats import norm
from datetime import datetime, timedelta
import os
import json
from dotenv import load_dotenv

load_dotenv() # Load environment variables from .env file

from database import get_db_connection
from pytz import timezone
import random

import pandas as pd
import psycopg2
from psycopg2 import extras
import logging
from datetime import datetime, date # Import date for CURRENT_DATE

# Configure logging for the main application
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

import plotly.express as px
import pandas_market_calendars as mcal

from charts import charts_app


from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from broker_api.broker_api import router as broker_api_router

### fyers auth import ##
import httpx
import pyotp
import asyncio
import json
from urllib import parse
from fyers_apiv3 import fyersModel

from fastapi import FastAPI
from broker_api.broker_api import router as kite_router
from mcp_server import mcp_router
######
app = FastAPI(title="Kite App API")



app.mount("/charts", charts_app)
app.include_router(mcp_router, prefix="/kite_mcp", tags=["MCP"])
app.include_router(broker_api_router, prefix="/broker")

# CSV file paths and their corresponding source list names
CSV_FILES = {
    'ind_nifty50list.csv': 'Nifty50',
    'ind_niftylargemidcap250list.csv': 'NiftyLargeMidcap250',
    'ind_nifty500list.csv': 'Nifty500'
}

def process_csv_data(csv_file_path, source_list_name):
    """Reads a CSV file and returns a list of dictionaries with instrument details."""
    data = []
    try:
        df = pd.read_csv(csv_file_path)
        for index, row in df.iterrows():
            symbol = row['Symbol']
            company_name = row['Company Name']
            sector = row['Industry'] # Assuming 'Industry' column maps to 'sector'
            data.append({
                'symbol': symbol,
                'company_name': company_name,
                'sector': sector,
                'source_list': source_list_name
            })
        logging.info(f"Successfully processed {len(data)} entries from {csv_file_path}.")
    except FileNotFoundError:
        logging.error(f"CSV file not found: {csv_file_path}")
    except KeyError as e:
        logging.error(f"Missing expected column in {csv_file_path}: {e}")
    except Exception as e:
        logging.error(f"Error processing {csv_file_path}: {e}")
    return data

@app.post("/ingest-stock-data")
async def ingest_stock_data_endpoint():
    """
    FastAPI endpoint to trigger the stock market instrument data ingestion process.
    """
    logging.info("FastAPI endpoint /ingest-stock-data triggered.")
    
    all_csv_entries = []
    for file_path, source_name in CSV_FILES.items():
        all_csv_entries.extend(process_csv_data(file_path, source_name))

    if not all_csv_entries:
        logging.error("No data processed from CSV files. Aborting ingestion.")
        raise HTTPException(status_code=500, detail="No data processed from CSV files.")

    conn = None
    try:
        conn = get_db_connection()
        kite_instruments_data = {}
        with conn.cursor(cursor_factory=extras.DictCursor) as cur:
            cur.execute(
                "SELECT tradingsymbol, instrument_token, instrument_type FROM kite_instruments WHERE instrument_type = 'EQ';"
            )
            kite_instruments_data = {row['tradingsymbol']: row for row in cur.fetchall()}
            logging.info(f"Fetched {len(kite_instruments_data)} equity instruments from kite_instruments.")
        
        if not kite_instruments_data:
            logging.warning("No equity instruments found in kite_instruments table. Synchronization will not proceed.")
            raise HTTPException(status_code=500, detail="No equity instruments found in kite_instruments table.")

        inserted_count = 0
        unmatched_count = 0
        
        with conn.cursor() as cur:
            for entry in all_csv_entries:
                symbol = entry['symbol']
                company_name = entry['company_name']
                sector = entry['sector']
                source_list = entry['source_list']

                if symbol in kite_instruments_data:
                    instrument_token = kite_instruments_data[symbol]['instrument_token']
                    
                    try:
                        cur.execute(
                            """
                            INSERT INTO kite_ticker_tickers (instrument_token, tradingsymbol, company_name, sector, source_list)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (instrument_token) DO UPDATE SET
                                tradingsymbol = EXCLUDED.tradingsymbol,
                                company_name = EXCLUDED.company_name,
                                sector = EXCLUDED.sector,
                                source_list = EXCLUDED.source_list,
                                last_updated = CURRENT_TIMESTAMP;
                            """,
                            (instrument_token, symbol, company_name, sector, source_list)
                        )
                        inserted_count += 1
                        logging.debug(f"Inserted new record for {symbol} (Token: {instrument_token}, Source: {source_list})")
                    except Exception as e:
                        logging.error(f"Error inserting record for {symbol} (Token: {instrument_token}, Source: {source_list}): {e}")
                else:
                    unmatched_count += 1
                    logging.warning(f"Symbol '{symbol}' from '{source_list}' not found in kite_instruments (instrument_type='EQ').")
            
            conn.commit()
        logging.info(f"Data synchronization complete. Inserted {inserted_count} records. {unmatched_count} symbols were unmatched.")
        return JSONResponse(content={"message": "Data ingestion and synchronization completed successfully.", "inserted_records": inserted_count, "unmatched_symbols": unmatched_count})

    except Exception as e:
        logging.critical(f"An unhandled error occurred during ingestion: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error during ingestion: {e}")
    finally:
        if conn:
            conn.close()
            logging.info("Database connection closed.")



# Add CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Svelte dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Welcome to Kite App API!"}

@app.get("/hello")
async def hello():
    return {"message": "Hello World from FastAPI Backend!"}

@app.get("/status")
async def status():
    return {"status": "running", "backend": "FastAPI"}
from strategies.momentum import get_momentum_portfolio

@app.get("/momentum-portfolio")
def fetch_momentum_portfolio():
    """
    Returns top momentum stocks.
    """
    return get_momentum_portfolio()
