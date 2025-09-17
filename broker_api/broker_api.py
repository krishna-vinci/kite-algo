import os
import uuid
import time
import json
import csv
import asyncio
from typing import List, Optional, Tuple, Dict, Any
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

SCHEDULER_NTFY_URL = os.getenv("SCHEDULER_NTFY_URL", "https://ntfy.krishna.quest/scheduler-alerts")

async def send_ntfy_notification(message: str, title: str = "Kite App Notification", tags: Optional[List[str]] = None):
    """Sends a notification to the ntfy.sh topic."""
    try:
        headers = {"Title": title}
        if tags:
            headers["Tags"] = ",".join(tags)
        async with httpx.AsyncClient() as client:
            response = await client.post(SCHEDULER_NTFY_URL, content=message, headers=headers)
            response.raise_for_status()
            logger.info(f"ntfy notification sent: {message}")
    except httpx.RequestError as e:
        logger.error(f"ntfy notification failed (request error): {e}")
    except httpx.HTTPStatusError as e:
        logger.error(f"ntfy notification failed (HTTP error): {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logger.error(f"ntfy notification failed (unexpected error): {e}")

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
from fastapi import WebSocket, WebSocketDisconnect

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

# ---- Centralized helpers for system token (DB is source of truth) ----
def upsert_kite_session(db: Session, session_id: str, access_token: str) -> "KiteSession":
    """
    If a KiteSession with session_id exists, update access_token and created_at=now().
    Else insert a new row. Caller is responsible for commit.
    """
    obj = db.query(KiteSession).filter_by(session_id=session_id).first()
    now_dt = datetime.utcnow()
    if obj:
        obj.access_token = access_token
        obj.created_at = now_dt
        return obj
    obj = KiteSession(session_id=session_id, access_token=access_token, created_at=now_dt)
    db.add(obj)
    return obj

def get_system_access_token(db: Session) -> Optional[str]:
    """Return access_token for session_id == 'system' if exists, else None."""
    ks = db.query(KiteSession).filter_by(session_id="system").first()
    return ks.access_token if ks else None

def run_headless_login_and_persist_system_token(db: Session) -> str:
    """
    Perform headless login and upsert the access_token to KiteSession with session_id='system'.
    Caller is responsible for committing the transaction.
    Returns the redacted fingerprint (last 6 chars) of the access_token.
    """
    kite, at = login_headless()
    upsert_kite_session(db, "system", at)
    fp = at[-6:] if isinstance(at, str) else ""
    logger.info("System access_token refreshed and upserted (..%s)", fp)
    return fp


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
    
    # Daily instruments update scheduling is managed by main; no internal scheduler here

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

    # Also persist/refresh system token so app startup and jobs use a consistent source
    upsert_kite_session(db, "system", at)
    db.commit()
    logger.info("System access token upserted via login (..%s)", (at[-6:] if isinstance(at, str) else ""))

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


@router.get("/instruments/fuzzy-search")
async def fuzzy_search_instruments(query: str = Query(..., min_length=1)):
    """
    Fuzzy search across indices, NSE equities, and NFO contracts with alias handling.
    Priority ranking:
      0) exact tradingsymbol match (including aliases like 'banknifty' -> 'NIFTY BANK')
      1) prefix tradingsymbol match
      2) prefix name match
      3) INDICES exchange
      4) alphabetical
    Returns up to 20 rows with fields:
      instrument_token, tradingsymbol, name, exchange, instrument_type, segment
    """
    q = (query or "").strip()
    if not q:
        return []

    # Enhanced parsing to separate instrument identifiers from numeric values
    # This helps with queries like "NIFTY 25000"
    parts = q.split()
    instrument_query_base = q
    numeric_value = None
    
    # If the last part is a number, separate it
    if len(parts) > 1:
        try:
            numeric_value = float(parts[-1])
            instrument_query_base = " ".join(parts[:-1])
        except ValueError:
            pass

    # Basic alias normalization for common India index terms
    qm = instrument_query_base.replace(" ", "").lower()
    alias_map = {
        "nifty50": "NIFTY 50",
        "nifty": "NIFTY 50",  # common shorthand
        "banknifty": "NIFTY BANK",
        "sensex": "SENSEX",
        "finnifty": "FINNIFTY",
        "niftymidcap100": "NIFTY MIDCAP 100",
        "midcap100": "NIFTY MIDCAP 100",
    }
    alias = alias_map.get(qm, instrument_query_base)

    # Prepare for "contains all words" check
    search_words = [word.strip() for word in instrument_query_base.split() if word.strip()]
    
    # Base WHERE condition
    where_clause = "(tradingsymbol ILIKE :like OR name ILIKE :like)"
    
    # Add parameters for dynamic WHERE clause and ORDER BY
    params = {
        "instrument_query": instrument_query_base,
        "alias": alias,
        "like": f"%{instrument_query_base}%",
        "prefix": f"{instrument_query_base}%",
        "alias_prefix": f"{alias}%",
        "numeric_value": numeric_value,
        "has_numeric_value": numeric_value is not None, # New parameter for explicit check
        "is_index_query": any(idx_term in instrument_query_base.lower() for idx_term in ['nifty', 'banknifty', 'sensex', 'finnifty'])
    }

    # Add individual word parameters for "contains all words" check
    for i, word in enumerate(search_words):
        params[f"word_{i}"] = f"%{word}%"
        # Also add to where_clause for initial filtering
        where_clause += f" AND (tradingsymbol ILIKE :word_{i} OR name ILIKE :word_{i})"


    sql = f"""
    WITH universe AS (
        SELECT instrument_token, tradingsymbol, name, exchange, instrument_type, segment, strike
        FROM kite_instruments
        WHERE {where_clause}
        UNION ALL
        SELECT instrument_token, tradingsymbol, name, exchange, instrument_type, segment, strike
        FROM kite_indices
        WHERE {where_clause}
    )
    SELECT instrument_token, tradingsymbol, name, exchange, instrument_type, segment
    FROM universe
    ORDER BY
        CASE
            -- Absolute Exact Matches (Priority 0)
            WHEN lower(tradingsymbol) = lower(:alias) THEN 0
            WHEN lower(tradingsymbol) = lower(:instrument_query) THEN 0
            WHEN lower(name) = lower(:alias) THEN 0
            WHEN lower(name) = lower(:instrument_query) THEN 0

            -- Strong Prefix Matches - tradingsymbol (Priority 1)
            WHEN tradingsymbol ILIKE :alias_prefix THEN 1
            WHEN tradingsymbol ILIKE :prefix THEN 1

            -- Strong Prefix Matches - name (Priority 2)
            WHEN name ILIKE :alias_prefix THEN 2
            WHEN name ILIKE :prefix THEN 2

            -- Options contracts with strike close to numeric value (Priority 3)
            WHEN :has_numeric_value AND (instrument_type = 'CE' OR instrument_type = 'PE')
                 AND ABS(strike - :numeric_value) <= 50 THEN 3

            -- Contains All Words - tradingsymbol (Priority 4)
            {"WHEN " + " AND ".join([f"tradingsymbol ILIKE :word_{i}" for i in range(len(search_words))]) + " THEN 4" if len(search_words) > 1 else ""}

            -- Contains All Words - name (Priority 5)
            {"WHEN " + " AND ".join([f"name ILIKE :word_{i}" for i in range(len(search_words))]) + " THEN 5" if len(search_words) > 1 else ""}

            -- Indices Boost (Priority 6) - only if query is index-related
            WHEN exchange = 'INDICES' AND :is_index_query THEN 6

            -- General Substring Matches (Priority 7)
            WHEN tradingsymbol ILIKE :like THEN 7
            WHEN name ILIKE :like THEN 7

            ELSE 8
        END,
        tradingsymbol
    LIMIT 10
    """
    rows = await database.fetch_all(sql, params)
    # Add a temporary test field to verify that the latest code is running
    return [{**row, "test_field": "test_value"} for row in rows]

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
            await send_ntfy_notification("Daily instrument update completed successfully.", title="Scheduler Success", tags=["success", "instruments"])

        except Exception as e:
            logger.error(f"Error in daily instruments update scheduler: {e}", exc_info=True)
            await send_ntfy_notification(f"Daily instrument update failed: {e}", title="Scheduler Failure", tags=["failure", "instruments"])
            # Wait for 1 hour before retrying the scheduler logic
            await asyncio.sleep(60 * 60)

async def update_all_instruments_daily():
    """Update all instruments daily"""
    logger.info("Daily instruments update started.")

    try:
        # Obtain a KiteConnect instance for the background task
        kite, at = login_headless()
        # Persist the system token obtained during daily job
        try:
            _db = SessionLocal()
            upsert_kite_session(_db, "system", at)
            _db.commit()
            logger.info("System access token upserted via daily job (..%s)", (at[-6:] if isinstance(at, str) else ""))
        finally:
            try:
                _db.close()
            except Exception:
                pass
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
            await send_ntfy_notification(f"Error importing instruments for exchange {exchange}: {e}", title="Instrument Import Failure", tags=["failure", "instruments"])

    logger.info("Daily instruments update completed.")
    await send_ntfy_notification("All instruments updated successfully.", title="Instrument Update Success", tags=["success", "instruments"])

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



# ───────── Alerts (Kite Alerts API proxy + DB mirror) ─────────
from fastapi import Request, Body
from typing import Any as _Any

# Sub-router for alerts
alerts_router = APIRouter(prefix="/alerts", tags=["alerts"])

def _alerts_ntfy_url() -> str:
    return os.getenv("KITE_ALERTS_NTFY_URL") or os.getenv("kite_alerts_NTFY_URL") or "https://ntfy.krishna.quest/kite-alerts"

def _kite_alerts_headers(api_key: str, access_token: str) -> Dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

async def _alerts_upsert_db(row: Dict[str, _Any]) -> None:
    """
    Upsert a single alert row returned by Kite into 'alerts' table (mirror fields).
    """
    sql = """
    INSERT INTO alerts (
        uuid, user_id, name, status, alert_type,
        lhs_exchange, lhs_tradingsymbol, lhs_attribute,
        operator, rhs_type, rhs_constant, rhs_exchange, rhs_tradingsymbol, rhs_attribute,
        basket, alert_count, updated_at
    ) VALUES (
        :uuid, :user_id, :name, :status, :alert_type,
        :lhs_exchange, :lhs_tradingsymbol, :lhs_attribute,
        :operator, :rhs_type, :rhs_constant, :rhs_exchange, :rhs_tradingsymbol, :rhs_attribute,
        :basket, :alert_count, NOW()
    )
    ON CONFLICT (uuid) DO UPDATE SET
        user_id = EXCLUDED.user_id,
        name = EXCLUDED.name,
        status = EXCLUDED.status,
        alert_type = EXCLUDED.alert_type,
        lhs_exchange = EXCLUDED.lhs_exchange,
        lhs_tradingsymbol = EXCLUDED.lhs_tradingsymbol,
        lhs_attribute = EXCLUDED.lhs_attribute,
        operator = EXCLUDED.operator,
        rhs_type = EXCLUDED.rhs_type,
        rhs_constant = EXCLUDED.rhs_constant,
        rhs_exchange = EXCLUDED.rhs_exchange,
        rhs_tradingsymbol = EXCLUDED.rhs_tradingsymbol,
        rhs_attribute = EXCLUDED.rhs_attribute,
        basket = EXCLUDED.basket,
        alert_count = EXCLUDED.alert_count,
        updated_at = NOW();
    """
    values = {
        "uuid": row.get("uuid"),
        "user_id": row.get("user_id", "me"),
        "name": row.get("name"),
        "status": row.get("status"),
        "alert_type": row.get("type"),
        "lhs_exchange": row.get("lhs_exchange"),
        "lhs_tradingsymbol": row.get("lhs_tradingsymbol"),
        "lhs_attribute": row.get("lhs_attribute"),
        "operator": row.get("operator"),
        "rhs_type": row.get("rhs_type"),
        "rhs_constant": row.get("rhs_constant"),
        "rhs_exchange": row.get("rhs_exchange"),
        "rhs_tradingsymbol": row.get("rhs_tradingsymbol"),
        "rhs_attribute": row.get("rhs_attribute"),
        "basket": json.dumps(row.get("basket")) if row.get("basket") is not None else None,
        "alert_count": int(row.get("alert_count") or 0),
    }
    await database.execute(sql, values)

async def _publish_ntfy_alert(title: str, message: str, tags: Optional[List[str]] = None) -> None:
    url = _alerts_ntfy_url()
    headers = {"Title": title}
    if tags:
        headers["Tags"] = ",".join(tags)
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(url, content=message, headers=headers)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"[NTFY] publish failed: {e}")

@alerts_router.post("")
async def create_alert(
    payload: Dict[str, Any] = Body(...),
    kite: KiteConnect = Depends(get_kite),
):
    """
    Create a simple Kite alert. Accepts JSON body with fields similar to Kite Alerts API.
    Minimal required for simple alert:
      - name, lhs_exchange, lhs_tradingsymbol, lhs_attribute='LastTradedPrice', operator, rhs_type='constant', rhs_constant, type='simple'
    """
    api_key = API_KEY
    access_token = getattr(kite, "access_token", None)
    if not access_token:
        raise HTTPException(401, "No access token available")
    # Prepare Kite form data with sensible defaults
    form = {
        "name": payload.get("name"),
        "lhs_exchange": payload.get("lhs_exchange"),
        "lhs_tradingsymbol": payload.get("lhs_tradingsymbol"),
        "lhs_attribute": payload.get("lhs_attribute", "LastTradedPrice"),
        "operator": payload.get("operator"),
        "rhs_type": payload.get("rhs_type", "constant"),
        "type": payload.get("type", "simple"),
    }
    if form["rhs_type"] == "constant":
        form["rhs_constant"] = payload.get("rhs_constant")
    # Optional ATO basket as JSON string
    if form["type"] == "ato" and payload.get("basket") is not None:
        form["basket"] = json.dumps(payload["basket"])
    # Validate minimal required
    missing = [k for k in ["name","lhs_exchange","lhs_tradingsymbol","operator"] if not form.get(k)]
    if form["rhs_type"] == "constant" and form.get("rhs_constant") is None:
        missing.append("rhs_constant")
    if missing:
        raise HTTPException(400, f"Missing fields: {', '.join(missing)}")
    url = "https://api.kite.trade/alerts"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=_kite_alerts_headers(api_key, access_token), data=form)
        try:
            r.raise_for_status()
        except Exception:
            raise HTTPException(r.status_code, r.text)
        resp = r.json()
        data = resp.get("data") or {}
        await _alerts_upsert_db(data)
        return data

@alerts_router.get("")
async def list_alerts(kite: KiteConnect = Depends(get_kite), refresh: bool = Query(False)):
    """
    List alerts from mirror. If refresh=true, first fetch from Kite and upsert mirror.
    """
    api_key = API_KEY
    access_token = getattr(kite, "access_token", None)
    if refresh and access_token:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get("https://api.kite.trade/alerts", headers=_kite_alerts_headers(api_key, access_token))
                r.raise_for_status()
                data = (r.json() or {}).get("data") or []
                for a in data:
                    await _alerts_upsert_db(a)
        except Exception as e:
            logger.error(f"[ALERTS] refresh failed: {e}")
    rows = await database.fetch_all("SELECT * FROM alerts ORDER BY updated_at DESC LIMIT 500")
    def _row_to_dict(r):
        d = dict(r)
        # decode json fields if string
        if d.get("basket") and isinstance(d["basket"], str):
            try:
                d["basket"] = json.loads(d["basket"])
            except Exception:
                pass
        return d
    return {"data": [_row_to_dict(r) for r in rows]}

@alerts_router.get("/{uuid}")
async def get_alert(uuid: str, kite: KiteConnect = Depends(get_kite), refresh: bool = Query(False)):
    api_key = API_KEY
    access_token = getattr(kite, "access_token", None)
    if refresh and access_token:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.kite.trade/alerts/{uuid}", headers=_kite_alerts_headers(api_key, access_token))
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                await _alerts_upsert_db(data)
    row = await database.fetch_one("SELECT * FROM alerts WHERE uuid = :u", {"u": uuid})
    if not row:
        raise HTTPException(404, "Alert not found")
    d = dict(row)
    if d.get("basket") and isinstance(d["basket"], str):
        try:
            d["basket"] = json.loads(d["basket"])
        except Exception:
            pass
    return d

@alerts_router.put("/{uuid}")
async def modify_alert(uuid: str, payload: Dict[str, Any] = Body(...), kite: KiteConnect = Depends(get_kite)):
    api_key = API_KEY
    access_token = getattr(kite, "access_token", None)
    if not access_token:
        raise HTTPException(401, "No access token available")
    # Kite expects form fields similar to create; pass through supported fields.
    allowed = {
        "name","lhs_exchange","lhs_tradingsymbol","lhs_attribute","operator",
        "rhs_type","rhs_constant","type","basket"
    }
    form = {k: v for k, v in payload.items() if k in allowed and v is not None}
    if "basket" in form and isinstance(form["basket"], (dict, list)):
        form["basket"] = json.dumps(form["basket"])
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.put(f"https://api.kite.trade/alerts/{uuid}", headers=_kite_alerts_headers(api_key, access_token), data=form)
        try:
            r.raise_for_status()
        except Exception:
            raise HTTPException(r.status_code, r.text)
        data = (r.json() or {}).get("data") or {}
        await _alerts_upsert_db(data)
        return data

@alerts_router.delete("/{uuid}")
async def delete_alert(uuid: str, kite: KiteConnect = Depends(get_kite)):
    api_key = API_KEY
    access_token = getattr(kite, "access_token", None)
    if not access_token:
        raise HTTPException(401, "No access token available")
    url = f"https://api.kite.trade/alerts?uuid={uuid}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.delete(url, headers=_kite_alerts_headers(api_key, access_token))
        try:
            r.raise_for_status()
        except Exception:
            raise HTTPException(r.status_code, r.text)
    # Remove from mirror
    await database.execute("DELETE FROM alerts WHERE uuid = :u", {"u": uuid})
    return {"status": "success"}

@alerts_router.get("/{uuid}/history")
async def alert_history(uuid: str, kite: KiteConnect = Depends(get_kite), refresh: bool = Query(False), limit: int = Query(50, ge=1, le=500)):
    api_key = API_KEY
    access_token = getattr(kite, "access_token", None)
    if refresh and access_token:
        # Fetch and append latest history
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"https://api.kite.trade/alerts/{uuid}/history", headers=_kite_alerts_headers(api_key, access_token))
            if r.status_code == 200:
                arr = (r.json() or {}).get("data") or []
                # Insert without strict dedupe (poller/dispatcher handle dedupe for notifications)
                for h in arr:
                    ts = h.get("created_at") or h.get("timestamp")
                    last_price = 0.0
                    meta = h.get("meta")
                    if isinstance(meta, list) and meta:
                        last_price = float(meta[0].get("last_price") or 0.0)
                    try:
                        await database.execute(
                            "INSERT INTO alert_history (alert_uuid, triggered_at, trigger_price, meta) VALUES (:u, :ts, :p, :m)",
                            {"u": uuid, "ts": ts, "p": last_price, "m": json.dumps(h)}
                        )
                    except Exception:
                        # Ignore duplicates or parsing issues
                        pass
    rows = await database.fetch_all(
        "SELECT id, alert_uuid, triggered_at, trigger_price, meta FROM alert_history WHERE alert_uuid = :u ORDER BY triggered_at DESC LIMIT :n",
        {"u": uuid, "n": limit}
    )
    return {"data": [dict(r) for r in rows]}

@alerts_router.post("/validate")
async def validate_alert(payload: Dict[str, Any] = Body(...)):
    """
    Validate alert parameters quickly against instruments DB.
    """
    lhs_exchange = payload.get("lhs_exchange")
    lhs_tradingsymbol = payload.get("lhs_tradingsymbol")
    operator = payload.get("operator")
    rhs_type = payload.get("rhs_type", "constant")
    rhs_constant = payload.get("rhs_constant")
    # Basic checks
    if not lhs_exchange or not lhs_tradingsymbol or not operator:
        return {"valid": False, "reason": "Missing symbol/operator"}
    # Ensure instrument exists
    row = await database.fetch_one(
        "SELECT instrument_token FROM kite_instruments WHERE exchange = :ex AND tradingsymbol = :ts LIMIT 1",
        {"ex": lhs_exchange, "ts": lhs_tradingsymbol}
    )
    if not row:
        return {"valid": False, "reason": "Instrument not found"}
    # rhs check
    if rhs_type == "constant" and rhs_constant is None:
        return {"valid": False, "reason": "rhs_constant required for rhs_type=constant"}
    # operator whitelist
    allowed_ops = {">=", "<=", ">", "<", "==", "!="}
    if operator not in allowed_ops:
        return {"valid": False, "reason": f"Invalid operator; allowed: {', '.join(sorted(allowed_ops))}"}
    return {"valid": True}

@alerts_router.post("/test-notification")
async def alerts_test_notification_endpoint():
    await _publish_ntfy_alert("Test: Kite Alerts (broker)", "Hello from /broker/alerts/test-notification", tags=["test","alerts"])
    return {"status": "ok"}

# Include alerts_router into the main router
try:
    router.include_router(alerts_router)
except Exception as _e:
    # If router was not yet defined for some reason, define and include
    router = APIRouter()
    router.include_router(alerts_router)

# ─────────── Instruments helpers for Alerts UI ───────────

from pydantic import BaseModel as _BM
from typing import Optional as _Opt, List as _List, Dict as _Dict, Any as _Any

@router.get("/instruments/top-defaults")
async def instruments_top_defaults():
    """
    Curated Top defaults for instrument picker.
    Defaults: NIFTY 50, NIFTY BANK, SENSEX, FINNIFTY, NIFTY MIDCAP 100
    Returns minimal fields required by the picker.
    """
    names = ["NIFTY 50", "NIFTY BANK", "SENSEX", "FINNIFTY", "NIFTY MIDCAP 100"]

    # Build safe placeholders for two IN clauses (indices table + instruments fallback)
    ph_a = ", ".join([f":a{i}" for i in range(len(names))])
    ph_b = ", ".join([f":b{i}" for i in range(len(names))])
    params = {}
    for i, n in enumerate(names):
        params[f"a{i}"] = n
        params[f"b{i}"] = n

    sql = f"""
    WITH src AS (
        SELECT instrument_token, tradingsymbol, name, COALESCE(exchange, 'INDICES') AS exchange,
               instrument_type, segment
        FROM kite_indices
        WHERE tradingsymbol IN ({ph_a})
        UNION
        SELECT instrument_token, tradingsymbol, name, COALESCE(exchange, 'INDICES') AS exchange,
               instrument_type, segment
        FROM kite_instruments
        WHERE segment = 'INDICES' AND tradingsymbol IN ({ph_b})
    )
    SELECT DISTINCT ON (tradingsymbol)
           instrument_token, tradingsymbol, name, exchange, instrument_type, segment
    FROM src
    ORDER BY tradingsymbol;
    """
    rows = await database.fetch_all(sql, params)
    return {"data": [dict(r) for r in rows]}

class ResolveItem(_BM):
    exchange: _Opt[str] = None
    tradingsymbol: str

class ResolveRequest(_BM):
    items: _List[ResolveItem]

@router.post("/instruments/resolve")
async def instruments_resolve(req: ResolveRequest):
    """
    Resolve a list of {exchange, tradingsymbol} pairs (case-insensitive) to canonical rows.
    - If exchange is 'INDICES' or missing, resolve from kite_indices first, then fallback to instruments (segment='INDICES')
    - Otherwise resolve from kite_instruments filtered by exchange.
    Response: { data: [ {found: bool, instrument?} ] }
    """
    out: _List[_Dict[str, _Any]] = []
    for item in req.items:
        ex = (item.exchange or "").strip().upper()
        ts = item.tradingsymbol.strip()
        row = None

        if ex in ("", "INDICES"):
            # Try indices table
            row = await database.fetch_one(
                "SELECT instrument_token, tradingsymbol, name, 'INDICES' AS exchange, instrument_type, segment "
                "FROM kite_indices WHERE lower(tradingsymbol) = lower(:ts) LIMIT 1",
                {"ts": ts}
            )
            if not row:
                # Fallback to instruments where segment is INDICES
                row = await database.fetch_one(
                    "SELECT instrument_token, tradingsymbol, name, COALESCE(exchange, 'INDICES') AS exchange, instrument_type, segment "
                    "FROM kite_instruments WHERE segment = 'INDICES' AND lower(tradingsymbol) = lower(:ts) LIMIT 1",
                    {"ts": ts}
                )
        else:
            row = await database.fetch_one(
                "SELECT instrument_token, tradingsymbol, name, exchange, instrument_type, segment "
                "FROM kite_instruments WHERE upper(exchange) = :ex AND lower(tradingsymbol) = lower(:ts) LIMIT 1",
                {"ex": ex, "ts": ts}
            )

        if row:
            out.append({"found": True, "instrument": dict(row)})
        else:
            out.append({"found": False, "instrument": None, "reason": "Not found"})

    return {"data": out}
