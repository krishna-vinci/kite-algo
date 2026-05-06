"""Thin Python SDK for external Kite Algo strategy workers."""

from importlib.metadata import PackageNotFoundError, version as _package_version
from pathlib import Path
import re

from . import options
from .async_client import AsyncKiteAlgoWorkerClient
from .client import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError
from .exceptions import AuthError, BrokerValidationError, PermissionDeniedError, StreamDisconnectedError
from .helpers import (
    amo_limit_order,
    ensure_run,
    live_equity_market_order,
    preview_then_place_order,
    wait_for_fresh_candle,
    wait_for_history,
    wait_for_quotes,
    wait_for_terminal_order_state,
    warmup_history,
)
from .indicators import BaseIndicator, IndicatorInput, IndicatorValue, LiveIndicatorEngine, NUMBA_AVAILABLE, TechnicalAnalysis, crossover, format_output, njit, normalize_input, sma, ta
from .models import (
    CostContract,
    WorkerCandle,
    WorkerHistoricalCandles,
    OrderPreview,
    PreviewPayload,
    RunProtectionState,
    SafetyCheckResult,
    WorkerOrderSnapshot,
    WorkerFundsSegment,
    WorkerFundsSnapshot,
    WorkerOrderResult,
    WorkerOrdersResponse,
    WorkerRunPnlLeg,
    WorkerRunPnlSnapshot,
    WorkerRunPnlTotals,
    WorkerTradeSnapshot,
    WorkerTradesResponse,
)
from .orders import OrderBuilder, equity_market_order, limit_order, market_order, option_market_order, sl_m_order, sl_order
from .options import OptionEntryPreviewRequest, OptionExpirySnapshot, OptionWorkerClient, option_leg
from .protection import BackendProtection, BasketProtection, OperationalProtection, ProtectedPosition
from .ws import StreamHealth, WorkerCandleWebSocketClient, WorkerRunPnlWebSocketClient, WorkerTickWebSocketClient, WorkerWebSocketClient

_MARKETDATA_AVAILABLE = False

try:
    from .marketdata import OhlcvArrays as _OhlcvArrays, candles_to_df as _candles_to_df, ohlcv_arrays as _ohlcv_arrays

    OhlcvArrays = _OhlcvArrays  # type: ignore[assignment]
    candles_to_df = _candles_to_df  # type: ignore[assignment]
    ohlcv_arrays = _ohlcv_arrays  # type: ignore[assignment]
    _MARKETDATA_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name not in {"numpy", "pandas"}:
        raise

    class OhlcvArrays:  # type: ignore[no-redef]
        pass

    def candles_to_df(*_args, **_kwargs):  # type: ignore[no-redef]
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker marketdata helpers")

    def ohlcv_arrays(*_args, **_kwargs):  # type: ignore[no-redef]
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker marketdata helpers")


def _resolve_version() -> str:
    try:
        return _package_version("kite-algo-worker")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        try:
            match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
        except OSError:
            match = None
        return match.group(1) if match else "0+unknown"


__version__ = _resolve_version()

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
    "BaseIndicator",
    "IndicatorInput",
    "IndicatorValue",
    "LiveIndicatorEngine",
    "NUMBA_AVAILABLE",
    "WorkerCandle",
    "WorkerHistoricalCandles",
    "ensure_run",
    "equity_market_order",
    "KiteAlgoWorkerClient",
    "KiteAlgoWorkerError",
    "live_equity_market_order",
    "limit_order",
    "market_order",
    "TechnicalAnalysis",
    "option_market_order",
    "OrderBuilder",
    "OptionEntryPreviewRequest",
    "OptionExpirySnapshot",
    "OptionWorkerClient",
    "OrderPreview",
    "PermissionDeniedError",
    "PreviewPayload",
    "preview_then_place_order",
    "ProtectedPosition",
    "OperationalProtection",
    "RunProtectionState",
    "SafetyCheckResult",
    "WorkerOrderSnapshot",
    "sl_order",
    "sl_m_order",
    "StreamDisconnectedError",
    "StreamHealth",
    "crossover",
    "format_output",
    "njit",
    "normalize_input",
    "option_leg",
    "options",
    "sma",
    "ta",
    "wait_for_history",
    "wait_for_quotes",
    "wait_for_terminal_order_state",
    "wait_for_fresh_candle",
    "warmup_history",
    "WorkerCandleWebSocketClient",
    "WorkerFundsSegment",
    "WorkerFundsSnapshot",
    "WorkerOrderResult",
    "WorkerOrdersResponse",
    "WorkerRunPnlLeg",
    "WorkerRunPnlSnapshot",
    "WorkerRunPnlTotals",
    "WorkerTradeSnapshot",
    "WorkerRunPnlWebSocketClient",
    "WorkerTickWebSocketClient",
    "WorkerWebSocketClient",
    "WorkerTradesResponse",
]

if _MARKETDATA_AVAILABLE:
    __all__.extend(["OhlcvArrays", "candles_to_df", "ohlcv_arrays"])
