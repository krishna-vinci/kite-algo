from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from .client import KiteAlgoWorkerClient
from .exceptions import KiteAlgoWorkerError
from .models import WorkerCandle, WorkerHistoricalCandles, WorkerOrderSnapshot
from .orders import equity_market_order, limit_order, market_order


TERMINAL_ORDER_STATES = {"COMPLETE", "CANCELLED", "REJECTED"}


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


def amo_market_order(
    exchange: str,
    tradingsymbol: str,
    transaction_type: str,
    product: str,
    quantity: int,
    **kwargs: Any,
):
    return market_order(exchange, tradingsymbol, transaction_type, product, quantity, variety="amo", **kwargs)


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


def wait_for_terminal_order_state(
    client: KiteAlgoWorkerClient,
    strategy_run_id: str,
    order_id: str,
    *,
    attempts: int = 20,
    sleep_seconds: float = 1.0,
):
    last: Optional[WorkerOrderSnapshot] = None
    for _ in range(attempts):
        last = client.get_order_snapshot(strategy_run_id, order_id)
        if last.status in TERMINAL_ORDER_STATES:
            return last
        time.sleep(sleep_seconds)
    return last


def wait_for_fresh_candle(
    client: KiteAlgoWorkerClient,
    instrument: str | int,
    *,
    interval: str = "5minute",
    lookback: int = 1,
    attempts: int = 20,
    sleep_seconds: float = 1.0,
) -> Optional[WorkerCandle]:
    last_snapshot: Optional[WorkerHistoricalCandles] = None
    for _ in range(attempts):
        last_snapshot = client.get_candles_snapshot(instrument, interval=interval, lookback=lookback)
        current = last_snapshot.current
        if current is not None and current.is_complete:
            return current
        if current is None and last_snapshot.candles:
            candidate = last_snapshot.candles[-1]
            if candidate.is_complete:
                return candidate
        time.sleep(sleep_seconds)

    if last_snapshot is None:
        return None
    if last_snapshot.current is not None:
        return last_snapshot.current
    if last_snapshot.candles:
        return last_snapshot.candles[-1]
    return None


def warmup_history(
    client: KiteAlgoWorkerClient,
    instrument: str | int,
    *,
    timeframe: str = "5minute",
    min_candles: int = 200,
    attempts: int = 10,
    sleep_seconds: float = 1.0,
    **kwargs: Any,
) -> WorkerHistoricalCandles:
    last: WorkerHistoricalCandles | None = None
    for _ in range(attempts):
        last = client.get_historical_candles_snapshot(instrument, timeframe=timeframe, **kwargs)
        if len(last.candles) >= min_candles:
            return last
        time.sleep(sleep_seconds)
    if last is None:
        raise RuntimeError("warmup_history exhausted without fetching any historical candles")
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
    "amo_market_order",
    "amo_limit_order",
    "ensure_run",
    "live_equity_market_order",
    "preview_then_place_order",
    "TERMINAL_ORDER_STATES",
    "wait_for_fresh_candle",
    "wait_for_terminal_order_state",
    "wait_for_history",
    "wait_for_quotes",
    "warmup_history",
]
