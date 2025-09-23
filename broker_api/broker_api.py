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
import meilisearch # Added meilisearch
import re # Added for regex parsing
import calendar # Added for month mapping
from kiteconnect import KiteConnect # For LTP fetching
from database import SessionLocal # For DB session in LTP helper

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

from fastapi import APIRouter, Depends, Response, HTTPException, Request, Query

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
    Response,
    BackgroundTasks
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
    underlying = Column(String, index=True, nullable=True) # Underlying symbol for derivatives
    option_type = Column(String(2), nullable=True) # CE, PE, or NULL
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
    
    # Ensure Meilisearch index is set up
    try:
        ensure_instruments_index()
    except Exception as e:
        logger.error(f"Failed to ensure Meilisearch index on startup: {e}", exc_info=True)

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
# ─────────── Meilisearch client and index helpers ───────────
_meili_client = None  # preserved (unused) to keep imports/refs stable
_meili_client_cache: Dict[str, meilisearch.Client] = {}

def _meili_health_ok(client: "meilisearch.Client") -> bool:
    """
    Small helper to check Meilisearch health using available method names.
    Returns True when healthy, False otherwise.
    """
    try:
        if hasattr(client, "health"):
            h = client.health()
        else:
            h = client.get_health()
        if isinstance(h, dict):
            status = h.get("status")
            # newer SDKs: {"status": "available"}
            return str(status).lower() == "available"
        # older SDKs might return truthy
        return bool(h)
    except Exception:
        return False

def get_meili_client(admin: bool = False) -> meilisearch.Client:
    """
    Returns a Meilisearch client based on role, with robust URL/key fallback and caching.
    - Builds ordered URL list:
        1) MEILI_URL (if set)
        2) http://meilisearch:7700
        3) http://localhost:7700
        4) http://127.0.0.1:7700
    - Builds ordered key list:
        admin=True  -> [MEILI_MASTER_KEY, MEILI_SEARCH_API_KEY, MEILI_API_KEY, None]
        admin=False -> [MEILI_SEARCH_API_KEY, MEILI_API_KEY, MEILI_MASTER_KEY, None]
    - Tries URLs × keys; on first healthy client, caches per role and returns.
    - Raises RuntimeError if no combination works.
    """
    role = "admin" if admin else "search"
    # Return cached client if available and healthy
    cached = _meili_client_cache.get(role)
    if cached and _meili_health_ok(cached):
        return cached

    # URL candidates (dedup preserving order)
    urls_ordered: List[str] = []
    env_url = os.getenv("MEILI_URL")
    if env_url:
        urls_ordered.append(env_url)
    urls_ordered.extend([
        "http://meilisearch:7700",
        "http://localhost:7700",
        "http://127.0.0.1:7700",
    ])
    seen = set()
    urls: List[str] = []
    for u in urls_ordered:
        if u not in seen:
            urls.append(u)
            seen.add(u)

    # Key candidates as per role
    def _env(name: str) -> Optional[str]:
        v = os.getenv(name)
        return v if (v is not None and str(v).strip() != "") else None

    if admin:
        keys: List[Optional[str]] = [
            _env("MEILI_MASTER_KEY"),
            _env("MEILI_SEARCH_API_KEY"),
            _env("MEILI_API_KEY"),
            None,
        ]
    else:
        keys = [
            _env("MEILI_SEARCH_API_KEY"),
            _env("MEILI_API_KEY"),
            _env("MEILI_MASTER_KEY"),
            None,
        ]

    tried_urls: List[str] = []

    for url in urls:
        tried_urls.append(url)
        for key in keys:
            try:
                client = meilisearch.Client(url) if key is None else meilisearch.Client(url, key)
                if _meili_health_ok(client):
                    _meili_client_cache[role] = client
                    return client
            except Exception:
                # continue trying other combinations
                continue

    # If all attempts failed, raise with summary of tried URLs
    summary = ", ".join(tried_urls)
    raise RuntimeError(f"Unable to connect to Meilisearch. Tried URLs (in order): {summary}")

def ensure_instruments_index():
    """Ensures the 'instruments' index exists and has the correct settings."""
    client = get_meili_client(admin=True)
    try:
        logger.info("Ensuring Meilisearch 'instruments' index exists and settings are applied...")
        index = client.index("instruments")
        index.fetch_info() # Check if index exists
    except meilisearch.errors.MeilisearchApiError as e:
        if e.code == 'index_not_found':
            logger.info("Meilisearch 'instruments' index not found, creating it.")
            task = client.create_index("instruments", {'primaryKey': 'id'})
            client.wait_for_task(task.task_uid)
            index = client.index("instruments")
        else:
            logger.error(f"Meilisearch API error when checking index: {e}", exc_info=True)
            return
    except Exception as e:
        logger.error(f"Unexpected error when checking Meilisearch index: {e}", exc_info=True)
        return

    settings = {
        "searchableAttributes": ["tradingsymbol", "aliases", "underlying", "name"],
        "rankingRules": ["typo","words","proximity","attribute","exactness","sort"],
        "customRanking": ["desc(boost_score)","asc(type_rank)","asc(expiry_ts)"],
        "filterableAttributes": [
            "underlying", "option_type", "exchange", "instrument_type", "segment",
            "expiry", "strike", "derivative_kind", "expiry_year", "expiry_month"
        ],
        "sortableAttributes": ["expiry", "expiry_ts", "strike", "type_rank", "boost_score"],
        "synonyms": {
            "nifty": ["NIFTY", "NIFTY 50", "NIFTY50"],
            "nifty50": ["NIFTY", "NIFTY 50", "NIFTY50"],
            "banknifty": ["BANKNIFTY", "NIFTY BANK", "BANK NIFTY"],
            "finnifty": ["FINNIFTY"],
            "sensex": ["SENSEX"],
            "midcap100": ["NIFTY MIDCAP 100"],
            "nifty bank": ["BANKNIFTY", "NIFTY BANK", "BANK NIFTY"],
            "crude": ["CRUDEOIL"],
            "crude oil": ["CRUDEOIL"]
        }
    }
    try:
        update_task = index.update_settings(settings)
        client.wait_for_task(update_task.task_uid)
        logger.info("Meilisearch 'instruments' index settings applied successfully.")
        try:
            effective_settings = index.get_settings()
            effective_sortables = effective_settings.get("sortableAttributes")
            logger.info(f"effective_sortables={effective_sortables}")
        except Exception as e:
            logger.warning(f"Could not fetch effective settings after update: {e}")
    except Exception as e:
        logger.error(f"Error applying Meilisearch index settings: {e}", exc_info=True)

# ─────────── Meilisearch reindex pipeline ───────────
async def meili_reindex_instruments():
    """
    Queries both kite_instruments and kite_indices tables, builds documents,
    and upserts them into the Meilisearch 'instruments' index.
    """
    # Ensure index settings are up-to-date before reindexing
    ensure_instruments_index()

    client = get_meili_client(admin=True)
    index = client.index("instruments")

    # SQL query to combine data from both tables
    sql_query = """
        SELECT
            instrument_token, exchange_token, tradingsymbol, name, last_price,
            expiry, strike, tick_size, lot_size, instrument_type, segment, exchange,
            underlying, option_type, last_updated
        FROM kite_instruments
        UNION ALL
        SELECT
            instrument_token, exchange_token, tradingsymbol, name, last_price,
            expiry, strike, tick_size, lot_size, instrument_type, segment, exchange,
            NULL AS underlying, NULL AS option_type, last_updated
        FROM kite_indices;
    """
    
    logger.info("Fetching instruments from PostgreSQL for Meilisearch reindexing...")
    db_records = await database.fetch_all(sql_query)
    
    documents = []
    
    # Month abbreviations to numbers mapping
    month_abbr_to_num = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if i}
    
    # Regex to extract underlying symbol from tradingsymbol for stock derivatives
    # Matches prefix before YYMON (e.g., RELIANCE25OCT) or YYM (e.g., RELIANCE25O)
    # This regex is designed to be non-greedy and capture the stock symbol part.
    # It looks for a pattern like 'DDMMM' or 'DDM' where D is digit, M is month char.
    # Example: RELIANCE25OCT2600CE -> RELIANCE
    # Example: NIFTY25OCT -> NIFTY
    # Example: TCS25O -> TCS
    underlying_symbol_regex = re.compile(r"^([A-Z0-9.&-]+?)(?:\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]?\d*|(?:\d{2}[JFMASOND][\dCEPE]*))", re.IGNORECASE)

    # Helper functions for Meilisearch document enrichment
    def _format_expiry_label(dt: Optional[date]) -> Optional[str]:
        if not dt: return None
        try:
            # Use datetime.strftime for consistent formatting, even if input is date
            return dt.strftime("%d-%b-%Y")
        except Exception:
            return None

    def _type_rank(doc: Dict[str, Any]) -> int:
        segment = str(doc.get("segment", "")).upper()
        instrument_type = str(doc.get("instrument_type", "")).upper()
        option_type = str(doc.get("option_type", "")).upper()

        if segment == "INDICES" or instrument_type == "INDEX": return 1
        if instrument_type == "FUT": return 2
        if instrument_type == "EQ" and not option_type: return 3
        if option_type in ("CE", "PE"): return 4
        return 9

    def _boost_and_aliases(underlying: str, tradingsymbol: str, name: Optional[str]) -> Tuple[int, List[str]]:
        u = (underlying or "").upper()
        base_aliases = []
        boost = 0

        if "BANKNIFTY" in u or "BANK" in u:
            boost = 100
            base_aliases.extend(["BANKNIFTY", "NIFTY BANK", "BANK NIFTY"])
        elif "FINNIFTY" in u:
            boost = 100
            base_aliases.extend(["FINNIFTY", "FIN NIFTY"])
        elif "SENSEX" in u:
            boost = 100
            base_aliases.extend(["SENSEX"])
        elif "NIFTY" in u:
            boost = 100
            base_aliases.extend(["NIFTY", "NIFTY50", "NIFTY 50"])
        
        # Add tradingsymbol and name to aliases, ensuring uniqueness
        all_aliases = set(base_aliases)
        if tradingsymbol:
            all_aliases.add(tradingsymbol.upper())
        if name:
            all_aliases.add(name.upper())
        
        return boost, list(all_aliases)

    for record in db_records:
        doc = {
            "id": str(record["instrument_token"]), # Meili primary key
            "instrument_token": record["instrument_token"],
            "exchange_token": record["exchange_token"],
            "tradingsymbol": record["tradingsymbol"],
            "name": record["name"],
            "last_price": float(record["last_price"]) if record["last_price"] is not None else None,
            "expiry": record["expiry"].isoformat() if record["expiry"] else None, # ISO date string
            "strike": float(record["strike"]) if record["strike"] is not None else None,
            "tick_size": float(record["tick_size"]) if record["tick_size"] is not None else None,
            "lot_size": int(record["lot_size"]) if record["lot_size"] is not None else None,
            "instrument_type": record["instrument_type"],
            "segment": record["segment"],
            "exchange": record["exchange"],
            "underlying": record["underlying"], # New field
            "option_type": record["option_type"], # New field
            "last_updated": record["last_updated"].isoformat() if record["last_updated"] else None, # ISO string
            # The following fields are for backward compatibility with existing fuzzy search logic
            "underlying_symbol": record["underlying"], # For existing fuzzy search
            "derivative_kind": "NONE", # Will be set below
            "expiry_ts": None, # Will be set below
            "expiry_year": None, # Will be set below
            "expiry_month": None, # Will be set below
            "expiry_label": None, # New field
            "type_rank": 9, # New field, default to 9
            "boost_score": 0, # New field, default to 0
            "aliases": [], # New field
        }

        instrument_type = record["instrument_type"]
        segment = record["segment"]
        tradingsymbol = record["tradingsymbol"]
        expiry_date = record["expiry"]
        strike_price = record["strike"]
 
        # Normalization for INDICES rows:
        # Ensure indices are represented with consistent fields for Meilisearch documents,
        # and derive a usable 'underlying' when missing to improve recall for base queries.
        seg_up = (segment or "").upper() if segment else None
        if seg_up == "INDICES":
            # Force canonical values for indices
            doc["instrument_type"] = "INDEX"
            doc["segment"] = "INDICES"
            doc["option_type"] = None
            doc["expiry"] = None
            doc["strike"] = None
            # Derive a simple underlying from tradingsymbol when absent
            up_ts = (tradingsymbol or "").upper()
            derived_underlying = None
            if "BANK" in up_ts:
                derived_underlying = "BANKNIFTY"
            elif "NIFTY" in up_ts:
                derived_underlying = "NIFTY"
            elif "SENSEX" in up_ts:
                derived_underlying = "SENSEX"
            elif "FINNIFTY" in up_ts:
                derived_underlying = "FINNIFTY"
            else:
                # Fallback: take first token and strip non-alphanumerics
                first_word = up_ts.split()[0] if up_ts.split() else up_ts
                cleaned = re.sub(r'[^A-Z0-9]', '', first_word)
                derived_underlying = cleaned if cleaned else up_ts
            doc["underlying"] = derived_underlying
            # Keep underlying_symbol in sync for compatibility
            doc["underlying_symbol"] = doc["underlying"]
            # reflect the normalized instrument_type for downstream logic
            instrument_type = doc["instrument_type"]
        else:
            # Keep instrument_type as read from DB for non-indices
            instrument_type = record["instrument_type"]
 
        # Determine derivative_kind
        if instrument_type in {"CE", "PE"}:
            doc["derivative_kind"] = "OPT"
        elif instrument_type == "FUT":
            doc["derivative_kind"] = "FUT"

        # Set expiry related fields
        if expiry_date:
            # Convert expiry date to UTC datetime at 00:00 and then to epoch seconds
            # Using pytz.utc to ensure it's timezone-aware
            expiry_utc = datetime.combine(expiry_date, datetime.min.time(), tzinfo=pytz.utc)
            doc["expiry_ts"] = int(expiry_utc.timestamp())
            doc["expiry_year"] = expiry_date.year
            doc["expiry_month"] = expiry_date.month
            doc["expiry_label"] = _format_expiry_label(expiry_date) # Assign expiry_label

        # Assign type_rank
        doc["type_rank"] = _type_rank(doc)

        # Assign boost_score and aliases
        underlying_for_aliases = doc.get("underlying") or tradingsymbol
        boost, alias_list = _boost_and_aliases(underlying_for_aliases, tradingsymbol, doc.get("name"))
        doc["boost_score"] = boost
        doc["aliases"] = list(set(alias_list)) # Ensure uniqueness

        documents.append(doc)

    total_documents = len(documents)
    if not total_documents:
        logger.info("No instruments to reindex in Meilisearch.")
        return {"total": 0, "batches": 0}

    batch_size = 5000 # Sensible default batch size
    batches = 0
    last_task_uid = None

    logger.info(f"Starting Meilisearch reindexing for {total_documents} instruments in batches of {batch_size}...")
    for i in range(0, total_documents, batch_size):
        batch = documents[i:i + batch_size]
        try:
            task = index.add_documents(batch, primary_key="id")
            last_task_uid = task.task_uid
            batches += 1
            logger.info(f"Sent batch {batches} to Meilisearch (task_uid: {last_task_uid}).")
        except Exception as e:
            logger.error(f"Error sending batch {batches} to Meilisearch: {e}", exc_info=True)
            # Continue with next batch or re-raise, depending on desired error handling
            # For now, we log and continue.

    if last_task_uid is not None:
        logger.info(f"Waiting for last Meilisearch indexing task ({last_task_uid}) to complete...")
        client.wait_for_task(last_task_uid)
        logger.info("Meilisearch reindexing completed.")
    else:
        logger.info("No documents were sent to Meilisearch for reindexing.")

    return {"total": total_documents, "batches": batches}





async def sync_and_reindex_orchestrator(
    session: Session,
    refresh_from_broker: bool,
    backfill_only_nulls: bool,
    reindex: bool,
    background_tasks: Optional[BackgroundTasks] = None
) -> Dict[str, Optional[int]]:
    """
    Orchestrates optional instrument refresh, backfill of underlying/option_type, and Meilisearch reindex.
    """
    refreshed_count: Optional[int] = None
    backfilled_counts: Dict[str, int] = {"processed": 0, "updated": 0, "skipped": 0}
    indexed_count: Optional[int] = None

    try:
        # 1. Refresh instruments from broker
        if refresh_from_broker:
            logger.info("Initiating instruments refresh from broker (orchestrator)...")
            # We need a KiteConnect instance for import_all_instruments.
            # For internal calls, we'll create a temporary one using the system token.
            _db = None
            try:
                _db = SessionLocal()
                access_token = get_system_access_token(_db)
                if not access_token:
                    logger.warning("No system access token found for instrument refresh. Skipping.")
                    refreshed_count = 0
                else:
                    kite_instance = KiteConnect(api_key=API_KEY)
                    kite_instance.set_access_token(access_token)
                    
                    # Call import_all_instruments directly
                    refresh_results = await import_all_instruments(kite_instance)
                    total_imported = 0
                    for res in refresh_results.get("results", []):
                        if "message" in res and "Imported" in res["message"]:
                            match = re.search(r"Imported (\d+) instruments", res["message"])
                            if match:
                                total_imported += int(match.group(1))
                    refreshed_count = total_imported
                    logger.info(f"Instruments refresh completed. Total imported: {refreshed_count}.")
            except Exception as e:
                logger.error(f"Error during instrument refresh in orchestrator: {e}", exc_info=True)
                refreshed_count = 0 # Indicate failure
            finally:
                if _db:
                    _db.close()
        else:
            logger.info("Instruments refresh skipped as per orchestrator request.")

        # 2. Backfill underlying and option_type
        logger.info("Initiating backfill for underlying and option_type (orchestrator)...")
        backfilled_counts = await _parse_and_backfill_underlying(session, only_nulls=backfill_only_nulls)
        logger.info(f"Backfill completed: Processed {backfilled_counts['processed']}, Updated {backfilled_counts['updated']}, Skipped {backfilled_counts['skipped']}.")

        # 3. Reindex Meilisearch
        if reindex:
            logger.info("Initiating Meilisearch reindex (orchestrator)...")
            reindex_stats = await meili_reindex_instruments()
            indexed_count = reindex_stats.get("total")
            logger.info(f"Meilisearch reindex completed. Total indexed: {indexed_count}.")
        else:
            logger.info("Meilisearch reindex skipped as per orchestrator request.")

        return {
            "refreshed": refreshed_count,
            "backfilled": backfilled_counts["processed"],
            "updated": backfilled_counts["updated"],
            "skipped": backfilled_counts["skipped"],
            "indexed": indexed_count
        }

    except Exception as e:
        logger.error(f"Error in sync-and-reindex orchestrator operation: {e}", exc_info=True)
        # Re-raise or handle as appropriate for a helper function
        raise e

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

@router.post("/instruments/meili/reindex")
async def trigger_meilisearch_reindex():
    """
    Triggers a full reindex of instruments into Meilisearch.
    """
    logger.info("Meilisearch reindex endpoint triggered.")
    try:
        stats = await meili_reindex_instruments()
        return {"status": "success", "message": "Meilisearch reindex initiated.", "stats": stats}
    except Exception as e:
        logger.error(f"Error triggering Meilisearch reindex: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to trigger Meilisearch reindex: {e}")

@router.get("/instruments/meili/health")
async def get_meilisearch_health():
    """
    Returns the health status of the Meilisearch service.
    """
    try:
        client = get_meili_client(admin=False)
        health = client.health()
        if health.get("status") == "available":
            return {"status": "ok"}
        else:
            return {"status": "error", "detail": health}
    except Exception as e:
        logger.error(f"Error checking Meilisearch health: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

async def _parse_and_backfill_underlying(session: Session, only_nulls: bool = True) -> Dict[str, int]:
    """
    Internal helper to populate 'underlying' and 'option_type' columns in 'kite_instruments'.
    
    Args:
        session: SQLAlchemy DB session.
        only_nulls: If True, only backfill records where 'underlying' is NULL.
                    If False, process all records.
                    
    Returns:
        A dictionary with counts: {"processed": int, "updated": int, "skipped": int}.
    """
    processed_count = 0
    updated_count = 0
    skipped_count = 0

    try:
        if only_nulls:
            instruments_to_process = session.query(KiteInstrument).filter(KiteInstrument.underlying == None).all()
            logger.info(f"Starting backfill for {len(instruments_to_process)} instruments where underlying is NULL.")
        else:
            instruments_to_process = session.query(KiteInstrument).all()
            logger.info(f"Starting full backfill for {len(instruments_to_process)} instruments.")
        
        # Regex to extract underlying symbol from tradingsymbol for stock derivatives
        underlying_symbol_regex = re.compile(r"^([A-Z0-9.&-]+?)(?:\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]?\d*|(?:\d{2}[JFMASOND][\dCEPE]*))", re.IGNORECASE)

        for instrument in instruments_to_process:
            processed_count += 1
            tradingsymbol = instrument.tradingsymbol
            current_underlying = instrument.underlying
            current_option_type = instrument.option_type

            new_underlying = None
            new_option_type = None

            # Handle Equity (underlying is tradingsymbol, no option type)
            if instrument.instrument_type == "EQ":
                new_underlying = tradingsymbol
                new_option_type = None
            # Handle Futures
            elif instrument.instrument_type == "FUT":
                match = underlying_symbol_regex.match(tradingsymbol)
                if match:
                    new_underlying = match.group(1)
                else:
                    first_digit_idx = re.search(r"\d", tradingsymbol)
                    if first_digit_idx:
                        new_underlying = tradingsymbol[:first_digit_idx.start()]
                    else:
                        new_underlying = tradingsymbol
                new_option_type = None
            # Handle Options (CE/PE)
            elif instrument.instrument_type in {"CE", "PE"}:
                match = underlying_symbol_regex.match(tradingsymbol)
                if match:
                    new_underlying = match.group(1)
                else:
                    first_digit_idx = re.search(r"\d", tradingsymbol)
                    if first_digit_idx:
                        new_underlying = tradingsymbol[:first_digit_idx.start()]
                    else:
                        new_underlying = tradingsymbol
                new_option_type = instrument.instrument_type
            
            # Only update if values have changed or are newly determined
            if (new_underlying and new_underlying.upper() != current_underlying) or \
               (new_option_type != current_option_type):
                instrument.underlying = new_underlying.upper() if new_underlying else None
                instrument.option_type = new_option_type
                updated_count += 1
            else:
                skipped_count += 1
        
        session.commit()
        logger.info(f"Backfill completed: Processed {processed_count}, Updated {updated_count}, Skipped {skipped_count} instruments.")
        return {"processed": processed_count, "updated": updated_count, "skipped": skipped_count}

    except Exception as e:
        session.rollback()
        logger.error(f"Error during underlying and option_type backfill: {e}", exc_info=True)
        raise e # Re-raise to be handled by the calling endpoint

@router.post("/broker/instruments/populate-underlying")
async def populate_underlying_and_option_type(db: Session = Depends(get_db)):
    """
    [DEPRECATED] Populates the 'underlying' and 'option_type' columns in the 'kite_instruments' table
    for records where 'underlying' is NULL. Designed for a one-time data backfill.
    Please use /broker/instruments/sync-and-reindex for unified maintenance operations.
    """
    logger.info("Deprecated /broker/instruments/populate-underlying endpoint called. Redirecting to helper.")
    try:
        counts = await _parse_and_backfill_underlying(db, only_nulls=True)
        return {"message": "Underlying and option_type populated successfully", **counts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error populating underlying and option_type: {e}")

async def sql_fallback_fuzzy_search(query: str, limit: int = 50, parsed: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    SQL-based fuzzy search using structured predicates if provided.
    Also applies LIKE on name/tradingsymbol as a safety net.
    """
    if not (query or "").strip():
        return []

    params = {"limit": limit}
    # Safety net LIKEs
    base_like = ["(tradingsymbol ILIKE :contains OR name ILIKE :contains)"]
    params["contains"] = f"%{query}%"

    where_conditions = list(base_like)

    if parsed:
        if parsed.get("underlying"):
            where_conditions.append("underlying = :underlying")
            params["underlying"] = parsed["underlying"]
        if parsed.get("option_type"):
            where_conditions.append("option_type = :option_type")
            params["option_type"] = parsed["option_type"]
        if parsed.get("instrument_type"):
            where_conditions.append("instrument_type = :instrument_type")
            params["instrument_type"] = parsed["instrument_type"]
        if parsed.get("exchange"):
            where_conditions.append("exchange = :exchange")
            params["exchange"] = parsed["exchange"]
        if parsed.get("strike") is not None:
            where_conditions.append("strike = :strike")
            params["strike"] = parsed["strike"]
        if parsed.get("expiry_date"):
            where_conditions.append("expiry = :expiry_date")
            params["expiry_date"] = parsed["expiry_date"]
        elif parsed.get("expiry_year") and parsed.get("expiry_month"):
            start, end = month_window(parsed["expiry_year"], parsed["expiry_month"])
            where_conditions.append("expiry >= :start_date AND expiry < :end_date")
            params["start_date"] = start
            params["end_date"] = end

    where_clause = " AND ".join(where_conditions)

    sql = f"""
        SELECT
            instrument_token, exchange_token, tradingsymbol, name, last_price,
            expiry, strike, tick_size, lot_size, instrument_type, segment,
            exchange, underlying, option_type
        FROM (
          SELECT
            instrument_token,
            exchange_token,
            tradingsymbol,
            name,
            last_price,
            expiry,
            strike,
            tick_size,
            lot_size,
            instrument_type,
            segment,
            exchange,
            underlying,
            option_type
          FROM public.kite_instruments
          UNION ALL
          SELECT
            instrument_token,
            exchange_token,
            tradingsymbol,
            name,
            last_price,
            expiry,
            strike,
            tick_size,
            lot_size,
            instrument_type,
            segment,
            exchange,
            NULL::VARCHAR(255) AS underlying,
            NULL::VARCHAR(10) AS option_type
          FROM public.kite_indices
        ) AS instruments_search_v
        WHERE {where_clause}
        ORDER BY
            CASE
                WHEN tradingsymbol ILIKE :exact_q THEN 1
                WHEN tradingsymbol ILIKE :prefix_q THEN 2
                ELSE 3
            END,
            expiry,
            strike
        LIMIT :limit
    """
    params["exact_q"] = query
    params["prefix_q"] = f"{query}%"

    rows = await database.fetch_all(sql, params)
    return [dict(row) for row in rows]

async def sql_fallback_plain(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Plain SQL fallback for zero-hit Meili responses on unstructured queries.
    - Prefix match on tradingsymbol OR contains match on name.
    - Order by LENGTH(tradingsymbol) ASC to prioritize tight symbol matches.
    """
    q_text = (query or "").strip()
    if not q_text:
        return []

    params = {
        "limit": limit,
        "prefix": f"{q_text}%",
        "contains": f"%{q_text}%"
    }

    sql = """
        SELECT
            instrument_token, exchange_token, tradingsymbol, name, last_price,
            expiry, strike, tick_size, lot_size, instrument_type, segment,
            exchange, underlying, option_type
        FROM (
          SELECT
            instrument_token,
            exchange_token,
            tradingsymbol,
            name,
            last_price,
            expiry,
            strike,
            tick_size,
            lot_size,
            instrument_type,
            segment,
            exchange,
            underlying,
            option_type
          FROM public.kite_instruments
          UNION ALL
          SELECT
            instrument_token,
            exchange_token,
            tradingsymbol,
            name,
            last_price,
            expiry,
            strike,
            tick_size,
            lot_size,
            instrument_type,
            segment,
            exchange,
            NULL::VARCHAR(255) AS underlying,
            NULL::VARCHAR(10) AS option_type
          FROM public.kite_indices
        ) AS instruments_search_v
        WHERE tradingsymbol ILIKE :prefix OR name ILIKE :contains
        ORDER BY LENGTH(tradingsymbol) ASC
        LIMIT :limit
    """
    rows = await database.fetch_all(sql, params)
    return [dict(r) for r in rows]


async def get_anchor_price_for_underlying(underlying_symbol: str) -> Optional[float]:
    """
    For major indices, fetches the last traded price (LTP) to use as an anchor for strike sorting.
    Uses the system KiteConnect session. Returns None on any failure.
    """
    if not underlying_symbol:
        return None

    index_map = {
        "NIFTY": "NIFTY 50",
        "BANKNIFTY": "NIFTY BANK",
        "FINNIFTY": "FINNIFTY",
        "SENSEX": "SENSEX",
    }
    
    index_tradingsymbol = index_map.get(underlying_symbol.upper())
    if not index_tradingsymbol:
        return None

    db = None
    try:
        db = SessionLocal()
        access_token = get_system_access_token(db)
        if not access_token:
            logger.warning(f"No system access token found for LTP fetch of {underlying_symbol}")
            return None

        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(access_token)
        
        # Set a short timeout to avoid blocking the search request for too long
        kite.set_timeout(5)

        instrument = f"INDICES:{index_tradingsymbol}"
        ltp_data = kite.ltp([instrument])
        
        if ltp_data and instrument in ltp_data and "last_price" in ltp_data[instrument]:
            price = ltp_data[instrument]["last_price"]
            logger.info(f"Fetched anchor price for {underlying_symbol}: {price}")
            return float(price)
        else:
            logger.warning(f"LTP data not found for {instrument}")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch anchor price for {underlying_symbol}: {e}", exc_info=True)
        return None
    finally:
        if db:
            db.close()


class SyncAndReindexRequest(BaseModel):
    refresh_from_broker: bool = False
    backfill_only_nulls: bool = True
    reindex: bool = True

    # refresh_from_broker=True calls an internal import/refresh function (e.g., import_all_instruments) directly if present;
    # it does not call any HTTP endpoint. If no internal refresh function exists, this endpoint still backfills
    # underlying/option_type for current DB records and reindexes Meilisearch.
@router.post("/broker/instruments/sync-and-reindex")
async def sync_and_reindex_instruments(
    request: SyncAndReindexRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    # kite: KiteConnect = Depends(get_kite) # KiteConnect instance is handled internally by orchestrator for refresh
):
    """
    Orchestrates optional instrument refresh, backfill of underlying/option_type, and Meilisearch reindex.
    """
    try:
        # Delegate to the centralized orchestrator
        results = await sync_and_reindex_orchestrator(
            session=db,
            refresh_from_broker=request.refresh_from_broker,
            backfill_only_nulls=request.backfill_only_nulls,
            reindex=request.reindex,
            background_tasks=background_tasks # Pass background_tasks if needed for future async operations
        )
        return results
    except Exception as e:
        logger.error(f"Error in unified sync-and-reindex endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Sync and reindex operation failed: {e}")

@router.get("/instruments/fuzzy-search")
async def fuzzy_search_instruments(
    q: Optional[str] = Query(None, alias="q"),
    query: Optional[str] = Query(None, alias="query"),
    limit: int = 50
):
    def _sanitize_sort(index, sort_list: list[str]) -> list[str]:
        try:
            settings = index.get_settings()
            allowed = set(settings.get("sortableAttributes") or [])
            sanitized = [s for s in (sort_list or []) if (s.split(":")[0] in allowed)]
            if len(sanitized) != len(sort_list or []):
                logger.info(f"sanitized_sort={sanitized} allowed={allowed}")
            return sanitized
        except Exception as e:
            logger.exception("Failed to sanitize sort, returning empty list.")
            return []  # fallback: no sort
    """
    Fuzzy search endpoint with Meilisearch-first and robust SQL fallback.
    Changes:
    - Accepts both 'q' and 'query'.
    - Short query guard (<=3): skip parsing; plain Meili search with no filter.
    - Always fallback to SQL when Meili returns zero hits.
    - Always return 200 with a list (possibly empty).
    """
    q_text = (q or query or "").strip()
    if not q_text:
        return []

    try:
        client = get_meili_client(admin=False)
        index = client.index("instruments")
    except Exception:
        logger.exception(f"Failed to init Meili client for q='{q_text}'. Falling back to SQL (plain). mode=sql_fallback_plain")
        rows = await sql_fallback_plain(q_text, limit)
        logger.info(f"q='{q_text}' mode=sql_fallback_plain sql_rows={len(rows)}")
        return rows

    # Short query guard: <= 3 characters -> skip structured parsing entirely
    if len(q_text) <= 3:
        try:
            options = {
                "limit": limit,
                "sort": ["boost_score:desc","type_rank:asc","expiry_ts:asc"],
                "attributesToRetrieve": [
                    'instrument_token', 'exchange_token', 'tradingsymbol', 'name',
                    'last_price', 'expiry', 'strike', 'tick_size', 'lot_size',
                    'instrument_type', 'segment', 'exchange', 'underlying', 'option_type'
                ]
            }
            # Pre-sanitize sort against current index settings to avoid invalid_search_sort
            try:
                sanitized = _sanitize_sort(index, options.get("sort"))
                if not sanitized:
                    options.pop("sort", None)
                else:
                    options["sort"] = sanitized
            except Exception:
                # On any settings error, drop sort to avoid invalid_search_sort
                options.pop("sort", None)
            try:
                result = index.search(q_text, options)
            except meilisearch.errors.MeilisearchApiError as e:
                msg = str(e)
                if "invalid_search_sort" in msg or "not sortable" in msg:
                    logger.warning(f"Meili search failed with invalid sort for q='{q_text}'. Sanitizing and retrying.")
                    options["sort"] = _sanitize_sort(index, options.get("sort"))
                    try:
                        result = index.search(q_text, options)
                    except Exception:
                        logger.error(f"Meili retry failed for q='{q_text}' after sanitizing. Retrying without sort.")
                        options.pop("sort", None)
                        result = index.search(q_text, options)
                elif "invalid_search_filter" in msg or "not filterable" in msg:
                    logger.warning(f"Meili search failed with invalid filter settings for q='{q_text}'. Resetting index settings and retrying.")
                    try:
                        # Attempt self-heal: re-apply index settings
                        ensure_instruments_index()
                        # Reacquire index handle and retry with same options
                        index = client.index("instruments")
                        result = index.search(q_text, options)
                    except Exception:
                        # Final attempt: drop any filters/sorts and try plain search
                        logger.error(f"Meili retry failed for q='{q_text}' after resetting settings. Retrying without filter/sort.")
                        options.pop("filter", None)
                        options.pop("sort", None)
                        result = index.search(q_text, options)
                else:
                    raise
            hits = result.get("hits", [])
            logger.info(f"q='{q_text}' mode=meili_q_only meili_hits={len(hits)}")
            if not hits:
                rows = await sql_fallback_plain(q_text, limit)
                logger.info(f"q='{q_text}' mode=sql_fallback_plain sql_rows={len(rows)}")
                return rows
            return hits
        except Exception:
            logger.exception(f"Meili error for short query q='{q_text}'. Falling back to SQL (plain). mode=sql_fallback_plain")
            rows = await sql_fallback_plain(q_text, limit)
            logger.info(f"q='{q_text}' mode=sql_fallback_plain sql_rows={len(rows)}")
            return rows

    # Longer queries: try to parse for structured filters
    parsed = {}
    try:
        parsed = parse_fo_query(q_text)
        logger.info(f"q='{q_text}' parsed={json.dumps(parsed, default=str)}")
    except Exception:
        logger.exception(f"Parser error for q='{q_text}'. Proceeding without filters.")

    options = {
        "limit": limit,
        "attributesToRetrieve": [
            'instrument_token', 'exchange_token', 'tradingsymbol', 'name',
            'last_price', 'expiry', 'strike', 'tick_size', 'lot_size',
            'instrument_type', 'segment', 'exchange', 'underlying', 'option_type'
        ]
    }
    
    filter_clauses = []

    # Determine if the user explicitly asked for derivatives
    explicit_derivative = bool(
        parsed.get("option_type")
        or parsed.get("derivative_kind")
        or parsed.get("expiry_date")
        or (parsed.get("expiry_year") and parsed.get("expiry_month"))
        or (parsed.get("strike") is not None)
        or ("FUT" in (parsed.get("instrument_type") or ""))
    )

    # For base queries (no explicit derivatives), exclude options so index/equity/futures surface
    if not explicit_derivative:
        filter_clauses.append("option_type IS NULL")
        # Prefer major indices and futures first for base queries
        options["sort"] = ["boost_score:desc","type_rank:asc","expiry_ts:asc"]

    # Numeric strike without CE/PE => both legs in a band, sorted by expiry then strike
    strike = parsed.get("strike")
    if (strike is not None) and not parsed.get("option_type"):
        # If a strike is provided without CE/PE, we should look for options, not exclude them.
        # So, we remove the "option_type IS NULL" filter if it was added.
        if "option_type IS NULL" in filter_clauses:
            filter_clauses.remove("option_type IS NULL")
        filter_clauses.append('(option_type = "CE" OR option_type = "PE")')
        tol = 50
        filter_clauses.append(f"(strike >= {int(strike - tol)} AND strike <= {int(strike + tol)})")
        # Prefer expiry_ts for sorting if available
        options["sort"] = ["expiry_ts:asc", "strike:asc"]

    # Add other parsed filters
    # This reuses the existing build_meili_filter logic but integrates it into the new clause system
    if parsed:
        # We handle strike and option_type manually above, so we can create a temporary parsed dict without them
        # to avoid double-filtering.
        temp_parsed = parsed.copy()
        temp_parsed.pop("strike", None)
        # We don't pop option_type because if it's present, it should be used.
        # The logic above for strike handling only applies when option_type is NOT specified.
        
        # The original build_meili_filter is fine to reuse for other attributes
        additional_filters = build_meili_filter(temp_parsed)
        if additional_filters:
            filter_clauses.extend(additional_filters)

    filter_str = " AND ".join(filter_clauses) if filter_clauses else None
    resid = parsed.get("residual") if parsed else None
    search_q = resid or q_text

    try:
        if filter_str:
            options["filter"] = filter_str

        # Pre-sanitize sort against index settings to avoid invalid_search_sort
        if "sort" in options:
            try:
                sanitized = _sanitize_sort(index, options.get("sort"))
                if not sanitized:
                    options.pop("sort", None)
                else:
                    options["sort"] = sanitized
            except Exception:
                options.pop("sort", None)
        
        try:
            result = index.search(search_q, options)
        except meilisearch.errors.MeilisearchApiError as e:
            msg = str(e)
            if "invalid_search_sort" in msg or "not sortable" in msg:
                logger.warning(f"Meili search failed with invalid sort for q='{q_text}'. Sanitizing and retrying.")
                options["sort"] = _sanitize_sort(index, options.get("sort"))
                try:
                    result = index.search(search_q, options)
                except Exception:
                    logger.error(f"Meili retry failed for q='{q_text}' after sanitizing. Retrying without sort.")
                    options.pop("sort", None)
                    result = index.search(search_q, options)
            elif "invalid_search_filter" in msg or "not filterable" in msg:
                logger.warning(f"Meili search failed with invalid filter for q='{q_text}' filter='{filter_str}'. Attempting index settings reset.")
                try:
                    # Self-heal: ensure index has correct filterableAttributes, wait for task, then retry
                    ensure_instruments_index()
                    index = client.index("instruments")
                    result = index.search(search_q, options)
                except Exception:
                    logger.error(f"Meili retry failed for q='{q_text}' after resetting settings. Retrying without filter.")
                    # Drop the filter and retry as plain query
                    options.pop("filter", None)
                    try:
                        result = index.search(search_q, options)
                    except Exception:
                        # Give up on Meili path; let caller fallback to SQL
                        raise
            else:
                raise
        hits = result.get("hits", [])
        mode = "meili_filtered" if filter_str else "meili_q_only"
        logger.info(f"q='{q_text}' mode={mode} meili_hits={len(hits)}")

        if not hits:
            # Always fallback to SQL whenever Meili hits are zero
            if filter_str:
                rows = await sql_fallback_fuzzy_search(q_text, limit, parsed)
                logger.info(f"q='{q_text}' mode=sql_fallback_structured sql_rows={len(rows)}")
                return rows
            else:
                rows = await sql_fallback_plain(q_text, limit)
                logger.info(f"q='{q_text}' mode=sql_fallback_plain sql_rows={len(rows)}")
                return rows

        return hits
    except (meilisearch.errors.MeilisearchCommunicationError, requests.exceptions.ConnectionError, httpx.ConnectError):
        logger.exception(f"Meili connection error for q='{q_text}'. Falling back to SQL (prefer structured if available).")
        if filter_str:
            rows = await sql_fallback_fuzzy_search(q_text, limit, parsed)
            logger.info(f"q='{q_text}' mode=sql_fallback_structured sql_rows={len(rows)}")
        else:
            rows = await sql_fallback_plain(q_text, limit)
            logger.info(f"q='{q_text}' mode=sql_fallback_plain sql_rows={len(rows)}")
        return rows
    except Exception:
        logger.exception(f"Unexpected Meili error for q='{q_text}'. Falling back to SQL (plain).")
        rows = await sql_fallback_plain(q_text, limit)
        logger.info(f"q='{q_text}' mode=sql_fallback_plain sql_rows={len(rows)}")
        return rows

# ─────────── Daily update functionality ───────────
async def schedule_daily_instruments_update():
    """Schedules the daily instruments maintenance orchestrator."""
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

            logger.info(f"Next daily instruments maintenance orchestrator run scheduled for {next_run_ist.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
            await asyncio.sleep(delay)

            # Run the daily maintenance task.
            # The task itself handles logging and notifications for its own success/failure.
            await update_all_instruments_daily()

        except Exception as e:
            logger.error(f"Error in daily maintenance scheduler loop: {e}", exc_info=True)
            await send_ntfy_notification(f"Daily maintenance scheduler failed: {e}", title="Scheduler Failure", tags=["failure", "instruments", "scheduler"])
            # Wait for 1 hour before retrying the scheduler logic
            await asyncio.sleep(60 * 60)

async def update_all_instruments_daily():
    """
    Runs unified instruments maintenance: optional refresh from broker, backfill underlying/option_type, and Meilisearch reindex via sync_and_reindex_orchestrator().
    """
    logger.info("Daily instruments maintenance job started.")
    db = None
    try:
        db = SessionLocal()
        # Invoke the unified orchestrator for daily maintenance.
        # This handles refreshing from broker, backfilling data, and reindexing.
        counts = await sync_and_reindex_orchestrator(
            session=db,
            refresh_from_broker=True,
            backfill_only_nulls=True,
            reindex=True,
            background_tasks=None
        )
        logger.info(f"Daily instruments maintenance completed successfully. Counts: {counts}")
        await send_ntfy_notification(
            f"Daily instrument maintenance finished. Details: {counts}",
            title="Scheduler Success",
            tags=["success", "instruments"]
        )
    except Exception as e:
        logger.error(f"Error during daily instruments maintenance: {e}", exc_info=True)
        await send_ntfy_notification(
            f"Daily instrument maintenance failed: {e}",
            title="Scheduler Failure",
            tags=["failure", "instruments"]
        )
    finally:
        if db:
            db.close()

def month_window(year: int, month: int) -> tuple[date, date]:
    """Computes the first day of a month and the first day of the next month."""
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)
    return start_date, end_date

def build_meili_filter(parsed: dict) -> list[str]:
    """
    Safe Meilisearch filter builder:
    - Only string equality with double quotes for: underlying, option_type, instrument_type, exchange.
    - Only numeric equality for strike.
    - Expiry: equality only (ISO date string) or skip.
    - No ranges, no IN clauses.
    """
    preds: list[str] = []

    # String fields: equality only, double quotes
    if parsed.get("underlying"):
        preds.append(f'underlying = "{parsed["underlying"]}"')

    if parsed.get("instrument_type"):
        preds.append(f'instrument_type = "{parsed["instrument_type"]}"')

    if parsed.get("option_type"):
        preds.append(f'option_type = "{parsed["option_type"]}"')

    if parsed.get("exchange"):
        preds.append(f'exchange = "{parsed["exchange"]}"')

    # Expiry equality only (ISO format) or skip on error
    if parsed.get("expiry_date"):
        try:
            preds.append(f'expiry = "{parsed["expiry_date"].isoformat()}"')
        except Exception:
            # Skip malformed expiry to avoid breaking the filter
            pass

    # Numeric strike: equality only
    if parsed.get("strike") is not None:
        try:
            sval = float(parsed["strike"])
            preds.append(f'strike = {int(sval) if sval.is_integer() else sval}')
        except Exception:
            # Ignore invalid strike
            pass

    return preds

def parse_fo_query(query: str) -> Dict[str, Any]:
    """
    Parses a user query for instruments, extracting structured intent.
    Returns a dictionary.
    """
    q = re.sub(r'\s+', ' ', query).strip().upper()
    
    result = {
        "underlying": None, "instrument_type": None, "option_type": None,
        "exchange": None, "expiry_date": None, "expiry_month": None,
        "expiry_year": None, "relative_week": None, "strike": None,
        "approximate_strike": False, "residual": ""
    }

    if "BANK NIFTY" in q or "NIFTY BANK" in q:
        result["underlying"] = "BANKNIFTY"
        q = q.replace("BANK NIFTY", "").replace("NIFTY BANK", "")

    tokens = q.split()
    
    # Month name to number mapping
    month_map = {name.upper(): i for i, name in enumerate(calendar.month_abbr) if i}
    
    # Exchange hints
    exchange_map = {"NSE": "NSE", "NFO": "NFO", "BFO": "BFO", "MCX": "MCX"}

    # --- Extraction Logic ---
    residual_tokens = []
    
    for token in tokens:
        # Option Type
        if token in ("CE", "PE"):
            result["option_type"] = token
            result["instrument_type"] = token # Infer instrument_type
            continue
        # Futures
        if token in ("FUT", "FUTURE", "FUTURES"):
            result["instrument_type"] = "FUT"
            continue
        # Explicit Equity token (only when user types 'EQ' or 'EQUITY')
        if token in ("EQ", "EQUITY"):
            result["instrument_type"] = "EQ"
            continue
        # Exchange
        if token in exchange_map:
            result["exchange"] = exchange_map[token]
            continue
        # Month
        if token in month_map:
            result["expiry_month"] = month_map[token]
            continue
        # Year (context-aware)
        current_year = datetime.now().year
        # 4-digit year
        if re.fullmatch(r"\d{4}", token):
            year_val = int(token)
            # Accept if a month is already found OR it's a reasonable year
            if result["expiry_month"] or (current_year - 5 <= year_val <= current_year + 5):
                result["expiry_year"] = year_val
                continue
        # 2-digit year
        if re.fullmatch(r"\d{2}", token):
            year_val = 2000 + int(token)
            # Accept only if a month is found OR it's a reasonable year
            if result["expiry_month"] or (current_year - 5 <= year_val <= current_year + 5):
                 if not result["expiry_year"]: # Don't overwrite a 4-digit year
                    result["expiry_year"] = year_val
                 continue
        # Strike
        if re.fullmatch(r"\d{3,}(\.\d+)?", token):
            try:
                result["strike"] = int(float(token))
            except ValueError:
                residual_tokens.append(token)
            continue
        
        residual_tokens.append(token)

    # Determine underlying and residual text
    if residual_tokens:
        # A simple heuristic: if the first token is a known underlying, use it.
        # This can be improved with a proper entity recognition system.
        potential_underlying = residual_tokens[0]
        # A more robust check would involve querying a list of known underlyings.
        # For now, we assume common ones.
        if potential_underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
             if not result["underlying"]: # Don't overwrite pre-parsed underlying
                result["underlying"] = potential_underlying
             # If the next token is a number, it's likely part of the name, not a separate residual
             if len(residual_tokens) > 1 and residual_tokens[1].isdigit():
                 result["residual"] = " ".join(residual_tokens)
             else:
                 result["residual"] = " ".join(residual_tokens[1:])
        else:
             # If not a known index, assume the first token is the underlying
             if not result["underlying"]:
                result["underlying"] = potential_underlying
             result["residual"] = " ".join(residual_tokens[1:])
    
    # If year is not specified for a month, assume current or next year
    if result["expiry_month"] and not result["expiry_year"]:
        today = date.today()
        if result["expiry_month"] < today.month:
            result["expiry_year"] = today.year + 1
        else:
            result["expiry_year"] = today.year
# Do not default instrument_type to 'EQ' automatically.
# Only set instrument_type when the user explicitly supplies an indicator (e.g., "EQ", "CE", "PE", "FUT").
        
    return result

####KITE
from .historical_data import fetch_and_store_historical_data, fetch_and_store_indices_historical_data
from database import get_db_connection

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



# ───────── Alerts (Kite Alerts) ─────────


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
