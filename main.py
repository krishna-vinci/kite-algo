import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from pydantic import BaseModel

from api.config.openapi import OPENAPI_TAGS
from app.bootstrap import combined_lifespan
from app.middleware import setup_middleware

from api.routers.worker_auth import router as worker_auth_router
from api.routers.worker_execution import router as worker_execution_router
from api.routers.worker_market import router as worker_market_router
from api.routers.worker_protection import router as worker_protection_router
from api.routers.analytics import router as analytics_router
from api.routers.auth import router as auth_router
from api.routers.control import router as control_router
from api.routers.historical import router as historical_router
from api.routers.ingestion import router as ingestion_router
from api.routers.instruments import router as instruments_router
from api.routers.journal import router as journal_router
from api.routers.market_data import router as market_data_router
from api.routers.marketwatch import router as marketwatch_router
from api.routers.user_settings import router as user_settings_router
from broker_api.market.candles_api import router as candles_api_router
from broker_api.core.routes import router as broker_core_router
from broker_api.core.historical_routes import router as broker_historical_routes_router
from broker_api.instruments.routes import router as broker_instruments_router
from broker_api.mutual_funds.kite_mutual_funds import router as kite_mutual_funds_router
from broker_api.orders import router as kite_orders_router
from broker_api.performance.performance_router import router as performance_router
from options.api.execution_router import router as options_execution_router
from options.api.market_router import router as options_market_router
from options.api.protection_router import router as options_protection_router
from options.api.strategy_router import router as options_strategy_router
from options.api.worker_options_router import router as worker_options_router
from app.monitor import install_log_buffer
from strategies.indexstoploss.router import router as indexstoploss_router

load_dotenv()  # Load environment variables from .env file

# Configure logging for the main application
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
install_log_buffer()


def _validate_auth_config_at_startup() -> None:
    """Fail fast at startup if required auth secrets are missing in non-dev mode."""
    app_env = (os.getenv("APP_ENV") or "").strip().lower()
    allow_insecure = (os.getenv("APP_ALLOW_INSECURE_DEV_AUTH") or "false").strip().lower() == "true"
    is_dev = app_env == "development" and allow_insecure

    jwt_secret = os.getenv("APP_JWT_SECRET") or os.getenv("JWT_SECRET")
    if not jwt_secret and not is_dev:
        raise RuntimeError(
            "APP_JWT_SECRET is required unless APP_ENV=development and APP_ALLOW_INSECURE_DEV_AUTH=true"
        )

    has_admin = bool(
        os.getenv("APP_ADMIN_PASSWORD")
        or os.getenv("APP_ADMIN_PASSWORD_HASH")
        or os.getenv("APP_ADMIN_PASSWORD_HASH_B64")
        or os.getenv("APP_ADMIN_PASSWORD_HASH_FILE")
    )
    if not has_admin and not is_dev:
        raise RuntimeError(
            "APP_ADMIN_PASSWORD or APP_ADMIN_PASSWORD_HASH* is required unless APP_ENV=development and APP_ALLOW_INSECURE_DEV_AUTH=true"
        )

    if is_dev:
        if not jwt_secret:
            logging.warning(
                "APP_JWT_SECRET is not set; using insecure development secret because APP_ENV=development and APP_ALLOW_INSECURE_DEV_AUTH=true"
            )
        if not has_admin:
            logging.warning(
                "APP admin credentials are not configured; allowing development fallback password because APP_ENV=development and APP_ALLOW_INSECURE_DEV_AUTH=true"
            )


_validate_auth_config_at_startup()

# Suppress INFO level logs from httpx for specific API calls
logging.getLogger("httpx").setLevel(logging.WARNING)

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

app = FastAPI(title="Kite App API", lifespan=combined_lifespan, openapi_tags=OPENAPI_TAGS)
setup_middleware(app)

# 3. Include API routes under /api
app.include_router(auth_router, prefix="/api")
app.include_router(market_data_router, prefix="/api")
app.include_router(instruments_router, prefix="/api")
app.include_router(historical_router, prefix="/api")
app.include_router(ingestion_router, prefix="/api")
app.include_router(user_settings_router, prefix="/api")
app.include_router(marketwatch_router, prefix="/api")
app.include_router(worker_auth_router, prefix="/api")
app.include_router(worker_market_router, prefix="/api")
app.include_router(worker_execution_router, prefix="/api")
app.include_router(worker_protection_router, prefix="/api")
app.include_router(control_router, prefix="/api")
app.include_router(journal_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(kite_orders_router, prefix="/api")
app.include_router(kite_mutual_funds_router, prefix="/api")
app.include_router(candles_api_router, prefix="/api")  # Unified candles API with all historical endpoints
app.include_router(broker_core_router, prefix="/api")
app.include_router(broker_historical_routes_router, prefix="/api")
app.include_router(broker_instruments_router, prefix="/api")
app.include_router(performance_router, prefix="/api")
app.include_router(indexstoploss_router, prefix="/api/strategies")
app.include_router(options_market_router)
app.include_router(options_strategy_router)
app.include_router(options_execution_router)
app.include_router(options_protection_router)
app.include_router(worker_options_router)

from broker_api.broker_api import ensure_instruments_index, get_meili_client, meili_reindex_instruments

logger = logging.getLogger(__name__)
@app.get("/", tags=["System"])
async def root():
    return {"message": "Welcome to Kite App API!"}
