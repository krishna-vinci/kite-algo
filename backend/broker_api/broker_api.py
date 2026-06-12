import asyncio
import calendar
import csv
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
import psycopg2
import pytz
import requests
from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, Response
from kiteconnect import KiteConnect
from psycopg2.extras import execute_values
from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, relationship, sessionmaker

from backend.app.auth import require_app_user
from backend.app.database import database

from backend.broker_api.session.kite_auth import login_headless
from backend.broker_api.session.kite_session import (
    KiteSession,
    build_kite_client,
    get_kite,
    get_system_access_token,
    rotate_broker_access_token,
    upsert_kite_session,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from backend.app.config import get_scheduler_ntfy_url


SCHEDULER_NTFY_URL = get_scheduler_ntfy_url()


async def send_ntfy_notification(message: str, title: str = "Kite App Notification", tags: Optional[List[str]] = None):
    """Sends a notification to the configured ntfy topic when enabled."""
    if not SCHEDULER_NTFY_URL:
        logger.info("Skipping ntfy notification because SCHEDULER_NTFY_URL is unset")
        return
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

from backend.broker_api.session.kite_auth import API_KEY



# Load environment variables
load_dotenv()

# API router
router = APIRouter()

# Pydantic request models
class TickerRequest(BaseModel):
    symbol: str

class InstrumentsRequest(BaseModel):
    instruments: List[str]


class OHLCResponseData(BaseModel):
    instrument_token: int
    last_price: float
    open: float
    high: float
    low: float
    previous_close: float
    volume: Optional[int] = None
    net_change: Optional[float] = None
    net_change_percent: Optional[float] = None
    close: Optional[float] = None


class OHLCResponse(BaseModel):
    status: str = "success"
    data: Dict[str, OHLCResponseData]


class PortfolioSnapshotCreate(BaseModel):
    strategy_name: str
    symbol: str
    quantity: int
    purchase_price: float # Use float for Pydantic, will be converted to Numeric for SQLAlchemy
    total_value: float # Use float for Pydantic, will be converted to Numeric for SQLAlchemy

    class Config:
        orm_mode = True # Enable ORM mode for Pydantic




# ───────── DATABASE SETUP ─────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('DB_USER', 'postgres')}:{os.getenv('DB_PASSWORD', 'postgres')}@{os.getenv('DB_HOST', 'postgres')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'postgres')}"
)

# synchronous engine + session
engine       = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()
metadata     = MetaData()

# module-level session storage
sessions: Dict[str, str] = {}

# ───────── ORM MODELS ─────────

def run_headless_login_and_persist_system_token(db: Session) -> str:
    """
    Perform headless login and upsert the access_token to KiteSession with session_id='system'.
    Caller is responsible for committing the transaction.
    Returns the redacted fingerprint (last 6 chars) of the access_token.
    """
    kite, at = login_headless()
    profile = kite.profile()
    broker_user_id = str(profile.get("user_id") or "").strip() or None
    rotated_sessions = rotate_broker_access_token(db, at, broker_user_id=broker_user_id)
    fp = at[-6:] if isinstance(at, str) else ""
    logger.info("System access_token refreshed and propagated (..%s, updated_sessions=%s)", fp, rotated_sessions)
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



@router.on_event("startup")
async def _startup():
    try:
        KiteSession.__table__.create(bind=engine, checkfirst=True)
    except Exception as e:
        logger.error(f"Failed to ensure KiteSession table: {e}", exc_info=True)

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

def get_psql_conn():
    """
    Fallback raw psycopg2 connection for ad-hoc queries.
    """
    return psycopg2.connect(DATABASE_URL)
MEILI_INSTRUMENT_MARKET_DATE_SQL = "(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date"

KITE_INSTRUMENT_IMPORT_EXCHANGES = ["NSE", "NFO", "BSE", "BFO", "CDS", "BCD", "MCX"]



async def sync_and_reindex_orchestrator(
    session: Session,
    refresh_from_broker: bool,
    backfill_only_nulls: bool,
    reindex: bool,
    background_tasks: Optional[BackgroundTasks] = None
) -> Dict[str, Optional[int]]:
    """
    Orchestrates optional instrument refresh and backfill of underlying/option_type.
    """
    refreshed_count: Optional[int] = None
    backfilled_counts: Dict[str, int] = {"processed": 0, "updated": 0, "skipped": 0}

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
                    kite_instance = build_kite_client(access_token, session_id="system")
                    
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

        # 3. Notify Go market-runtime to refresh instrument cache
        try:
            import httpx
            runtime_url = os.getenv("MARKET_RUNTIME_HTTP_URL", "http://market-runtime:8780")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{runtime_url}/internal/market-runtime/instruments/refresh")
                if resp.status_code == 200:
                    logger.info("Go instrument store refreshed successfully")
                else:
                    logger.warning(f"Go instrument store refresh returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"Failed to notify Go market-runtime of instrument refresh: {e}")

        return {
            "refreshed": refreshed_count,
            "backfilled": backfilled_counts["processed"],
            "updated": backfilled_counts["updated"],
            "skipped": backfilled_counts["skipped"]
        }

    except Exception as e:
        logger.error(f"Error in sync-and-reindex orchestrator operation: {e}", exc_info=True)
        # Re-raise or handle as appropriate for a helper function
        raise e

######kite

# ─────────── Login endpoint ───────────
@router.post("/login_kite")
def headless_login(request: Request, response: Response, db: Session = Depends(get_db)):
    require_app_user(request)
    try:
        kite, at = login_headless()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"An unexpected error occurred: {e}")

    profile = kite.profile()
    broker_user_id = str(profile.get("user_id") or "").strip() or None

    sid = str(uuid.uuid4())
    upsert_kite_session(db, sid, at, broker_user_id=broker_user_id)
    db.commit()

    # Also persist/refresh system token so app startup and jobs use a consistent source
    rotate_broker_access_token(db, at, broker_user_id=broker_user_id)
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
        path="/",
    )

    # Also return session_id so the frontend can send it in the X-Session-ID header (dev-friendly)
    return {"session_id": sid, "profile": profile, "authenticated": True}


# ─────────── Logout endpoint ───────────
@router.post("/logout_kite")
def logout(response: Response, request: Request, db: Session = Depends(get_db)):
    require_app_user(request)
    sid = request.cookies.get("kite_session_id")
    if sid:
        db.query(KiteSession).filter_by(session_id=sid).delete()
        db.commit()
    response.delete_cookie("kite_session_id", path="/")
    return {"message": "Logged out"}









@router.get("/profile_kite")
def profile(request: Request, db: Session = Depends(get_db)):
    require_app_user(request)
    try:
        kite = get_kite(request, db)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        access_token = get_system_access_token(db)
        if not access_token:
            raise
        kite = build_kite_client(access_token, session_id="system")
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


@router.get("/quote/ohlc", response_model=OHLCResponse, summary="Get OHLC and LTP for multiple instruments")
def get_ohlc(
    i: List[str] = Query(..., description="Instrument identifier in the format EXCHANGE:TRADINGSYMBOL"),
    kite: KiteConnect = Depends(get_kite),
):
    """
    Retrieves OHLC (previous day's close) and last traded price for up to 1000 instruments.
    """
    instruments = sorted(list(set(i)))
    count = len(instruments)
    if not (1 <= count <= 1000):
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "Number of instruments must be between 1 and 1000."},
        )

    try:
        ohlc_data = kite.quote(instruments)
        
        response_data = {}
        for instrument, data in ohlc_data.items():
            try:
                # Ensure all required fields are present
                if "instrument_token" in data and "last_price" in data and "ohlc" in data and "open" in data["ohlc"] and "high" in data["ohlc"] and "low" in data["ohlc"] and "close" in data["ohlc"]:
                    last_price = data["last_price"]
                    previous_close = data["ohlc"]["close"]
                    net_change = last_price - previous_close
                    net_change_percent = (net_change / previous_close * 100) if previous_close != 0 else 0
                    
                    response_data[instrument] = {
                        "instrument_token": data["instrument_token"],
                        "last_price": last_price,
                        "open": data["ohlc"]["open"],
                        "high": data["ohlc"]["high"],
                        "low": data["ohlc"]["low"],
                        "previous_close": previous_close,
                        "volume": data.get("volume", 0),
                        "net_change": round(net_change, 2),
                        "net_change_percent": round(net_change_percent, 2),
                        "close": last_price,
                    }
            except (KeyError, TypeError):
                # Skip instruments with missing data
                continue
        
        return {"status": "success", "data": response_data}

    except Exception as e:
        logger.error(f"Upstream Kite OHLC request failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail={"status": "error", "message": f"Upstream error: {str(e)}"},
        )



 
 # ─────────── Instruments import functionality ───────────
def batch_upsert_instruments(records: list, batch_size: int = 1000):
    """Batch upsert instruments using psycopg2 for performance"""
    if not records:
        return 0
    
    conn = get_psql_conn()
    total_upserted = 0
    
    try:
        with conn.cursor() as cur:
            upsert_query = """
                INSERT INTO kite_instruments (
                    instrument_token, exchange_token, tradingsymbol, name, last_price,
                    expiry, strike, tick_size, lot_size, instrument_type, segment, exchange, last_updated
                ) VALUES %s
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
            
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                values = []
                for record in batch:
                    values.append((
                        int(record['instrument_token']) if record['instrument_token'] else None,
                        int(record['exchange_token']) if record['exchange_token'] else None,
                        record['tradingsymbol'],
                        record.get('name', ''),
                        float(record['last_price']) if record['last_price'] else None,
                        record['expiry'] if record['expiry'] else None,
                        float(record['strike']) if record['strike'] else None,
                        float(record['tick_size']) if record['tick_size'] else None,
                        int(record['lot_size']) if record['lot_size'] else None,
                        record.get('instrument_type', ''),
                        record.get('segment', ''),
                        record.get('exchange', ''),
                        datetime.utcnow()
                    ))
                
                execute_values(cur, upsert_query, values, page_size=batch_size)
                conn.commit()
                total_upserted += len(batch)
                logger.info(f"Upserted batch {i//batch_size + 1}: {len(batch)} instruments (total: {total_upserted})")
        
        return total_upserted
    except Exception as e:
        conn.rollback()
        logger.error(f"Batch upsert failed: {e}", exc_info=True)
        raise
    finally:
        conn.close()

async def import_instruments_for_exchange(exchange: str, kite: KiteConnect):
    """Import instruments for a specific exchange"""
    try:
        instruments = kite.instruments(exchange)
        count = batch_upsert_instruments(instruments, batch_size=1000)
        return {"message": f"Imported {count} instruments for exchange {exchange}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to import instruments: {str(e)}")

# ─────────── Instruments endpoints ───────────

async def import_all_instruments(kite: KiteConnect = Depends(get_kite)):
    """Import all instruments from major exchanges for internal maintenance flows."""
    results = []
    
    for exchange in KITE_INSTRUMENT_IMPORT_EXCHANGES:
        try:
            result = await import_instruments_for_exchange(exchange, kite)
            results.append(result)
        except Exception as e:
            results.append({"exchange": exchange, "error": str(e)})
    
    return {"message": "Imported all instruments", "results": results}

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

        kite = build_kite_client(access_token, session_id="system")
        
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
    refresh_from_broker: bool = True
    backfill_only_nulls: bool = True
    reindex: bool = True  # DEPRECATED: Meilisearch removed

    # refresh_from_broker=True calls an internal import/refresh function (e.g., import_all_instruments) directly if present;
    # it does not call any HTTP endpoint. If no internal refresh function exists, this endpoint still backfills
    # underlying/option_type for current DB records and reindexes Meilisearch.
@router.post("/instruments/sync-and-reindex")
async def sync_and_reindex_instruments(
    background_tasks: BackgroundTasks,
    request: Optional[SyncAndReindexRequest] = Body(default=None),
    db: Session = Depends(get_db),
    # kite: KiteConnect = Depends(get_kite) # KiteConnect instance is handled internally by orchestrator for refresh
):
    """
    Orchestrates instrument refresh from broker, backfill of underlying/option_type, and Meilisearch reindex.

    If the request body is omitted, this performs the full maintenance flow by default.
    """
    try:
        request = request or SyncAndReindexRequest()
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
    search_term = (q or query or "").strip()
    rows = await sql_fallback_plain(search_term, limit)
    return {"results": rows, "total": len(rows), "source": "sql"}

# ─────────── Daily update functionality ───────────
async def schedule_daily_instruments_update():
    """Schedules the daily instruments maintenance orchestrator."""
    IST = pytz.timezone('Asia/Kolkata')
    while True:
        try:
            now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
            now_ist = now_utc.astimezone(IST)

            # Calculate next run time for 07:00 AM IST
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
    exchange_map = {exchange: exchange for exchange in KITE_INSTRUMENT_IMPORT_EXCHANGES}

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
from backend.broker_api.market.historical_data import fetch_and_store_historical_data, fetch_and_store_indices_historical_data
from backend.app.database import get_db_connection

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
