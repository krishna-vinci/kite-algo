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
from broker_api.alerts_router import router as alerts_router
from broker_api.performance_router import router as performance_router


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
from alerts.engine import AlertsEngine
from database import get_user_settings, update_user_settings
from pydantic import BaseModel

class UserSubscriptions(BaseModel):
    groups: List[dict]
    activeGroupId: Optional[str] = None

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

def run_schema_migrations() -> None:
    """
    Ensure database schema is applied by executing schema.sql via get_db_connection().
    Safe to call multiple times.
    """
    conn = None
    try:
        conn = get_db_connection()  # get_db_connection() internally applies schema.sql and commits
        logging.info("Schema migrations ensured.")
    except Exception as e:
        logging.error("Schema migration failed: %s", e, exc_info=True)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

# 2. Combine the lifespans
@asynccontextmanager
async def combined_lifespan(app: FastAPI):
    global ws_manager
    # Perform headless login at startup and store the KiteConnect instance
    token_watcher_task = None
    scheduler_task = None
    try:
        # Ensure the schema is applied before any other database operations
        run_schema_migrations()
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
        # Expose ws_manager for routers to access latest ticks
        app.state.ws_manager = ws_manager

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
        # Start AlertsEngine (Phase 0) after WS manager and async DB are ready
        try:
            alerts_engine = AlertsEngine(async_db, ws_manager, app)
            alerts_engine.start()
            app.state.alerts_engine = alerts_engine
            logging.info("AlertsEngine started (interval_ms=%s)", getattr(alerts_engine, "interval_ms", None))
        except Exception as e:
            logging.error("Failed to start AlertsEngine: %s", e, exc_info=True)

        # Start alert dispatcher (WS text messages) and 2s polling fallback
        alerts_ntfy_url = os.getenv("KITE_ALERTS_NTFY_URL") or os.getenv("kite_alerts_NTFY_URL") or "https://ntfy.krishna.quest/kite-alerts"
        # TODO: alert_event_dispatcher needs to be implemented. See broker_api/websocket_manager.py:487
        # asyncio.create_task(alert_event_dispatcher(ws_manager, alerts_ntfy_url))
        # TODO: alerts_poll_worker needs to be implemented.
        # asyncio.create_task(alerts_poll_worker(API_KEY, alerts_ntfy_url))

        # Ensure Meilisearch index exists on startup (and bootstrap reindex if empty)
        try:
            # Quick fix: force-reset settings on every startup
            reset_meili_settings()
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
    # Stop AlertsEngine
    try:
        eng = getattr(app.state, "alerts_engine", None)
        if eng:
            await eng.stop()
            logging.info("AlertsEngine stopped.")
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
app.include_router(alerts_router, prefix="/alerts")

app.include_router(performance_router, prefix="/broker")

from broker_api.broker_api import ensure_instruments_index, get_meili_client, meili_reindex_instruments
import logging

logger = logging.getLogger(__name__)


def reset_meili_settings():
    """
    Force-applies the latest index settings from the Python codebase to Meilisearch.
    This is a quick fix for ensuring settings are synchronized on startup.
    """
    try:
        logger.info("Attempting to reset Meilisearch index settings...")
        ensure_instruments_index()
        logger.info("Meilisearch index settings reset successfully.")
    except Exception as e:
        logger.error(f"Failed to reset Meilisearch settings: {e}", exc_info=True)

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
                            ON CONFLICT (instrument_token, source_list) DO NOTHING;
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


@app.get("/user/subscriptions")
def get_subscriptions(scope: Optional[str] = Query(default=None, pattern="^(sidebar|marketwatch)$")):
    """
    GET /user/subscriptions
    - If scope is provided, return {"subscriptions": settings_json.get(f"subscriptions_{scope}") or {}}
    - If no scope, return legacy {"subscriptions": settings_json.get("subscriptions") or {}}
    """
    db = SessionLocal()
    try:
        settings = get_user_settings(db)
        if scope:
            value = settings.get(f"subscriptions_{scope}") or {}
        else:
            value = settings.get("subscriptions") or {}
        return JSONResponse(content={"subscriptions": value})
    finally:
        db.close()

@app.put("/user/subscriptions")
async def put_subscriptions(
    request: Request,
    scope: Optional[str] = Query(default=None, pattern="^(sidebar|marketwatch)$")
):
    """
    PUT /user/subscriptions
    - Accepts JSON body that must contain a top-level "subscriptions" object.
    - If scope is provided, upsert into settings_json[f"subscriptions_{scope}"].
    - If no scope, upsert into legacy "subscriptions".
    """
    body = await request.json()
    subs = body.get("subscriptions")
    if subs is None or not isinstance(subs, (dict, list)):
        # Keep consistent error shape
        raise HTTPException(status_code=400, detail="Body must contain a 'subscriptions' object")

    db = SessionLocal()
    try:
        # Load full settings_json, mutate appropriate key, then persist
        settings = get_user_settings(db) or {}
        if scope:
            settings[f"subscriptions_{scope}"] = subs
        else:
            settings["subscriptions"] = subs
        update_user_settings(db, settings)
        return JSONResponse(content={"status": "ok"})
    finally:
        db.close()

@app.websocket("/broker/ws/marketwatch")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)


    # Auto-subscribe on connect (aggregate across legacy + scoped namespaces)
    db = SessionLocal()
    try:
        settings = get_user_settings(db) or {}

        def _extract_tokens_from_subs(_subs: dict) -> int:
            """
            Extract tokens from a subscriptions object using tolerant parsing.
            Returns count added into outer token_set.
            """
            if not _subs or not isinstance(_subs, dict):
                return 0
            token_count = 0
            for group in _subs.get("groups", []) or []:
                if not group or not isinstance(group, dict):
                    continue
                # Prefer explicit 'tokens' array
                if isinstance(group.get("tokens"), list):
                    for t in group["tokens"]:
                        try:
                            token_set.add(int(t))
                            token_count += 1
                        except (ValueError, TypeError):
                            pass
                # Fallback to instruments array
                elif isinstance(group.get("instruments"), list):
                    for inst in group["instruments"]:
                        if not inst or not isinstance(inst, dict):
                            continue
                        token = inst.get("instrument_token") or inst.get("token")
                        if token is None:
                            continue
                        try:
                            token_set.add(int(token))
                            token_count += 1
                        except (ValueError, TypeError):
                            pass
            return token_count

        token_set: set[int] = set()
        # Legacy
        legacy = settings.get("subscriptions")
        if isinstance(legacy, dict):
            c = _extract_tokens_from_subs(legacy)
            logging.info("[WS auto-restore] Loaded %d tokens from legacy 'subscriptions'", c)

        # Sidebar
        sb = settings.get("subscriptions_sidebar")
        if isinstance(sb, dict):
            c = _extract_tokens_from_subs(sb)
            logging.info("[WS auto-restore] Loaded %d tokens from 'subscriptions_sidebar'", c)

        # Marketwatch
        mw = settings.get("subscriptions_marketwatch")
        if isinstance(mw, dict):
            c = _extract_tokens_from_subs(mw)
            logging.info("[WS auto-restore] Loaded %d tokens from 'subscriptions_marketwatch'", c)

        all_tokens = list(token_set)
        if all_tokens:
            # Validate mode from any available section, fallback to 'quote'
            mode = None
            for candidate in (legacy, sb, mw):
                if isinstance(candidate, dict):
                    m = candidate.get("mode")
                    if m in {"ltp", "quote", "full"}:
                        mode = m
                        break
            if mode not in {"ltp", "quote", "full"}:
                mode = "quote"

            await ws_manager.subscribe(websocket, all_tokens, mode)
            logging.info("[WS auto-restore] Auto-subscribed client to %d unique tokens on connect (mode=%s).", len(all_tokens), mode)
    finally:
        db.close()

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


