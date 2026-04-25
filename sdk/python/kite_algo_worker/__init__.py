"""Thin Python SDK for external Kite Algo strategy workers."""

from .client import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError
from .orders import (
    OrderBuilder,
    equity_market_order,
    limit_order,
    market_order,
    option_market_order,
    sl_m_order,
    sl_order,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AlgoWorkerConfig",
    "KiteAlgoWorkerClient",
    "KiteAlgoWorkerError",
    "OrderBuilder",
    "market_order",
    "limit_order",
    "sl_order",
    "sl_m_order",
    "option_market_order",
    "equity_market_order",
]
