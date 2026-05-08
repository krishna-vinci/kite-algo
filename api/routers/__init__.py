# Auto-discovered routers — add new routers here
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

# Also import broker_api and strategy routers
from broker_api.orders.routes import router as orders_router
from broker_api.core.routes import router as broker_core_router
from options.api.worker_options_router import router as worker_options_router

ALL_ROUTERS = [
    (auth_router, "/api"),
    (market_data_router, "/api"),
    (instruments_router, "/api"),
    (historical_router, "/api"),
    (ingestion_router, "/api"),
    (user_settings_router, "/api"),
    (marketwatch_router, "/api"),
    (worker_auth_router, "/api"),
    (worker_market_router, "/api"),
    (worker_execution_router, "/api"),
    (worker_protection_router, "/api"),
    (control_router, "/api"),
    (journal_router, "/api"),
    (analytics_router, "/api"),
    (orders_router, "/api"),
    (broker_core_router, "/api"),
    (worker_options_router, ""),
]
