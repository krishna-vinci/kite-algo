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
from typing import Dict, Any
from broker_api.redis_events import get_redis
import redis.asyncio as redis

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
from auth_service import auth_exempt_path, get_optional_app_user
from runtime_monitor import heartbeat, install_log_buffer, set_component_status, set_meta

# Configure logging for the main application
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
install_log_buffer()

# Suppress INFO level logs from httpx for specific API calls
logging.getLogger("httpx").setLevel(logging.WARNING)

import plotly.express as px
import pandas_market_calendars as mcal

from charts import charts_app
from api.openapi import OPENAPI_TAGS
from api.routers.auth import router as auth_router
from api.routers.market_data import router as market_data_router
from api.routers.instruments import router as instruments_router
from api.routers.historical import router as historical_router
from api.routers.ingestion import router as ingestion_router
from api.routers.user_settings import router as user_settings_router
from api.routers.marketwatch import router as marketwatch_router


from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from broker_api.broker_api import router as broker_api_router
from broker_api.alerts_router import router as alerts_router
from broker_api.performance_router import router as performance_router
from broker_api.options_router import router as options_router
from broker_api.candles_api import router as candles_api_router
from broker_api.kite_mutual_funds import router as kite_mutual_funds_router



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
from broker_api.kite_orders import router as kite_orders_router
from strategies.indexstoploss.router import router as indexstoploss_router

from broker_api.broker_api import get_kite
from kiteconnect import KiteConnect
from typing import List, Optional
from server import mcp
from contextlib import asynccontextmanager
import server
from broker_api.kite_auth import login_headless
import logging
from database import SessionLocal, database as async_db
from broker_api.kite_session import KiteSession, build_kite_client, get_system_access_token, make_account_id, upsert_kite_session
from broker_api.broker_api import run_headless_login_and_persist_system_token
from broker_api.kite_auth import API_KEY
from broker_api.order_runtime import order_event_runtime, realtime_positions_service, refresh_processing_stuck_rows
from broker_api.websocket_manager import WebSocketManager
from alerts.engine import AlertsEngine
from database import get_user_settings, update_user_settings
from pydantic import BaseModel
import csv

class UserSubscriptions(BaseModel):
    groups: List[dict]
    activeGroupId: Optional[str] = None

class OverlaySnapshotTick(BaseModel):
    instrument_token: int
    last_price: float
    change_percent: Optional[float] = None
    tick_timestamp: int
    server_timestamp: int
    age_ms: Optional[int] = None
    source: str

class OverlaySnapshotResponse(BaseModel):
    status: str
    data: Dict[str, OverlaySnapshotTick]

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
                        kite = build_kite_client(ks.access_token, session_id=sid)
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
    order_runtime_task = None
    set_component_status("app", "starting", detail="Application startup in progress")
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
                kite = build_kite_client(system_at, session_id="system")
                at = system_at
                try:
                    # Lightweight validation
                    profile = await asyncio.to_thread(kite.profile)
                    broker_user_id = str((profile or {}).get("user_id") or "").strip() or None
                    if broker_user_id:
                        upsert_kite_session(db, "system", at, broker_user_id=broker_user_id)
                        db.commit()
                    logging.info("Using system access_token from DB (..%s)", at[-6:] if isinstance(at, str) else "")
                    set_meta("daily_broker_login", {
                        "mode": "startup_existing_token",
                        "last_success_at": datetime.utcnow().isoformat(),
                        "token_suffix": at[-6:] if isinstance(at, str) else "",
                        "status": "healthy",
                    })
                except Exception as e:
                    logging.warning("System token validation failed (..%s); performing headless login: %s", (at[-6:] if isinstance(at, str) else ""), e)
                    _kite, at = login_headless()
                    kite = build_kite_client(at, session_id="system")
                    profile = await asyncio.to_thread(kite.profile)
                    broker_user_id = str((profile or {}).get("user_id") or "").strip() or None
                    upsert_kite_session(db, "system", at, broker_user_id=broker_user_id)
                    db.commit()
                    logging.info("Refreshed system access_token via headless login (..%s)", at[-6:] if isinstance(at, str) else "")
                    set_meta("daily_broker_login", {
                        "mode": "startup_refresh",
                        "last_success_at": datetime.utcnow().isoformat(),
                        "token_suffix": at[-6:] if isinstance(at, str) else "",
                        "status": "healthy",
                    })
            else:
                # No system token; perform headless login and persist
                _kite, at = login_headless()
                kite = build_kite_client(at, session_id="system")
                profile = await asyncio.to_thread(kite.profile)
                broker_user_id = str((profile or {}).get("user_id") or "").strip() or None
                upsert_kite_session(db, "system", at, broker_user_id=broker_user_id)
                db.commit()
                logging.info("Obtained system access_token via headless login (..%s)", at[-6:] if isinstance(at, str) else "")
                set_meta("daily_broker_login", {
                    "mode": "startup_new_login",
                    "last_success_at": datetime.utcnow().isoformat(),
                    "token_suffix": at[-6:] if isinstance(at, str) else "",
                    "status": "healthy",
                })
        finally:
            try:
                if db:
                    db.close()
            except Exception:
                pass

        server.mcp_kite_instance = kite
        logging.info("MCP Kite instance initialized successfully.")

        # Ensure async DB is connected (required for Meilisearch reindex and other async ops)
        try:
            # Check if 'is_connected' property exists (databases < 0.8.0) or just connect
            # 'databases' library usually handles idempotency of connect()
            if not async_db.is_connected:
                await async_db.connect()
                logging.info("Async database connected.")
        except Exception as e:
             logging.error(f"Failed to connect to async database: {e}")

        # Initialize and start the WebSocketManager with the selected token
        logging.info("Initializing WebSocketManager...")
        # Get the main event loop to pass to the WebSocketManager
        main_event_loop = asyncio.get_running_loop()
        ws_manager = WebSocketManager(api_key=API_KEY, access_token=at, main_event_loop=main_event_loop)
        
        # Inject real-time positions service into WebSocket manager
        ws_manager.realtime_positions_service = realtime_positions_service
        logging.info("Real-time positions service injected into WebSocketManager")
        
        ws_manager.start()
        logging.info("WebSocketManager started.")
        set_component_status("websocket_manager", "healthy", detail="WebSocket manager started")
        # Expose ws_manager for routers to access latest ticks
        app.state.ws_manager = ws_manager

        async def _order_runtime_worker():
            poll_seconds = max(1.0, float(os.getenv("ORDER_RUNTIME_POLL_SECONDS", "1.0")))
            reconcile_seconds = max(15.0, float(os.getenv("POSITIONS_RECONCILE_SECONDS", "30")))
            last_reconcile_monotonic = 0.0
            cached_token = at
            kite_client = build_kite_client(cached_token, session_id="system")
            set_component_status("order_runtime_worker", "healthy", detail="Order runtime worker started")
            await refresh_processing_stuck_rows()
            while True:
                try:
                    await asyncio.sleep(poll_seconds)
                    db = SessionLocal()
                    try:
                        current_token = get_system_access_token(db) or cached_token
                        system_session = db.query(KiteSession).filter_by(session_id="system").first()
                        broker_user_id = getattr(system_session, "broker_user_id", None)
                    finally:
                        db.close()

                    if current_token != cached_token:
                        kite_client = build_kite_client(current_token, session_id="system")
                        cached_token = current_token

                    processed = await order_event_runtime.process_pending_events(batch_size=100)
                    synced = await order_event_runtime.sync_dirty_orders(kite_client, realtime_positions_service, batch_size=25)

                    now_monotonic = asyncio.get_running_loop().time()
                    account_id = make_account_id(broker_user_id)
                    if account_id and (now_monotonic - last_reconcile_monotonic) >= reconcile_seconds:
                        await realtime_positions_service.reconcile_account_positions(kite_client, account_id, corr_id="periodic_reconcile")
                        last_reconcile_monotonic = now_monotonic

                    heartbeat(
                        "order_runtime_worker",
                        detail="Processed canonical order events and synced dirty orders",
                        meta={
                            "processed_events": processed,
                            "synced_orders": synced,
                            "poll_seconds": poll_seconds,
                            "reconcile_seconds": reconcile_seconds,
                            "account_id": account_id,
                        },
                    )
                except asyncio.CancelledError:
                    set_component_status("order_runtime_worker", "stopped", detail="Order runtime worker cancelled")
                    break
                except Exception as exc:
                    logging.error("Order runtime worker error: %s", exc, exc_info=True)
                    set_component_status("order_runtime_worker", "degraded", detail=str(exc))

        order_runtime_task = asyncio.create_task(_order_runtime_worker())

        # Start background token watcher to rotate WS token when DB 'system' token changes
        async def _system_token_watcher():
            poll_seconds = int(os.getenv("SYSTEM_TOKEN_POLL_SEC", "45"))
            last_token = at
            set_component_status("system_token_watcher", "healthy", detail="Watching for system token changes")
            while True:
                try:
                    await asyncio.sleep(max(30, min(poll_seconds, 60)))
                    heartbeat("system_token_watcher", detail="Polling for token changes", meta={"poll_seconds": poll_seconds})
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
                        server.mcp_kite_instance = build_kite_client(new_token, session_id="system")
                        aggregator = getattr(app.state, "candle_aggregator", None)
                        if aggregator and getattr(aggregator, "running", False):
                            await aggregator.stop()
                            await aggregator.start(
                                access_token=new_token,
                                intervals=list(getattr(aggregator, "intervals", []) or []),
                                owner_scope=getattr(aggregator, "owner_scope", "all"),
                                refresh_seconds=getattr(aggregator, "refresh_seconds", 30),
                            )
                        set_component_status("websocket_manager", "healthy", detail="WebSocket token rotated", meta={"token_suffix": new_fp})
                        last_token = new_token
                except asyncio.CancelledError:
                    set_component_status("system_token_watcher", "stopped", detail="Token watcher cancelled")
                    break
                except Exception as e:
                    logging.error("Token watcher error: %s", e, exc_info=True)
                    set_component_status("system_token_watcher", "degraded", detail=str(e))
                    # Continue watching
                    continue

        token_watcher_task = asyncio.create_task(_system_token_watcher())
        # Initialize daily token gate in app state and start scheduler
        app.state.daily_token_ready = daily_token_ready
        if not daily_token_ready.is_set():
            daily_token_ready.set()
        logging.info("[GATE] Initialized and open at startup (will close at next 07:31 IST)")
        set_meta("daily_token_gate", {"ready": True, "last_changed_at": datetime.utcnow().isoformat()})
        scheduler_task = asyncio.create_task(daily_token_scheduler())

        # Start AlertsEngine (Phase 0) after WS manager and async DB are ready
        try:
            alerts_engine = AlertsEngine(async_db, ws_manager, app)
            alerts_engine.start()
            app.state.alerts_engine = alerts_engine
            logging.info("AlertsEngine started (interval_ms=%s)", getattr(alerts_engine, "interval_ms", None))
            set_component_status("alerts_engine", "healthy", detail="Alerts engine started")
        except Exception as e:
            logging.error("Failed to start AlertsEngine: %s", e, exc_info=True)
            set_component_status("alerts_engine", "degraded", detail=str(e))
        
        # Start PositionProtectionEngine (Phase 1) after AlertsEngine
        try:
            from strategies.indexstoploss.index_stoploss_algo import PositionProtectionEngine
            from broker_api.kite_orders import OrdersService
            
            orders_service = OrdersService()
            protection_engine = PositionProtectionEngine(
                db=async_db,
                ws_manager=ws_manager,
                orders_service=orders_service,
                app=app
            )
            protection_engine.start()
            app.state.protection_engine = protection_engine
            logging.info("PositionProtectionEngine started (500ms interval)")
            set_component_status("protection_engine", "healthy", detail="Position protection engine started")
        except Exception as e:
            logging.error("Failed to start PositionProtectionEngine: %s", e, exc_info=True)
            set_component_status("protection_engine", "degraded", detail=str(e))
        
        # Initialize Phase 3: StrikeSelector and PositionBuilder
        try:
            from strategies.strike_selector import StrikeSelector, PositionBuilder
            from broker_api.instruments_repository import InstrumentsRepository
            
            # Get OptionsSessionManager from app state
            osm = getattr(app.state, "options_session_manager", None)
            if osm:
                db_session = SessionLocal()
                instruments_repo = InstrumentsRepository(db=db_session)
                
                strike_selector = StrikeSelector(osm, instruments_repo)
                position_builder = PositionBuilder(strike_selector, instruments_repo)
                
                app.state.strike_selector = strike_selector
                app.state.position_builder = position_builder
                app.state.phase3_db_session = db_session
                logging.info("Phase 3: StrikeSelector and PositionBuilder initialized")
            else:
                logging.warning("OptionsSessionManager not available, Phase 3 components not initialized")
        except Exception as e:
            logging.error("Failed to initialize Phase 3 components: %s", e, exc_info=True)

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
                # Handle both dict (older versions) and IndexStats object (newer versions)
                if isinstance(stats, dict):
                    num_docs = (stats.get("numberOfDocuments") or stats.get("number_of_documents") or 0)
                else:
                    # Try camelCase first then snake_case attributes
                    num_docs = getattr(stats, "numberOfDocuments", getattr(stats, "number_of_documents", 0))

                if int(num_docs) == 0:
                    logger.info("Meilisearch 'instruments' index is empty; triggering bootstrap reindex...")
                    await meili_reindex_instruments()
            except Exception as ie:
                logger.exception("Startup Meilisearch reindex-if-empty check failed: %s", ie)
        except Exception as e:
            logger.exception("Failed to ensure Meilisearch index on startup: %s", e)

        # Auto-start Candle Aggregator with all supported intervals
        try:
            from broker_api.candle_aggregator import get_aggregator
            logging.info("Starting Candle Aggregator...")
            aggregator = get_aggregator(API_KEY)
            
            if not aggregator.running:
                # Start with ALL supported intervals including 3minute, 30minute, and day
                await aggregator.start(
                    access_token=at,
                    intervals=["minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"],
                    owner_scope="all",
                    refresh_seconds=30
                )
                logging.info("Candle Aggregator started successfully with all intervals")
                app.state.candle_aggregator = aggregator
                set_component_status("candle_aggregator", "healthy", detail="Candle aggregator started")
            else:
                logging.info("Candle Aggregator already running")
                set_component_status("candle_aggregator", "healthy", detail="Candle aggregator already running")
        except Exception as e:
            logging.error("Failed to start Candle Aggregator: %s", e, exc_info=True)
            set_component_status("candle_aggregator", "degraded", detail=str(e))
    except Exception as e:
        logging.error(f"Failed to initialize MCP Kite instance or WebSocketManager: {e}", exc_info=True)
        set_meta("daily_broker_login", {
            "status": "degraded",
            "last_error": str(e),
            "last_failure_at": datetime.utcnow().isoformat(),
        })
        # Depending on the desired behavior, you might want to exit the application
        # or proceed without a valid Kite instance for MCP.
        # For now, we'll log the error and continue.
        server.mcp_kite_instance = None

    set_component_status("app", "healthy", detail="Application startup complete")

    async with mcp_app.lifespan(app):
        yield
    
    # Cleanup on shutdown
    # Cancel token watcher first
    set_component_status("app", "stopping", detail="Application shutdown in progress")
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
    # Cancel order runtime worker
    try:
        if 'order_runtime_task' in locals() and order_runtime_task:
            order_runtime_task.cancel()
            try:
                await order_runtime_task
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
            set_component_status("alerts_engine", "stopped", detail="Alerts engine stopped")
    except Exception:
        pass
    
    # Stop PositionProtectionEngine
    try:
        protection_eng = getattr(app.state, "protection_engine", None)
        if protection_eng:
            await protection_eng.stop()
            logging.info("PositionProtectionEngine stopped.")
            set_component_status("protection_engine", "stopped", detail="Position protection engine stopped")
    except Exception:
        pass

    try:
        phase3_db_session = getattr(app.state, "phase3_db_session", None)
        if phase3_db_session:
            phase3_db_session.close()
            app.state.phase3_db_session = None
    except Exception:
        pass

    # Stop Candle Aggregator
    try:
        aggregator = getattr(app.state, "candle_aggregator", None)
        if aggregator and aggregator.running:
            logging.info("Stopping Candle Aggregator...")
            await aggregator.stop()
            logging.info("Candle Aggregator stopped.")
            set_component_status("candle_aggregator", "stopped", detail="Candle aggregator stopped")
    except Exception as e:
        logging.error("Error stopping Candle Aggregator: %s", e, exc_info=True)

    if ws_manager:
        logging.info("Stopping WebSocketManager...")
        ws_manager.stop()
        logging.info("WebSocketManager stopped.")
        set_component_status("websocket_manager", "stopped", detail="WebSocket manager stopped")

    set_component_status("app", "stopped", detail="Application shutdown complete")


app = FastAPI(title="Kite App API", lifespan=combined_lifespan, openapi_tags=OPENAPI_TAGS)

# 3. Mount the MCP app at a subpath so normal FastAPI routes remain reachable
# Final MCP endpoint will be available at /llm/mcp (since mcp_app was created with path='/mcp')
app.mount("/llm", mcp_app_wrapped)

# 3b. Also expose MCP directly at /mcp for clients expecting the legacy path
# For this mount, set the MCP ASGI app path to "/" so the full endpoint is exactly "/mcp"
mcp_app_direct = mcp.http_app(path="/")
mcp_app_direct_wrapped = MCPAuthWrapper(mcp_app_direct)
app.mount("/mcp", mcp_app_direct_wrapped)

# 4. Include API routes under /api
app.include_router(auth_router, prefix="/api")
app.include_router(market_data_router, prefix="/api")
app.include_router(instruments_router, prefix="/api")
app.include_router(historical_router, prefix="/api")
app.include_router(ingestion_router, prefix="/api")
app.include_router(user_settings_router, prefix="/api")
app.include_router(marketwatch_router, prefix="/api")
app.include_router(kite_orders_router, prefix="/api")
app.include_router(kite_mutual_funds_router, prefix="/api")
app.include_router(options_router, prefix="/api")
app.include_router(candles_api_router, prefix="/api")  # Unified candles API with all historical endpoints
app.include_router(performance_router, prefix="/api")
app.include_router(momentum_router, prefix="/api")
app.include_router(alerts_router, prefix="/api/alerts")
app.include_router(indexstoploss_router, prefix="/api/strategies")

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

# Add CORS middleware for frontend (production: single allowed origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://kite.krishna.quest"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def app_auth_guard(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api") or auth_exempt_path(path):
        return await call_next(request)

    user = get_optional_app_user(request)
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "App authentication required"})

    request.state.app_user = user
    return await call_next(request)

@app.get("/", tags=["System"])
async def root():
    return {"message": "Welcome to Kite App API!"}

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
    set_component_status("daily_token_scheduler", "healthy", detail="Daily token scheduler started")
    while True:
        try:
            now = datetime.now(tz)
            next_run = now.replace(hour=7, minute=31, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            sleep_sec = max(1, int((next_run - now).total_seconds()))
            logging.info("[SCHED] Next daily headless login scheduled at %s", next_run.strftime("%Y-%m-%d %H:%M:%S %Z%z"))
            set_meta("daily_token_scheduler", {
                "next_run": next_run.isoformat(),
                "sleep_seconds": sleep_sec,
                "last_heartbeat": datetime.utcnow().isoformat(),
            })
            heartbeat("daily_token_scheduler", detail="Scheduler sleeping until next run", meta={"next_run": next_run.isoformat()})
            await asyncio.sleep(sleep_sec)

            # Begin rotation
            logging.info("[SCHED] 07:31 IST reached; clearing gate and refreshing system token")
            daily_token_ready.clear()
            set_meta("daily_token_gate", {"ready": False, "last_changed_at": datetime.utcnow().isoformat()})
            set_component_status("daily_token_scheduler", "running", detail="Refreshing daily system token")

            # Retry loop until success
            retry_count = 0
            while True:
                try:
                    retry_count += 1
                    heartbeat("daily_token_scheduler", detail="Attempting headless broker login", meta={"attempt": retry_count})
                    db = SessionLocal()
                    try:
                        fp = run_headless_login_and_persist_system_token(db)
                        db.commit()
                    finally:
                        db.close()
                    logging.info("[SCHED] System access_token rotated (..%s)", fp)
                    set_meta("daily_broker_login", {
                        "mode": "daily_scheduler",
                        "status": "healthy",
                        "last_success_at": datetime.utcnow().isoformat(),
                        "attempts": retry_count,
                        "token_suffix": fp,
                    })
                    break
                except Exception as e:
                    logging.warning("[SCHED] Headless login failed: %s; retrying in 30s", e)
                    set_component_status("daily_token_scheduler", "degraded", detail=f"Headless login failed: {e}", meta={"attempt": retry_count})
                    set_meta("daily_broker_login", {
                        "mode": "daily_scheduler",
                        "status": "degraded",
                        "last_error": str(e),
                        "last_failure_at": datetime.utcnow().isoformat(),
                        "attempts": retry_count,
                    })
                    await asyncio.sleep(30)

            # Open gate
            daily_token_ready.set()
            logging.info("[GATE] Opened after successful token refresh")
            set_meta("daily_token_gate", {"ready": True, "last_changed_at": datetime.utcnow().isoformat()})
            set_component_status("daily_token_scheduler", "healthy", detail="Daily token refresh completed")

            # Kick off dependent daily jobs (fire-and-forget)
            # No dependent jobs for token refresh; other schedulers handle their own updates.

        except asyncio.CancelledError:
            logging.info("[SCHED] Daily token scheduler cancelled")
            if not daily_token_ready.is_set():
                daily_token_ready.set()
                set_meta("daily_token_gate", {"ready": True, "last_changed_at": datetime.utcnow().isoformat()})
            set_component_status("daily_token_scheduler", "stopped", detail="Daily token scheduler cancelled")
            break
        except Exception as e:
            logging.error("[SCHED] Scheduler loop error: %s", e, exc_info=True)
            set_component_status("daily_token_scheduler", "degraded", detail=str(e))
            await asyncio.sleep(30)
