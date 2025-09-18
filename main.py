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
from zoneinfo import ZoneInfo

# Configure logging for the main application
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Suppress INFO level logs from httpx for specific API calls
logging.getLogger("httpx").setLevel(logging.WARNING)

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

from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect
from broker_api.broker_api import router as kite_router
from strategies.momentum import router as momentum_router

from broker_api.broker_api import get_kite
from kiteconnect import KiteConnect
from typing import List, Optional
from server import mcp
from contextlib import asynccontextmanager
import server
from kite_auth import login_headless
import logging
from database import SessionLocal, database as async_db
from broker_api.broker_api import KiteSession, get_system_access_token, upsert_kite_session
from broker_api.broker_api import run_headless_login_and_persist_system_token
from broker_api.kite_auth import API_KEY
from broker_api.websocket_manager import WebSocketManager


# Global instance for the WebSocketManager
ws_manager: Optional[WebSocketManager] = None

# Daily gating event (set once headless login succeeds; cleared before daily rotation)
daily_token_ready: asyncio.Event = asyncio.Event()

# 1. Create the MCP's ASGI app
mcp_app = mcp.http_app(path='/mcp')

# Wrap the MCP ASGI app to inject a per-request KiteConnect (based on cookie 'kite_session_id')
class MCPAuthWrapper:
    def __init__(self, app):
        self.app = app

    @staticmethod
    def _get_cookie(scope, name: str) -> str | None:
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"cookie")
        if not raw:
            return None
        try:
            cookie_str = raw.decode("latin-1")
            for part in cookie_str.split(";"):
                k_v = part.strip().split("=", 1)
                if len(k_v) == 2 and k_v[0] == name:
                    return k_v[1]
        except Exception:
            return None
        return None

    async def __call__(self, scope, receive, send):
        # Intercept all HTTP requests for this mounted app to inject request-scoped KiteConnect
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        ctx_token = None
        try:
            sid = self._get_cookie(scope, "kite_session_id")
            if sid:
                db = SessionLocal()
                try:
                    ks = db.query(KiteSession).filter_by(session_id=sid).first()
                    if ks:
                        from kiteconnect import KiteConnect
                        kite = KiteConnect(api_key=API_KEY)
                        kite.set_access_token(ks.access_token)
                        ctx_token = server.set_request_kite(kite)
                finally:
                    db.close()
            return await self.app(scope, receive, send)
        finally:
            if ctx_token is not None:
                server.reset_request_kite(ctx_token)

# Wrapped app that injects request-scoped KiteConnect into FastMCP tools
mcp_app_wrapped = MCPAuthWrapper(mcp_app)

# 2. Combine the lifespans
@asynccontextmanager
async def combined_lifespan(app: FastAPI):
    global ws_manager
    # Perform headless login at startup and store the KiteConnect instance
    token_watcher_task = None
    scheduler_task = None
    try:
        # Determine system access_token from DB; validate and fallback to headless login
        at = None
        kite = None
        db = None
        try:
            db = SessionLocal()
            # Prefer explicit "system" session_id token
            system_at = get_system_access_token(db)
            if system_at:
                from kiteconnect import KiteConnect
                kite = KiteConnect(api_key=API_KEY)
                kite.set_access_token(system_at)
                at = system_at
                try:
                    # Lightweight validation
                    kite.profile()
                    logging.info("Using system access_token from DB (..%s)", at[-6:] if isinstance(at, str) else "")
                except Exception as e:
                    logging.warning("System token validation failed (..%s); performing headless login: %s", (at[-6:] if isinstance(at, str) else ""), e)
                    kite, at = login_headless()
                    upsert_kite_session(db, "system", at)
                    db.commit()
                    logging.info("Refreshed system access_token via headless login (..%s)", at[-6:] if isinstance(at, str) else "")
            else:
                # No system token; perform headless login and persist
                kite, at = login_headless()
                upsert_kite_session(db, "system", at)
                db.commit()
                logging.info("Obtained system access_token via headless login (..%s)", at[-6:] if isinstance(at, str) else "")
        finally:
            try:
                if db:
                    db.close()
            except Exception:
                pass

        server.mcp_kite_instance = kite
        logging.info("MCP Kite instance initialized successfully.")

        # Initialize and start the WebSocketManager with the selected token
        logging.info("Initializing WebSocketManager...")
        # Get the main event loop to pass to the WebSocketManager
        main_event_loop = asyncio.get_event_loop()
        ws_manager = WebSocketManager(api_key=API_KEY, access_token=at, main_event_loop=main_event_loop)
        ws_manager.start()
        logging.info("WebSocketManager started.")

        # Start background token watcher to rotate WS token when DB 'system' token changes
        async def _system_token_watcher():
            poll_seconds = int(os.getenv("SYSTEM_TOKEN_POLL_SEC", "45"))
            last_token = at
            while True:
                try:
                    await asyncio.sleep(max(30, min(poll_seconds, 60)))
                    _db = SessionLocal()
                    try:
                        new_token = get_system_access_token(_db)
                    finally:
                        _db.close()
                    if new_token and new_token != last_token:
                        old_fp = (last_token[-6:] if isinstance(last_token, str) else "")
                        new_fp = (new_token[-6:] if isinstance(new_token, str) else "")
                        logging.info("System token change detected; rotating WS token (..%s -> ..%s)", old_fp, new_fp)
                        ws_manager.reinit_with_token(new_token)
                        last_token = new_token
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logging.error("Token watcher error: %s", e, exc_info=True)
                    # Continue watching
                    continue

        token_watcher_task = asyncio.create_task(_system_token_watcher())
        # Initialize daily token gate in app state and start scheduler
        app.state.daily_token_ready = daily_token_ready
        if not daily_token_ready.is_set():
            daily_token_ready.set()
        logging.info("[GATE] Initialized and open at startup (will close at next 07:31 IST)")
        scheduler_task = asyncio.create_task(daily_token_scheduler())

        # Ensure async DB is connected for workers
        try:
            if hasattr(async_db, "is_connected") and not async_db.is_connected:
                await async_db.connect()
        except Exception:
            pass
        # Start alert dispatcher (WS text messages) and 2s polling fallback
        alerts_ntfy_url = os.getenv("KITE_ALERTS_NTFY_URL") or os.getenv("kite_alerts_NTFY_URL") or "https://ntfy.krishna.quest/kite-alerts"
        asyncio.create_task(alert_event_dispatcher(ws_manager, alerts_ntfy_url))
        asyncio.create_task(alerts_poll_worker(API_KEY, alerts_ntfy_url))

        # Ensure Meilisearch index exists on startup (and bootstrap reindex if empty)
        try:
            ensure_instruments_index()
            logger.info("Meilisearch index 'instruments' ensured on startup")
            try:
                client = get_meili_client(admin=True)
                index = client.index("instruments")
                stats = index.get_stats() if hasattr(index, "get_stats") else index.stats()
                num_docs = (stats.get("numberOfDocuments") or stats.get("number_of_documents") or 0)
                if int(num_docs) == 0:
                    logger.info("Meilisearch 'instruments' index is empty; triggering bootstrap reindex...")
                    await meili_reindex_instruments()
            except Exception as ie:
                logger.exception("Startup Meilisearch reindex-if-empty check failed: %s", ie)
        except Exception as e:
            logger.exception("Failed to ensure Meilisearch index on startup: %s", e)
    except Exception as e:
        logging.error(f"Failed to initialize MCP Kite instance or WebSocketManager: {e}", exc_info=True)
        # Depending on the desired behavior, you might want to exit the application
        # or proceed without a valid Kite instance for MCP.
        # For now, we'll log the error and continue.
        server.mcp_kite_instance = None

    async with mcp_app.lifespan(app):
        yield
    
    # Cleanup on shutdown
    # Cancel token watcher first
    try:
        if 'token_watcher_task' in locals() and token_watcher_task:
            token_watcher_task.cancel()
            try:
                await token_watcher_task
            except Exception:
                pass
    except Exception:
        pass
    # Cancel daily scheduler
    try:
        if 'scheduler_task' in locals() and scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except Exception:
                pass
    except Exception:
        pass
    if ws_manager:
        logging.info("Stopping WebSocketManager...")
        ws_manager.stop()
        logging.info("WebSocketManager stopped.")


app = FastAPI(title="Kite App API", lifespan=combined_lifespan)

# 3. Mount the MCP app at a subpath so normal FastAPI routes remain reachable
# Final MCP endpoint will be available at /llm/mcp (since mcp_app was created with path='/mcp')
app.mount("/llm", mcp_app_wrapped)

# 3b. Also expose MCP directly at /mcp for clients expecting the legacy path
# For this mount, set the MCP ASGI app path to "/" so the full endpoint is exactly "/mcp"
mcp_app_direct = mcp.http_app(path="/")
mcp_app_direct_wrapped = MCPAuthWrapper(mcp_app_direct)
app.mount("/mcp", mcp_app_direct_wrapped)

# 4. Include existing API routes (mounted under /broker to match frontend)
app.include_router(broker_api_router, prefix="/broker")
app.include_router(momentum_router, prefix="/broker")

from broker_api.broker_api import ensure_instruments_index, get_meili_client, meili_reindex_instruments
import logging

logger = logging.getLogger(__name__)


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
    # IMPORTANT: For credentialed requests, browsers require a specific echoed Origin.
    # Using allow_origin_regex lets Starlette echo the incoming Origin when it matches.
    allow_origins=[],                 # do not use "*" with allow_credentials=True
    allow_origin_regex=".*",          # echo any Origin in dev; tighten for prod
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
    info = {"status": "running", "backend": "FastAPI"}
    try:
        wm = ws_manager
        if wm:
            info.update({
                "websocket_status": wm.get_websocket_status(),
                "num_clients": len(wm.clients),
                "aggregated_token_count": len(wm.token_refcount),
                "flush_interval_ms": wm.flush_interval_ms,
            })
    except Exception:
        pass
    return info


@app.websocket("/broker/ws/marketwatch")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if not action:
                await websocket.send_text(json.dumps({"type": "error", "message": "Missing action"}))
                continue

            if action == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            tokens = data.get("tokens") or []
            mode = data.get("mode")

            if tokens and isinstance(tokens, list):
                try:
                    tokens = [int(t) for t in tokens]
                except Exception:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Invalid tokens"}))
                    continue

            if action == "subscribe" and tokens:
                await ws_manager.subscribe(websocket, tokens, mode)
            elif action == "unsubscribe" and tokens:
                await ws_manager.unsubscribe(websocket, tokens)
            elif action == "set_mode" and tokens and mode:
                await ws_manager.set_mode(websocket, tokens, mode)
            else:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid action"}))
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logging.info("Client disconnected.")
    except Exception as e:
        logging.error(f"Error in websocket endpoint: {e}", exc_info=True)
        ws_manager.disconnect(websocket)

# ───────── Daily token gate and scheduler ─────────
async def ensure_daily_token_ready(timeout: float = 900.0) -> None:
    """
    Wait until the daily system token has been refreshed and the gate is opened.
    Logs if waiting exceeds timeout but continues to wait afterwards.
    """
    try:
        if daily_token_ready.is_set():
            return
        logging.info("[GATE] Waiting for daily system token refresh to complete...")
        await asyncio.wait_for(daily_token_ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logging.warning("[GATE] Still waiting for system token refresh; continuing to wait without timeout.")
        await daily_token_ready.wait()

async def daily_token_scheduler() -> None:
    """
    Runs forever:
      - Sleeps until 07:31 Asia/Kolkata
      - Clears gate, performs headless login + persist 'system' token with retries
      - Sets gate on success
      - Triggers dependent daily jobs (e.g., instruments refresh)
    """
    tz = ZoneInfo("Asia/Kolkata")
    while True:
        try:
            now = datetime.now(tz)
            next_run = now.replace(hour=7, minute=31, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            sleep_sec = max(1, int((next_run - now).total_seconds()))
            logging.info("[SCHED] Next daily headless login scheduled at %s", next_run.strftime("%Y-%m-%d %H:%M:%S %Z%z"))
            await asyncio.sleep(sleep_sec)

            # Begin rotation
            logging.info("[SCHED] 07:31 IST reached; clearing gate and refreshing system token")
            daily_token_ready.clear()

            # Retry loop until success
            while True:
                try:
                    db = SessionLocal()
                    try:
                        fp = run_headless_login_and_persist_system_token(db)
                        db.commit()
                    finally:
                        db.close()
                    logging.info("[SCHED] System access_token rotated (..%s)", fp)
                    break
                except Exception as e:
                    logging.warning("[SCHED] Headless login failed: %s; retrying in 30s", e)
                    await asyncio.sleep(30)

            # Open gate
            daily_token_ready.set()
            logging.info("[GATE] Opened after successful token refresh")

            # Kick off dependent daily jobs (fire-and-forget)
            # No dependent jobs for token refresh; other schedulers handle their own updates.

        except asyncio.CancelledError:
            logging.info("[SCHED] Daily token scheduler cancelled")
            break
        except Exception as e:
            logging.error("[SCHED] Scheduler loop error: %s", e, exc_info=True)
            await asyncio.sleep(30)

# ───────── Alerts realtime dispatcher and polling fallback ─────────
import os as _os
import json as _json
import httpx as _httpx
from typing import Optional as _Optional, List as _List, Dict as _Dict
from datetime import datetime as _dt

# Helper: publish to ntfy
async def _publish_ntfy(ntfy_url: str, title: str, message: str, tags: _Optional[_List[str]] = None) -> None:
    headers = {"Title": title}
    if tags:
        headers["Tags"] = ",".join(tags)
    async with _httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(ntfy_url, content=message, headers=headers)
            r.raise_for_status()
            logging.info(f"[NTFY] published: {title}")
        except Exception as e:
            logging.error(f"[NTFY] publish failed: {e}")

# Helper: Upsert alert mirror row
async def _upsert_alert_row(db, row: _Dict) -> None:
    # Minimal normalization from Kite Alerts list/get payload shape
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
        "basket": _json.dumps(row.get("basket")) if row.get("basket") is not None else None,
        "alert_count": int(row.get("alert_count") or 0),
    }
    try:
        await async_db.execute(sql, values)
    except Exception as e:
        # Cheap way to detect missing columns from the alerts extension
        if "column" in str(e) and "does not exist" in str(e):
            logging.warning(f"[ALERTS] upsert failed, attempting one-shot schema migration: {e}")
            try:
                run_schema_migrations()
                await async_db.execute(sql, values)
                logging.info("[ALERTS] schema migration successful, retried upsert")
            except Exception as e2:
                logging.error(f"[ALERTS] failed to run schema migration or retry upsert: {e2}", exc_info=True)
                raise e2
        else:
            raise e

# Helper: fetch alert mirror state (last counts and controls)
async def _get_alert_controls(uuid: str):
    sql = """
    SELECT last_alert_count, last_notified_at, cooldown_sec, schedule, name, lhs_tradingsymbol, operator, rhs_constant
    FROM alerts WHERE uuid = :uuid
    """
    return await async_db.fetch_one(sql, {"uuid": uuid})

# Helper: update controls after notify
async def _update_alert_after_notify(uuid: str, new_count: int) -> None:
    sql = """
    UPDATE alerts
    SET last_alert_count = :cnt, last_notified_at = NOW(), updated_at = NOW()
    WHERE uuid = :uuid
    """
    await async_db.execute(sql, {"uuid": uuid, "cnt": new_count})

# Helper: set only last_alert_count (when suppressing notification)
async def _ack_alert_count(uuid: str, new_count: int) -> None:
    sql = "UPDATE alerts SET last_alert_count = :cnt, updated_at = NOW() WHERE uuid = :uuid"
    await async_db.execute(sql, {"uuid": uuid, "cnt": new_count})

# Helper: get latest history timestamp seen
async def _get_latest_history_time(uuid: str):
    sql = "SELECT MAX(triggered_at) AS t FROM alert_history WHERE alert_uuid = :uuid"
    row = await async_db.fetch_one(sql, {"uuid": uuid})
    return row["t"] if row else None

# Helper: insert history row
async def _insert_alert_history(uuid: str, triggered_at: str, trigger_price: float, meta: _Dict, condition: _Optional[str]):
    # Parse timestamp string into a datetime object
    ts_dt = None
    if triggered_at:
        try:
            # Try parsing the format from Kite API first
            ts_dt = _dt.strptime(triggered_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # Fallback for ISO format (e.g., from utcnow().isoformat())
            try:
                ts_dt = _dt.fromisoformat(triggered_at)
            except ValueError:
                logging.warning(f"[ALERTS] could not parse timestamp '{triggered_at}', using NOW()")
                ts_dt = _dt.utcnow()
    else:
        ts_dt = _dt.utcnow()

    sql = """
    INSERT INTO alert_history (alert_uuid, triggered_at, trigger_price, meta)
    VALUES (:uuid, :ts, :price, :meta)
    """
    await async_db.execute(sql, {
        "uuid": uuid,
        "ts": ts_dt,
        "price": float(trigger_price) if trigger_price is not None else 0.0,
        "meta": _json.dumps({"condition": condition, "meta": meta})
    })

# Basic schedule check (Phase 1: allow all hours; placeholder for market hours)
def _is_within_schedule(_schedule_json: _Optional[str]) -> bool:
    # TODO: Implement real market hours window in Phase 1 polish
    return True

# Cooldown check
def _passes_cooldown(last_notified_at, cooldown_sec: int) -> bool:
    if not last_notified_at:
        return True
    try:
        # last_notified_at is a datetime object when fetched from DB
        delta = _dt.utcnow() - last_notified_at
        return delta.total_seconds() >= max(int(cooldown_sec or 0), 0)
    except Exception:
        return True

# Build Kite REST headers
def _kite_headers(api_key: str, access_token: str) -> _Dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

# Fetch Kite alerts list
async def _kite_list_alerts(api_key: str, access_token: str) -> _List[_Dict]:
    url = "https://api.kite.trade/alerts"
    async with _httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=_kite_headers(api_key, access_token))
        r.raise_for_status()
        payload = r.json()
        return payload.get("data", []) or []

# Fetch Kite alert history
async def _kite_alert_history(api_key: str, access_token: str, uuid: str) -> _List[_Dict]:
    url = f"https://api.kite.trade/alerts/{uuid}/history"
    async with _httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=_kite_headers(api_key, access_token))
        r.raise_for_status()
        payload = r.json()
        return payload.get("data", []) or []

# One-shot reconciliation tick (used by WS dispatcher and fallback worker)
async def alerts_poll_tick(api_key: str, access_token: str, ntfy_url: str) -> None:
    try:
        alerts = await _kite_list_alerts(api_key, access_token)
    except Exception as e:
        logging.error(f"[ALERTS] list failed: {e}")
        return

    for a in alerts:
        try:
            await _upsert_alert_row(async_db, a)
            uuid = a.get("uuid")
            if not uuid:
                continue
            # Controls
            ctrl = await _get_alert_controls(uuid)
            last_alert_count = int(ctrl["last_alert_count"]) if ctrl and ctrl["last_alert_count"] is not None else 0
            cooldown_sec = int(ctrl["cooldown_sec"]) if ctrl and ctrl["cooldown_sec"] is not None else 120
            last_notified_at = ctrl["last_notified_at"] if ctrl else None
            schedule_json = ctrl["schedule"] if ctrl else None

            current_count = int(a.get("alert_count") or 0)
            if current_count <= last_alert_count:
                continue

            # Fetch history and process only new entries
            hist = await _kite_alert_history(api_key, access_token, uuid)
            latest_seen = await _get_latest_history_time(uuid)
            new_events = []
            for h in hist:
                created_at = h.get("created_at") or h.get("timestamp")
                # When DB has no history, consider all as new
                if latest_seen is None:
                    new_events.append(h)
                else:
                    # Compare timestamps lexically; Postgres will parse during insert
                    try:
                        # Convert both to datetime for robust compare
                        ca = _dt.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                        if latest_seen.tzinfo is None:
                            # best-effort naive compare
                            is_new = ca > latest_seen.replace(tzinfo=None)
                        else:
                            is_new = ca.replace(tzinfo=None) > latest_seen.replace(tzinfo=None)
                        if is_new:
                            new_events.append(h)
                    except Exception:
                        # Fallback: accept as new when count increased
                        new_events.append(h)

            # If no fine-grained history diff could be determined, still ack count
            if not new_events:
                await _ack_alert_count(uuid, current_count)
                continue

            # Evaluate schedule/cooldown only once per batch (suppress if not allowed)
            if not _is_within_schedule(_json.dumps(schedule_json) if isinstance(schedule_json, dict) else schedule_json):
                await _ack_alert_count(uuid, current_count)
                continue
            if not _passes_cooldown(last_notified_at, cooldown_sec):
                await _ack_alert_count(uuid, current_count)
                continue

            # Insert histories and notify (collapse multiple to a single notification with summary)
            for h in new_events:
                condition = h.get("condition")
                meta = h.get("meta")
                # meta array; try last_price if available
                last_price = None
                if isinstance(meta, list) and meta:
                    last_price = meta[0].get("last_price")
                await _insert_alert_history(
                    uuid=uuid,
                    triggered_at=h.get("created_at") or h.get("timestamp") or _dt.utcnow().isoformat(),
                    trigger_price=last_price if last_price is not None else 0.0,
                    meta=h,
                    condition=condition
                )

            # Build notification message
            name = a.get("name") or (ctrl["name"] if ctrl else uuid)
            sym = a.get("lhs_tradingsymbol") or (ctrl["lhs_tradingsymbol"] if ctrl else "")
            op = a.get("operator") or (ctrl["operator"] if ctrl else "")
            rhs = a.get("rhs_constant") if a.get("rhs_constant") is not None else (ctrl["rhs_constant"] if ctrl else "")
            title = f"Alert triggered: {name}"
            body = f"{sym} {op} {rhs} | {len(new_events)} event(s) | uuid={uuid}"
            await _publish_ntfy(ntfy_url, title, body, tags=["alert", "kite", sym] if sym else ["alert", "kite"])

            # Update controls
            await _update_alert_after_notify(uuid, current_count)

        except Exception as e:
            logging.error(f"[ALERTS] reconcile error for uuid={a.get('uuid')}: {e}", exc_info=True)

# Background worker: 2s fallback polling
async def alerts_poll_worker(api_key: str, ntfy_url: str) -> None:
    logging.info("[ALERTS] 2s polling worker started")
    while True:
        await ensure_daily_token_ready()
        try:
            db = SessionLocal()
            try:
                at = get_system_access_token(db)
            finally:
                db.close()
            if at:
                await alerts_poll_tick(api_key, at, ntfy_url)
        except Exception as e:
            logging.error(f"[ALERTS] poll tick failed: {e}", exc_info=True)
        await asyncio.sleep(2)

# Dispatcher: consume WebSocket alert events queue and trigger quick reconcile + immediate ntfy
async def alert_event_dispatcher(ws_mgr: "WebSocketManager", ntfy_url: str) -> None:
    logging.info("[ALERTS] WS dispatcher started")
    api_key = API_KEY
    while True:
        await ensure_daily_token_ready()
        event = await ws_mgr.alert_event_queue.get()
        try:
            # Immediate lightweight notification with raw context
            raw = event.get("raw") if isinstance(event, dict) else {}
            title = "Alert event (stream)"
            body = _json.dumps(raw)[:1000]  # cap size
            await _publish_ntfy(ntfy_url, title, body, tags=["alert", "kite", "ws"])
        except Exception as e:
            logging.error(f"[ALERTS] dispatcher immediate notify failed: {e}")
        # Quick reconcile pass to persist+enrich using the latest system token
        try:
            db = SessionLocal()
            try:
                access_token = get_system_access_token(db)
            finally:
                db.close()
            if access_token:
                await alerts_poll_tick(api_key, access_token, ntfy_url)
        except Exception as e:
            logging.error(f"[ALERTS] dispatcher reconcile failed: {e}", exc_info=True)

# Minimal endpoints for testing the pipeline
@app.post("/alerts/test-notification")
async def alerts_test_notification():
    ntfy_url = _os.getenv("KITE_ALERTS_NTFY_URL") or _os.getenv("kite_alerts_NTFY_URL") or "https://ntfy.krishna.quest/kite-alerts"
    await _publish_ntfy(ntfy_url, "Test: Kite Alerts", "This is a test notification from backend.", tags=["test","alerts"])
    return {"status": "ok"}

@app.get("/alerts/mirror")
async def alerts_mirror_list(limit: int = 100):
    rows = await async_db.fetch_all(
        "SELECT * FROM alerts ORDER BY updated_at DESC LIMIT :n",
        {"n": max(1, min(limit, 500))}
    )
    # Convert to JSON-serializable
    def _row_to_dict(r):
        try:
            d = dict(r)
            if d.get("basket") and isinstance(d["basket"], str):
                try:
                    d["basket"] = _json.loads(d["basket"])
                except Exception:
                    pass
            return d
        except Exception:
            return {}
    return {"data": [_row_to_dict(r) for r in rows]}

# Admin endpoint to ensure schema.sql migrations are applied
from database import get_db_connection as _get_db_conn_for_schema

@app.post("/admin/run-schema")
def run_schema_migrations():
    """
    One-shot: executes schema.sql via database.get_db_connection() which runs create_tables_if_not_exists.
    Safe and idempotent. Use before first Alerts usage to ensure columns exist.
    """
    conn = None
    try:
        conn = _get_db_conn_for_schema()
        return {"status": "ok", "message": "schema.sql executed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
