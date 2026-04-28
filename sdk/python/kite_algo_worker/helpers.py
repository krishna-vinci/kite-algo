from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from .client import KiteAlgoWorkerClient
from .exceptions import KiteAlgoWorkerError
from .orders import equity_market_order, limit_order


def live_equity_market_order(
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    *,
    product: str = "CNC",
    exchange: str = "NSE",
    market_protection: int = -1,
    **kwargs: Any,
):
    return equity_market_order(
        tradingsymbol,
        transaction_type,
        quantity,
        product=product,
        exchange=exchange,
        market_protection=market_protection,
        **kwargs,
    )


def amo_limit_order(
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    product: str,
    quantity: int,
    price: float,
    **kwargs: Any,
):
    return limit_order(exchange, tradingsymbol, transaction_type, product, quantity, price, variety="amo", **kwargs)


def wait_for_history(
    client: KiteAlgoWorkerClient,
    instrument: str | int,
    *,
    timeframe: str = "day",
    attempts: int = 10,
    sleep_seconds: float = 1.0,
    **kwargs: Any,
):
    last: dict[str, Any] = {}
    for _ in range(attempts):
        last = client.get_historical_candles(instrument, timeframe=timeframe, **kwargs)
        if last.get("candles"):
            return last
        time.sleep(sleep_seconds)
    return last


def wait_for_quotes(
    client: KiteAlgoWorkerClient,
    instruments: list[str | int],
    *,
    mode: str = "quote",
    attempts: int = 10,
    sleep_seconds: float = 1.0,
):
    last: dict[str, Any] = {}
    for _ in range(attempts):
        last = client.get_quotes(instruments, mode=mode)
        if last.get("quotes"):
            return last
        time.sleep(sleep_seconds)
    return last


def ensure_run(
    client: KiteAlgoWorkerClient,
    *,
    strategy_run_id: str,
    template_id: str,
    account_scope: str,
    execution_mode: str,
    metadata: Mapping[str, Any],
):
    try:
        return client.get_run(strategy_run_id)
    except KiteAlgoWorkerError as exc:
        if exc.status_code != 404:
            raise
        return client.create_run(
            strategy_run_id=strategy_run_id,
            template_id=template_id,
            account_scope=account_scope,
            execution_mode=execution_mode,
            metadata=metadata,
        )


def preview_then_place_order(
    client: KiteAlgoWorkerClient,
    strategy_run_id: str,
    order: Mapping[str, Any],
    *,
    idempotency_key: str,
    metadata: Optional[Mapping[str, Any]] = None,
):
    client.preview_order(strategy_run_id, order, metadata=metadata)
    return client.place_order(strategy_run_id, order, idempotency_key, metadata=metadata)


__all__ = [
    "amo_limit_order",
    "ensure_run",
    "live_equity_market_order",
    "preview_then_place_order",
    "wait_for_history",
    "wait_for_quotes",
]
