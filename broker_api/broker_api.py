import os
import uuid
import time
import json
import csv
import asyncio
from typing import List, Optional, Tuple, Dict
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from urllib import parse
import gzip
import csv
import io
import logging # Added logging


import requests
import httpx
import pyotp
import pytz
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm
from pydantic import BaseModel
from kiteconnect import KiteConnect
import uuid
# from datetime import datetime # Already imported above, no need to re-import

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Global state for historical data update progress
historical_data_update_progress = {
    "status": "idle", # "idle", "in_progress", "completed", "failed"
    "total_instruments": 0,
    "processed_instruments": 0,
    "current_instrument_symbol": "",
    "start_time": None,
    "end_time": None,
    "error": None,
}

from sqlalchemy import Column, String, DateTime, inspect
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Response, HTTPException, Request

from .kite_auth import login_headless
from kiteconnect import KiteConnect
from database import SessionLocal, Base

from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    APIRouter,
    HTTPException,
    Depends,
    Form,
    Cookie,
    Header,
    Query,
    Response
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from pydantic import BaseModel

from sqlalchemy import (
    create_engine,
    MetaData,
    Column,
    Integer,
    String,
    Date,
    Float,
    BigInteger,
    ForeignKey,
    Numeric,
    DateTime,
    Table,
    select
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, relationship, Session

from databases import Database

import psycopg2
from psycopg2.extras import execute_batch


from fastapi import APIRouter, Response, HTTPException, Depends
from sqlalchemy.orm import Session
import uuid

from database import SessionLocal  # Your DB session factory
from database import FyersSession

from broker_api.kite_auth import login_headless, get_kite
from broker_api.kite_auth import API_KEY



# Load environment variables
load_dotenv()

# API router
router = APIRouter()

# Pydantic request models
class TickerRequest(BaseModel):
    symbol: str

class InstrumentsRequest(BaseModel):
    instruments: List[str]


class PortfolioSnapshotCreate(BaseModel):
    strategy_name: str
    symbol: str
    quantity: int
    purchase_price: float # Use float for Pydantic, will be converted to Numeric for SQLAlchemy
    total_value: float # Use float for Pydantic, will be converted to Numeric for SQLAlchemy

    class Config:
        orm_mode = True # Enable ORM mode for Pydantic




# ───────── DATABASE SETUP ─────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krishna:1122@db.db-net:5432/finance")

# synchronous engine + session
engine       = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()
metadata     = MetaData()

# async database client
database     = Database(DATABASE_URL)

# module-level session storage
sessions: Dict[str, str] = {}

# ───────── ORM MODELS ─────────

class KiteSession(Base):
    __tablename__ = "kite_sessions"
    session_id    = Column(String(36), primary_key=True, index=True)
    access_token  = Column(String, nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False)


class Ticker(Base):
    __tablename__ = "tickers"
    id           = Column(Integer, primary_key=True)
    symbol       = Column(String(10), unique=True, nullable=False)
    company_name = Column(String(50))
    sector       = Column(String(50))
    kite_symbol = Column(String(50), unique=True, nullable=False)  # mimicking symbol constraints
    stock_data   = relationship("StockData", back_populates="ticker")

class StockData(Base):
    __tablename__ = "historical_stock_data"
    ticker_id = Column(Integer, ForeignKey("tickers.id"), primary_key=True)
    date      = Column(Date, primary_key=True)
    open      = Column(Float)
    high      = Column(Float)
    low       = Column(Float)
    close     = Column(Float)
    volume    = Column(BigInteger)
    ticker    = relationship("Ticker", back_populates="stock_data")

class PortfolioAllocation(Base):
    __tablename__ = "portfolio_allocations"
    symbol             = Column(String, primary_key=True)
    target_weight_pct  = Column(Numeric, nullable=False)
    allocated_funds    = Column(Numeric, nullable=False)
    approximate_shares = Column(Integer, nullable=False)

class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"
    symbol       = Column(String, primary_key=True)
    shares       = Column(Integer, nullable=False)
    avg_price    = Column(Numeric, nullable=False)
    last_updated = Column(DateTime, nullable=False)

class OrderHistory(Base):
    __tablename__ = "order_history"
    order_id       = Column(Integer, primary_key=True, autoincrement=True)
    symbol         = Column(String, nullable=False)
    side           = Column(String, nullable=False)
    qty            = Column(Integer, nullable=False)
    price          = Column(Numeric, nullable=False)
    order_tag      = Column(String, nullable=False)
    placed_at      = Column(DateTime, nullable=False)
    pnl_pct        = Column(Numeric, nullable=True)
    pnl_annual_pct = Column(Numeric, nullable=True)

# ───────── ORM MODEL FOR INSTRUMENTS ─────────
class KiteInstrument(Base):
    __tablename__ = "kite_instruments"
    instrument_token = Column(BigInteger, primary_key=True)
    exchange_token = Column(BigInteger)
    tradingsymbol = Column(String, index=True)
    name = Column(String)
    last_price = Column(Float)
    expiry = Column(Date)
    strike = Column(Float)
    tick_size = Column(Float)
    lot_size = Column(Integer)
    instrument_type = Column(String, index=True)  # EQ, FUT, CE, PE, etc.
    segment = Column(String, index=True)          # EQ, NFO-FUT, NFO-OPT, etc.
    exchange = Column(String, index=True)         # NSE, NFO, BSE, BFO, MCX, etc.
    last_updated = Column(DateTime, default=datetime.utcnow)


class KiteIndex(Base):
    __tablename__ = "kite_indices"
    instrument_token = Column(BigInteger, primary_key=True)
    exchange_token = Column(BigInteger)
    tradingsymbol = Column(String, index=True)
    name = Column(String)
    last_price = Column(Float)
    expiry = Column(Date)
    strike = Column(Float)
    tick_size = Column(Float)
    lot_size = Column(Integer)
    instrument_type = Column(String, index=True)
    segment = Column(String, index=True)
    exchange = Column(String, index=True)
    last_updated = Column(DateTime, default=datetime.utcnow)

class KiteIndexHistoricalData(Base):
    __tablename__ = "kite_indices_historical_data"
    instrument_token = Column(BigInteger, ForeignKey("kite_indices.instrument_token", ondelete="CASCADE"), primary_key=True)
    timestamp = Column(DateTime(timezone=True), primary_key=True)
    interval = Column(String(10), primary_key=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(BigInteger, nullable=False)
    oi = Column(BigInteger)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    strategy_name = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    purchase_price = Column(Numeric, nullable=False)
    total_value = Column(Numeric, nullable=False)

class PortfolioHistory(Base):
    __tablename__ = "portfolio_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    strategy_name = Column(String, nullable=False)
    total_capital = Column(Numeric, nullable=False)
    total_value = Column(Numeric, nullable=False)
    profit_loss = Column(Numeric, nullable=False)
    percentage_change = Column(Numeric, nullable=False)

# ───────── FASTAPI SETUP ─────────
app    = FastAPI()
router = APIRouter()



@router.on_event("startup")
async def _startup():
    # For development: Drop table if it exists to ensure fresh schema with primary key
    inspector = inspect(engine)

    # auto-create all tables (including kite_instruments with primary key)
    Base.metadata.create_all(bind=engine)
    await database.connect()
    
    # Start background task for daily instruments update
    asyncio.create_task(schedule_daily_instruments_update())

@router.on_event("shutdown")
async def _shutdown():
    await database.disconnect()

def get_db() -> Session:
    """
    Dependency: yields a SQLAlchemy Session and closes it after use.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_token(session_id: Optional[str] = Cookie(None), db: Session = Depends(get_db)) -> str:
    if session_id:
        session = db.query(FyersSession).filter_by(session_id=session_id).first()
        if session:
            return session.access_token
    raise HTTPException(status_code=401, detail="Unauthorized")


def get_psql_conn():
    """
    Fallback raw psycopg2 connection for ad-hoc queries.
    """
    return psycopg2.connect(DATABASE_URL)





######kite

# ─────────── Helper to load Kite client ───────────
def get_kite(request: Request, db: Session = Depends(get_db)) -> KiteConnect:
    # Support session via header (for dev cross-origin) or cookie
    sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
    if not sid:
        raise HTTPException(401, "Not authenticated; login first")
    ks = db.query(KiteSession).filter_by(session_id=sid).first()
    if not ks:
        raise HTTPException(401, "Invalid session")
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ks.access_token)
    return kite






# ─────────── Login endpoint ───────────
@router.post("/login_kite")
def headless_login(request: Request, response: Response, db: Session = Depends(get_db)):
    try:
        kite, at = login_headless()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"An unexpected error occurred: {e}")

    sid = str(uuid.uuid4())
    db.add(KiteSession(session_id=sid, access_token=at))
    db.commit()

    # Determine if the request is over HTTPS (directly or via reverse proxy)
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto or request.url.scheme
    is_secure = scheme == "https"

    # For cross-origin XHR/fetch with cookies, browsers require SameSite=None and Secure when using HTTPS.
    # In dev over plain HTTP across devices, some browsers will block SameSite=None without Secure.
    # We still set the cookie for completeness, and also return session_id for header-based auth as a fallback.
    response.set_cookie(
        "kite_session_id",
        sid,
        httponly=True,
        secure=is_secure,
        samesite="none" if is_secure else "lax",
    )

    # Also return session_id so the frontend can send it in the X-Session-ID header (dev-friendly)
    return {"session_id": sid, "access_token": at, "profile": kite.profile()}


# ─────────── Logout endpoint ───────────
@router.post("/logout_kite")
def logout(response: Response, request: Request, db: Session = Depends(get_db)):
    sid = request.cookies.get("kite_session_id")
    if sid:
        db.query(KiteSession).filter_by(session_id=sid).delete()
        db.commit()
    response.delete_cookie("kite_session_id")
    return {"message": "Logged out"}









# ─────────── Profile & holdings ───────────
@router.get("/profile_kite")
def profile(kite: KiteConnect = Depends(get_kite)):
    return kite.profile()


@router.get("/holdings_kite")
def holdings(kite: KiteConnect = Depends(get_kite)):
    return kite.holdings()

@router.get("/margins")
def get_margins(kite: KiteConnect = Depends(get_kite)):
    try:
        margins = kite.margins()
        
        # Filter for the essential fields
        essential_margins = {
            "equity": {
                "net": margins["equity"]["net"],
                "opening_balance": margins["equity"]["available"]["opening_balance"],
                "m2m_unrealised": margins["equity"]["utilised"]["m2m_unrealised"],
                "m2m_realised": margins["equity"]["utilised"]["m2m_realised"],
            },
            "commodity": {
                "net": margins["commodity"]["net"],
                "opening_balance": margins["commodity"]["available"]["opening_balance"],
                "m2m_unrealised": margins["commodity"]["utilised"]["m2m_unrealised"],
                "m2m_realised": margins["commodity"]["utilised"]["m2m_realised"],
            }
        }
        return essential_margins
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/ltp")
def get_ltp(request: InstrumentsRequest, kite: KiteConnect = Depends(get_kite)):
    """
    Retrieve last price for a list of instruments.
    Instruments are in the format of `exchange:tradingsymbol`. For example NSE:INFY
    """
    try:
        # The kite.ltp method expects a list of instrument strings
        ltp_data = kite.ltp(request.instruments)
        return ltp_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve LTP: {str(e)}")

 
 # ─────────── Instruments import functionality ───────────
async def upsert_instrument(record, db_session):
    """Upsert instrument with proper error handling for constraint issues"""
    query = """
        INSERT INTO kite_instruments (
            instrument_token, exchange_token, tradingsymbol, name, last_price,
            expiry, strike, tick_size, lot_size, instrument_type, segment, exchange, last_updated
        ) VALUES (
            :instrument_token, :exchange_token, :tradingsymbol, :name, :last_price,
            :expiry, :strike, :tick_size, :lot_size, :instrument_type, :segment, :exchange, NOW()
        )
        ON CONFLICT (instrument_token) DO UPDATE SET
            exchange_token = EXCLUDED.exchange_token,
            tradingsymbol = EXCLUDED.tradingsymbol,
            name = EXCLUDED.name,
            last_price = EXCLUDED.last_price,
            expiry = EXCLUDED.expiry,
            strike = EXCLUDED.strike,
            tick_size = EXCLUDED.tick_size,
            lot_size = EXCLUDED.lot_size,
            instrument_type = EXCLUDED.instrument_type,
            segment = EXCLUDED.segment,
            exchange = EXCLUDED.exchange,
            last_updated = NOW()
    """
    await database.execute(query, values={
        "instrument_token": int(record['instrument_token']) if record['instrument_token'] else None,
        "exchange_token": int(record['exchange_token']) if record['exchange_token'] else None,
        "tradingsymbol": record['tradingsymbol'],
        "name": record.get('name', ''),
        "last_price": float(record['last_price']) if record['last_price'] else None,
        "expiry": record['expiry'] if record['expiry'] else None,
        "strike": float(record['strike']) if record['strike'] else None,
        "tick_size": float(record['tick_size']) if record['tick_size'] else None,
        "lot_size": int(record['lot_size']) if record['lot_size'] else None,
        "instrument_type": record.get('instrument_type', ''),
        "segment": record.get('segment', ''),
        "exchange": record.get('exchange', '')
    })

async def import_instruments_for_exchange(exchange: str, kite: KiteConnect):
    """Import instruments for a specific exchange"""
    try:
        # Get instruments from Kite API
        instruments = kite.instruments(exchange)
        
        # Upsert each instrument
        async with database.transaction():
            for record in instruments:
                await upsert_instrument(record, None)  # db_session parameter not used in current implementation
        
        return {"message": f"Imported {len(instruments)} instruments for exchange {exchange}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to import instruments: {str(e)}")

# ─────────── Instruments endpoints ───────────

@router.post("/import_instruments/all")
async def import_all_instruments(kite: KiteConnect = Depends(get_kite)):
    """Import all instruments from major exchanges"""
    exchanges = ["NSE", "NFO", "BSE", "BFO", "MCX"]
    results = []
    
    for exchange in exchanges:
        try:
            result = await import_instruments_for_exchange(exchange, kite)
            results.append(result)
        except Exception as e:
            results.append({"exchange": exchange, "error": str(e)})
    
    return {"message": "Imported all instruments", "results": results}

@router.get("/instruments/nse")
async def get_nse_instruments():
    """Get NSE equity instruments"""
    query = "SELECT * FROM kite_instruments WHERE exchange = 'NSE' AND instrument_type = 'EQ' ORDER BY tradingsymbol"
    results = await database.fetch_all(query)
    return results

@router.get("/instruments/nfo")
async def get_nfo_instruments():
    """Get NFO instruments"""
    query = "SELECT * FROM kite_instruments WHERE exchange = 'NFO' ORDER BY tradingsymbol"
    results = await database.fetch_all(query)
    return results

@router.get("/instruments/commodity")
async def get_commodity_instruments():
    """Get commodity instruments"""
    query = "SELECT * FROM kite_instruments WHERE exchange IN ('MCX', 'BFO') ORDER BY tradingsymbol"
    results = await database.fetch_all(query)
    return results

@router.get("/instruments/search/{symbol}")
async def search_instruments(symbol: str):
    """Search instruments by symbol"""
    query = "SELECT * FROM kite_instruments WHERE tradingsymbol ILIKE :symbol ORDER BY tradingsymbol"
    results = await database.fetch_all(query, {"symbol": f"%{symbol}%"})
    return results

# ─────────── Daily update functionality ───────────
async def schedule_daily_instruments_update():
    """Schedule daily instruments update task"""
    IST = pytz.timezone('Asia/Kolkata')
    while True:
        try:
            now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
            now_ist = now_utc.astimezone(IST)

            # Calculate next run time for 8:00 AM IST
            next_run_ist = now_ist.replace(hour=7, minute=0, second=0, microsecond=0)
            if now_ist >= next_run_ist:
                next_run_ist += timedelta(days=1)

            # Convert next_run_ist to UTC for comparison and sleep calculation
            next_run_utc = next_run_ist.astimezone(pytz.utc)
            delay = (next_run_utc - now_utc).total_seconds()

            logger.info(f"Next instrument update scheduled for {next_run_ist.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
            await asyncio.sleep(delay)

            # Run the daily update
            await update_all_instruments_daily()

        except Exception as e:
            logger.error(f"Error in daily instruments update scheduler: {e}", exc_info=True)
            # Wait for 1 hour before retrying the scheduler logic
            await asyncio.sleep(60 * 60)

async def update_all_instruments_daily():
    """Update all instruments daily"""
    logger.info("Daily instruments update started.")

    try:
        # Obtain a KiteConnect instance for the background task
        kite, _ = login_headless()
    except HTTPException as e:
        logger.error(f"Error during headless login for daily update: {e.detail}", exc_info=True)
        return
    except Exception as e:
        logger.error(f"Unexpected error during headless login for daily update: {e}", exc_info=True)
        return

    exchanges = ["NSE", "NFO", "BSE", "BFO", "MCX"]
    for exchange in exchanges:
        try:
            logger.info(f"Importing instruments for exchange: {exchange}")
            await import_instruments_for_exchange(exchange, kite)
            logger.info(f"Successfully imported instruments for exchange: {exchange}")
        except Exception as e:
            logger.error(f"Error importing instruments for exchange {exchange}: {e}", exc_info=True)

    logger.info("Daily instruments update completed.")

####KITE
from .historical_data import fetch_and_store_historical_data, fetch_and_store_indices_historical_data
from database import get_db_connection
from fastapi import BackgroundTasks

@router.post("/clear_historical_data")
def clear_historical_data(conn = Depends(get_psql_conn)):
    """
    Deletes all records from the kite_historical_data table.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE kite_historical_data RESTART IDENTITY;")
            conn.commit()
        return {"message": "Successfully cleared all historical data."}
    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"Error clearing historical data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error clearing historical data: {e}")
    finally:
        if conn:
            conn.close()

@router.post("/fetch_historical_data")
async def fetch_historical_data_initial(background_tasks: BackgroundTasks, kite: KiteConnect = Depends(get_kite)):
    """
    Fetches historical data for all instruments in the kite_ticker_tickers table for the last 3 years.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT instrument_token, tradingsymbol FROM kite_ticker_tickers")
            instruments = [{"token": row[0], "symbol": row[1]} for row in cur.fetchall()]
        
        if not instruments:
            return {"message": "No instruments found in kite_ticker_tickers table. Nothing to fetch."}

        # Define the user's timezone to ensure all date operations are consistent.
        IST = pytz.timezone('Asia/Calcutta')
        to_date = datetime.now(IST)
        from_date = to_date - timedelta(days=3*260)
        
        background_tasks.add_task(run_historical_data_fetch, kite, instruments, from_date, to_date, "day")
        
        logging.info(f"Started background task to fetch historical data for {len(instruments)} instruments.")
        return {"message": f"Started fetching historical data for {len(instruments)} instruments in the background."}
    except Exception as e:
        logging.error(f"Error starting historical data fetch: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting historical data fetch: {e}")
    finally:
        if conn:
            conn.close()

def run_historical_data_fetch(kite: KiteConnect, instruments: list, from_date: datetime, to_date: datetime, interval: str):
    """
    The actual data fetching and storing process that runs in the background.
    """
    conn = None
    try:
        conn = get_db_connection()
        total_records = 0
        instrument = None # Define here for use in exception logging
        
        # Convert datetime to date for the fetch function
        start_date = from_date.date()
        end_date = to_date.date()

        for instrument in instruments:
            records_fetched = fetch_and_store_historical_data(
                kite, conn, instrument["token"], instrument["symbol"], start_date, end_date, interval
            )
            if records_fetched > 0:
                # Commit after each instrument to ensure data is saved incrementally.
                conn.commit()
                total_records += records_fetched
                logging.info(f"Committed {records_fetched} records for {instrument['symbol']}")
        
        logging.info(f"Finished initial historical data fetch. Total records committed: {total_records}.")
    except Exception as e:
        logging.error(f"Error during historical data fetch for instrument {instrument.get('token', 'N/A') if instrument else 'N/A'}: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


@router.post("/update_historical_data")
async def update_historical_data(
    background_tasks: BackgroundTasks,
    kite: KiteConnect = Depends(get_kite),
    to_date: Optional[date] = Query(None, description="The end date for the data fetch in YYYY-MM-DD format. Defaults to today.")
):
    """
    Updates historical data for all instruments. Fetches data from the last recorded point up to the specified `to_date`.
    """
    global historical_data_update_progress
    historical_data_update_progress = {
        "status": "in_progress",
        "total_instruments": 0,
        "processed_instruments": 0,
        "current_instrument_symbol": "",
        "start_time": datetime.now().isoformat(),
        "end_time": None,
        "error": None,
    }

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT instrument_token, tradingsymbol FROM kite_ticker_tickers")
            instruments = [{"token": row[0], "symbol": row[1]} for row in cur.fetchall()]
        
        if not instruments:
            historical_data_update_progress.update({
                "status": "completed",
                "end_time": datetime.now().isoformat(),
                "error": "No instruments found in kite_ticker_tickers table. Nothing to update."
            })
            return {"message": "No instruments found in kite_ticker_tickers table. Nothing to update."}

        historical_data_update_progress["total_instruments"] = len(instruments)

        # Define the user's timezone
        IST = pytz.timezone('Asia/Calcutta')

        # Use the provided to_date, or default to today's date in the correct timezone.
        end_date_val = to_date if to_date else datetime.now(IST).date()
        
        background_tasks.add_task(run_historical_data_update, kite, instruments, "day", end_date_val)
        
        logging.info(f"Started background task to update historical data for {len(instruments)} instruments.")
        return {"message": f"Started updating historical data for {len(instruments)} instruments in the background."}
    except Exception as e:
        logging.error(f"Error starting historical data update: {e}", exc_info=True)
        historical_data_update_progress.update({
            "status": "failed",
            "end_time": datetime.now().isoformat(),
            "error": str(e)
        })
        raise HTTPException(status_code=500, detail=f"Error starting historical data update: {e}")
    finally:
        if conn:
            conn.close()

def run_historical_data_update(kite: KiteConnect, instruments: list, interval: str, to_date: date):
    """
    The actual data updating process that runs in the background.
    """
    global historical_data_update_progress
    conn = None
    try:
        conn = get_db_connection()
        total_records = 0
        
        instrument = None # Define here for use in exception logging
        for i, instrument in enumerate(instruments, 1):
            historical_data_update_progress.update({
                "processed_instruments": i,
                "current_instrument_symbol": instrument['symbol']
            })
            logging.info(f"Processing instrument {i}/{len(instruments)}: {instrument['symbol']} ({instrument['token']})")
            
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT MAX("timestamp") FROM kite_historical_data WHERE instrument_token = %s AND interval = %s""",
                    (instrument["token"], interval)
                )
                last_timestamp = cur.fetchone()[0]
            
            if last_timestamp:
                # Fetch from the day after the last recorded timestamp to get only new data
                from_date = last_timestamp.date() + timedelta(days=1)
                logging.info(f"Last record for {instrument['symbol']} ({instrument['token']}) is on {last_timestamp.date()}. Fetching new data from {from_date}.")
            else:
                # If no data exists, fetch for the last 3 years.
                from_date = to_date - timedelta(days=3*260) # Approx 3 years of trading days
                logging.info(f"No existing data for {instrument['symbol']} ({instrument['token']}). Fetching last 3 years from {from_date}.")

            if from_date <= to_date:
                logging.info(f"Date range valid for {instrument['symbol']}: {from_date} to {to_date}")
                records_fetched = fetch_and_store_historical_data(
                    kite, conn, instrument["token"], instrument["symbol"], from_date, to_date, interval
                )
                if records_fetched > 0:
                    # Commit after each instrument to ensure data is saved incrementally.
                    conn.commit()
                    total_records += records_fetched
                    logging.info(f"Successfully committed {records_fetched} new records for {instrument['symbol']}")
                else:
                    logging.info(f"No new records to commit for {instrument['symbol']}")
            else:
                logging.info(f"Data for {instrument['symbol']} ({instrument['token']}) is already up to date (from_date {from_date} > to_date {to_date}).")

        historical_data_update_progress.update({
            "status": "completed",
            "end_time": datetime.now().isoformat(),
            "processed_instruments": len(instruments)
        })
        logging.info(f"Finished historical data update. Total new records committed: {total_records}.")
    except Exception as e:
        logging.error(f"Error during historical data update for instrument {instrument.get('token', 'N/A') if instrument else 'N/A'}: {e}", exc_info=True)
        historical_data_update_progress.update({
            "status": "failed",
            "end_time": datetime.now().isoformat(),
            "error": str(e)
        })
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

@router.get("/historical_data_progress")
async def get_historical_data_progress():
    """
    Returns the current progress of the historical data update.
    """
    global historical_data_update_progress
    return historical_data_update_progress




@router.post("/update_indices_from_instruments")
async def update_indices_from_instruments():
    """
    Updates the kite_indices table with data from kite_instruments where the segment is 'INDICES'.
    """
    try:
        # First, clear the existing indices to ensure the table is fresh
        delete_query = "TRUNCATE TABLE kite_indices RESTART IDENTITY CASCADE;"
        await database.execute(delete_query)

        # Now, select and insert the indices from the instruments table
        insert_query = """
            INSERT INTO kite_indices (
                instrument_token, exchange_token, tradingsymbol, name, last_price,
                expiry, strike, tick_size, lot_size, instrument_type, segment, exchange, last_updated
            )
            SELECT
                instrument_token, exchange_token, tradingsymbol, name, last_price,
                expiry, strike, tick_size, lot_size, instrument_type, segment, exchange, last_updated
            FROM
                kite_instruments
            WHERE
                segment = 'INDICES'
        """
        await database.execute(insert_query)

        return {"message": "Successfully updated the indices table."}
    except Exception as e:
        logging.error(f"Error updating indices table: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating indices table: {e}")


@router.post("/fetch_indices_historical_data")
async def fetch_indices_historical_data(background_tasks: BackgroundTasks, kite: KiteConnect = Depends(get_kite)):
    """
    Fetches historical data for all indices in the kite_indices table for the last 5 years.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT instrument_token, tradingsymbol FROM kite_indices")
            instruments = [{"token": row[0], "symbol": row[1]} for row in cur.fetchall()]
        
        if not instruments:
            return {"message": "No instruments found in kite_indices table. Nothing to fetch."}

        IST = pytz.timezone('Asia/Calcutta')
        to_date = datetime.now(IST)
        from_date = to_date - timedelta(days=5*365)
        
        background_tasks.add_task(run_historical_data_fetch_indices, kite, instruments, from_date, to_date, "day")
        
        logging.info(f"Started background task to fetch historical data for {len(instruments)} indices.")
        return {"message": f"Started fetching historical data for {len(instruments)} indices in the background."}
    except Exception as e:
        logging.error(f"Error starting historical data fetch for indices: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting historical data fetch for indices: {e}")
    finally:
        if conn:
            conn.close()

def run_historical_data_fetch_indices(kite: KiteConnect, instruments: list, from_date: datetime, to_date: datetime, interval: str):
    """
    The actual data fetching and storing process for indices that runs in the background.
    """
    conn = None
    try:
        conn = get_db_connection()
        total_records = 0
        instrument = None
        
        start_date = from_date.date()
        end_date = to_date.date()
        logging.info(f"[IMPORTANT] Indices historical fetch: instruments={len(instruments)}, date_range={start_date}..{end_date}, interval={interval}")

        for instrument in instruments:
            records_fetched = fetch_and_store_indices_historical_data(
                kite, conn, instrument["token"], instrument["symbol"], start_date, end_date, interval
            )
            if records_fetched > 0:
                conn.commit()
                total_records += records_fetched
                logging.info(f"Committed {records_fetched} records for index {instrument['symbol']}")
        
        logging.info(f"Finished initial historical data fetch for indices. Total records committed: {total_records}.")
    except Exception as e:
        logging.error(f"Error during historical data fetch for index {instrument.get('token', 'N/A') if instrument else 'N/A'}: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

@router.post("/update_indices_historical_data")
async def update_indices_historical_data(
    background_tasks: BackgroundTasks,
    kite: KiteConnect = Depends(get_kite),
    to_date: Optional[date] = Query(None, description="The end date for the data fetch in YYYY-MM-DD format. Defaults to today.")
):
    """
    Updates historical data for all indices. Fetches data from the last recorded point up to the specified `to_date`.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT instrument_token, tradingsymbol FROM kite_indices")
            instruments = [{"token": row[0], "symbol": row[1]} for row in cur.fetchall()]
        
        if not instruments:
            return {"message": "No instruments found in kite_indices table. Nothing to update."}

        IST = pytz.timezone('Asia/Calcutta')
        end_date_val = to_date if to_date else datetime.now(IST).date()
        
        background_tasks.add_task(run_historical_data_update_indices, kite, instruments, "day", end_date_val)
        
        logging.info(f"Started background task to update historical data for {len(instruments)} indices.")
        return {"message": f"Started updating historical data for {len(instruments)} indices in the background."}
    except Exception as e:
        logging.error(f"Error starting historical data update for indices: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting historical data update for indices: {e}")
    finally:
        if conn:
            conn.close()

def run_historical_data_update_indices(kite: KiteConnect, instruments: list, interval: str, to_date: date):
    """
    The actual data updating process for indices that runs in the background.
    """
    conn = None
    try:
        conn = get_db_connection()
        total_records = 0

        logging.info(f"[IMPORTANT] Indices historical update: instruments={len(instruments)}, interval={interval}, to_date={to_date}")

        instrument = None
        for i, instrument in enumerate(instruments, 1):
            logging.info(f"Processing index {i}/{len(instruments)}: {instrument['symbol']} ({instrument['token']})")
            
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT MAX("timestamp") FROM kite_indices_historical_data WHERE instrument_token = %s AND interval = %s""",
                    (instrument["token"], interval)
                )
                last_timestamp = cur.fetchone()[0]
            
            if last_timestamp:
                from_date = last_timestamp.date() + timedelta(days=1)
                logging.info(f"Last record for index {instrument['symbol']} is on {last_timestamp.date()}. Fetching new data from {from_date}.")
            else:
                from_date = to_date - timedelta(days=5*365)
                logging.info(f"No existing data for index {instrument['symbol']}. Fetching last 5 years from {from_date}.")

            if from_date <= to_date:
                records_fetched = fetch_and_store_indices_historical_data(
                    kite, conn, instrument["token"], instrument["symbol"], from_date, to_date, interval
                )
                if records_fetched > 0:
                    conn.commit()
                    total_records += records_fetched
                    logging.info(f"Successfully committed {records_fetched} new records for index {instrument['symbol']}")
                else:
                    logging.info(f"No new records to commit for index {instrument['symbol']}")
            else:
                logging.info(f"Data for index {instrument['symbol']} is already up to date.")

        logging.info(f"Finished historical data update for indices. Total new records committed: {total_records}.")
    except Exception as e:
        logging.error(f"Error during historical data update for index {instrument.get('token', 'N/A') if instrument else 'N/A'}: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
