"""Thin Python SDK for external Kite Algo strategy workers."""

from .async_client import AsyncKiteAlgoWorkerClient
from .client import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError
from .exceptions import AuthError, BrokerValidationError, PermissionDeniedError, StreamDisconnectedError
from .helpers import amo_limit_order, ensure_run, live_equity_market_order, wait_for_history
from .models import CostContract, OrderPreview, PreviewPayload, RunProtectionState, WorkerOrderResult, WorkerOrdersResponse, WorkerTradesResponse
from .orders import OrderBuilder, equity_market_order, limit_order, market_order, option_market_order, sl_m_order, sl_order
from .protection import BackendProtection, BasketProtection, OperationalProtection, ProtectedPosition
from .ws import WorkerCandleWebSocketClient, WorkerRunPnlWebSocketClient, WorkerTickWebSocketClient, WorkerWebSocketClient

__version__ = "0.5.0"

__all__ = [
    "__version__",
    "AlgoWorkerConfig",
    "AsyncKiteAlgoWorkerClient",
    "AuthError",
    "amo_limit_order",
    "BackendProtection",
    "BasketProtection",
    "BrokerValidationError",
    "CostContract",
    "ensure_run",
    "equity_market_order",
    "KiteAlgoWorkerClient",
    "KiteAlgoWorkerError",
    "live_equity_market_order",
    "limit_order",
    "market_order",
    "option_market_order",
    "OrderBuilder",
    "OrderPreview",
    "PermissionDeniedError",
    "PreviewPayload",
    "ProtectedPosition",
    "OperationalProtection",
    "RunProtectionState",
    "sl_order",
    "sl_m_order",
    "StreamDisconnectedError",
    "wait_for_history",
    "WorkerCandleWebSocketClient",
    "WorkerOrderResult",
    "WorkerOrdersResponse",
    "WorkerRunPnlWebSocketClient",
    "WorkerTickWebSocketClient",
    "WorkerWebSocketClient",
    "WorkerTradesResponse",
]
