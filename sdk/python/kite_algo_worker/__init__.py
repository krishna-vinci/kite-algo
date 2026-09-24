"""Thin Python SDK for external Kite Algo strategy workers.

Only the HTTP client stack imports eagerly.  Everything that needs optional
numerical dependencies (``pandas``/``numpy``/``numba`` via ``indicators`` and
``marketdata``) resolves lazily through module ``__getattr__``, so embedding
processes such as the MCP adapter stay light until they actually touch the
heavy surfaces.
"""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version as _package_version
from pathlib import Path
import re

from .async_client import AsyncKiteAlgoWorkerClient
from .client import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError
from .exceptions import (
    AuthError,
    BrokerValidationError,
    CalendarRangeUncoveredError,
    PermissionDeniedError,
    StreamDisconnectedError,
    UnsupportedSchemaVersionError,
    WorkerDataUnavailableError,
)

_LAZY_EXPORTS: dict[str, str] = {}


def _lazy(module: str, *names: str) -> None:
    for name in names:
        _LAZY_EXPORTS[name] = module


_lazy("fundamentals", "FundamentalFeatureRow", "FundamentalFeatures", "FundamentalsEnvelope", "FundamentalsStatus", "FundamentalsStatements", "FundamentalsSymbolStatus", "FundamentalsSyncRun")
_lazy("helpers", "amo_limit_order", "amo_market_order", "ensure_run", "live_equity_market_order", "preview_then_place_order", "wait_for_fresh_candle", "wait_for_history", "wait_for_quotes", "wait_for_terminal_order_state", "warmup_history")
_lazy("investment", "WorkerAccountPortfolioSnapshot", "WorkerCalendarSession", "WorkerIndexConstituentStatus", "WorkerIndexConstituentsSnapshot", "WorkerIndexMember", "WorkerMarketCalendarSnapshot", "WorkerMarketCalendarStatus", "WorkerPortfolioHolding", "WorkerPortfolioPosition", "WorkerSourceEnvelope")
_lazy("managed_run", "ManagedRun")
_lazy("indicators", "BaseIndicator", "IndicatorInput", "IndicatorValue", "LiveIndicatorEngine", "NUMBA_AVAILABLE", "TechnicalAnalysis", "crossover", "format_output", "njit", "normalize_input", "sma", "ta")
_lazy("models", "CostContract", "ItemizedCharges", "WorkerBasketExecution", "WorkerBasketExecutionLeg", "WorkerBasketExecutionsResponse", "WorkerBracketActionResult", "WorkerBracketIntent", "WorkerBracketListResponse", "WorkerCandle", "WorkerExecutionEvent", "WorkerExecutionEventsResponse", "WorkerHistoricalCandles", "OrderPreview", "PreviewPayload", "RunProtectionState", "SafetyCheckResult", "WorkerGttTrigger", "WorkerGttWriteResult", "WorkerOrderHistoryEvent", "WorkerOrderHistoryResponse", "WorkerOrderSnapshot", "WorkerFundsSegment", "WorkerFundsSnapshot", "WorkerOrderResult", "WorkerOrdersResponse", "WorkerRunHealthSnapshot", "WorkerRunPnlLeg", "WorkerRunPnlSnapshot", "WorkerRunPnlTotals", "WorkerTimelineEvent", "WorkerTimelineResponse", "WorkerTradeSnapshot", "WorkerTradesResponse")
_lazy("orders", "OrderBuilder", "equity_market_order", "limit_order", "market_order", "option_market_order", "sl_m_order", "sl_order")
_lazy("options", "AsyncOptionWorkerClient", "OptionEntryPreviewRequest", "OptionExpirySnapshot", "OptionWorkerClient", "SpreadLegSelection", "SpreadSpec", "option_leg", "resolve_delta_leg", "resolve_offset_leg", "resolve_option_contracts", "resolve_option_leg", "resolve_spread")
_lazy("protection", "BackendProtection", "BasketProtection", "OperationalProtection", "ProtectedPosition")
_lazy("readiness", "ReadinessCheck", "RunnerPackage", "RunnerProfile", "SourceEntrypoint", "SourceImports", "SourceReadiness")
_lazy("run_config", "RunConfig")
_lazy("ws", "StreamHealth", "WorkerCandleWebSocketClient", "WorkerRunPnlWebSocketClient", "WorkerTickWebSocketClient", "WorkerWebSocketClient")

_MARKETDATA_NAMES = ("OhlcvArrays", "candles_to_df", "ohlcv_arrays")
_MARKETDATA_MODULE = None
_MARKETDATA_RESOLVED = False


class OhlcvArraysStub:
    """Placeholder type for ``OhlcvArrays`` when pandas/numpy are unavailable."""


def _missing_marketdata_helper(*_args, **_kwargs):
    raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker marketdata helpers")


def _load_marketdata():
    global _MARKETDATA_MODULE, _MARKETDATA_RESOLVED
    if not _MARKETDATA_RESOLVED:
        try:
            _MARKETDATA_MODULE = import_module(".marketdata", __name__)
        except ModuleNotFoundError as exc:
            if exc.name not in {"numpy", "pandas"}:
                raise
        _MARKETDATA_RESOLVED = True
    return _MARKETDATA_MODULE, _MARKETDATA_MODULE is not None


def __getattr__(name: str):
    if name == "_MARKETDATA_AVAILABLE":
        globals()[name] = _load_marketdata()[1]
        return globals()[name]
    if name in _MARKETDATA_NAMES:
        module, _available = _load_marketdata()
        if module is not None:
            value = getattr(module, name)
        elif name == "OhlcvArrays":
            value = OhlcvArraysStub
        else:
            value = _missing_marketdata_helper
        globals()[name] = value
        return value
    if name == "options":
        module = import_module(".options", __name__)
        globals()[name] = module
        return module
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | set(_MARKETDATA_NAMES) | {"options"})


def _resolve_version() -> str:
    # Prefer the source checkout's canonical metadata so tests and local
    # tooling do not report a stale editable-install version after a bump.
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    except OSError:
        match = None
    if match:
        return match.group(1)
    try:
        return _package_version("kite-algo-worker")
    except PackageNotFoundError:
        return match.group(1) if match else "0+unknown"


__version__ = _resolve_version()

__all__ = [
    "__version__",
    "AlgoWorkerConfig",
    "AsyncKiteAlgoWorkerClient",
    "AsyncOptionWorkerClient",
    "AuthError",
    "amo_limit_order",
    "amo_market_order",
    "BackendProtection",
    "BasketProtection",
    "WorkerBasketExecution",
    "WorkerBasketExecutionLeg",
    "WorkerBasketExecutionsResponse",
    "WorkerBracketActionResult",
    "WorkerBracketIntent",
    "WorkerBracketListResponse",
    "BrokerValidationError",
    "CalendarRangeUncoveredError",
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
    "FundamentalFeatureRow",
    "FundamentalFeatures",
    "FundamentalsEnvelope",
    "FundamentalsStatus",
    "FundamentalsStatements",
    "FundamentalsSymbolStatus",
    "FundamentalsSyncRun",
    "ItemizedCharges",
    "ManagedRun",
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
    "RunConfig",
    "RunProtectionState",
    "SafetyCheckResult",
    "SpreadLegSelection",
    "SpreadSpec",
    "WorkerOrderSnapshot",
    "WorkerOrderHistoryEvent",
    "WorkerOrderHistoryResponse",
    "sl_order",
    "sl_m_order",
    "StreamDisconnectedError",
    "StreamHealth",
    "UnsupportedSchemaVersionError",
    "WorkerDataUnavailableError",
    "crossover",
    "format_output",
    "njit",
    "normalize_input",
    "option_leg",
    "options",
    "resolve_option_contracts",
    "resolve_option_leg",
    "resolve_offset_leg",
    "resolve_delta_leg",
    "resolve_spread",
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
    "WorkerGttTrigger",
    "WorkerGttWriteResult",
    "WorkerOrderResult",
    "WorkerOrdersResponse",
    "WorkerRunHealthSnapshot",
    "WorkerRunPnlLeg",
    "WorkerRunPnlSnapshot",
    "WorkerRunPnlTotals",
    "WorkerTimelineEvent",
    "WorkerTimelineResponse",
    "WorkerExecutionEvent",
    "WorkerExecutionEventsResponse",
    "WorkerTradeSnapshot",
    "WorkerRunPnlWebSocketClient",
    "WorkerTickWebSocketClient",
    "WorkerWebSocketClient",
    "WorkerTradesResponse",
    "WorkerAccountPortfolioSnapshot",
    "WorkerCalendarSession",
    "WorkerIndexConstituentStatus",
    "WorkerIndexConstituentsSnapshot",
    "WorkerIndexMember",
    "WorkerMarketCalendarSnapshot",
    "WorkerMarketCalendarStatus",
    "WorkerPortfolioHolding",
    "WorkerPortfolioPosition",
    "WorkerSourceEnvelope",
    "OhlcvArrays",
    "candles_to_df",
    "ohlcv_arrays",
]
