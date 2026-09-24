"""Example 1 - index ticker + indicator -> one separately traded instrument.

The index ticker (for example ``NSE:NIFTY50``) is the SIGNAL SOURCE. The
instrument actually traded is configured separately and resolved to a real
broker coordinate before any proposal exists, so the index is never silently
turned into its constituents or treated as a tradable instrument.

Contract notes this file takes seriously:

* every call uses a documented SDK method and the response keys the platform
  actually returns (candles carry ``ts``; the indicator returns
  ``name``/``timestamps``/``values``/``ready``/``warmup_rows``);
* the strategy and account come from the run's PERSISTED binding, never from a
  parameter;
* every early exit is a named no-action with its reason printed to the child's
  own log, and an unresolved wait ends non-zero instead of pretending success;
* a request that reached ``executed`` is a DISPATCH result, not a fill: the
  child then inspects its own attributed book and pending work.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional


def _note(ctx, text: str) -> None:  # noqa: ANN001
    """Mirror the decision to the child log, then report it as progress.

    The platform caps a progress note at 200 characters, so the mirrored line is
    truncated for the API while the log keeps the full text.
    """
    print(f"[strategy] {text}", file=sys.stderr, flush=True)
    try:
        ctx.progress(text[:200])
    except Exception as exc:  # noqa: BLE001 - the platform is the authority
        print(f"[strategy] progress refused: {exc}", file=sys.stderr, flush=True)
        raise


def _stop(ctx, reason: str, **fields: Any) -> int:  # noqa: ANN001
    payload = json.dumps(fields, default=str, sort_keys=True) if fields else ""
    _note(ctx, f"no action: {reason}{(' ' + payload) if payload else ''}")
    return 0


def _unresolved(ctx, reason: str, **fields: Any) -> int:  # noqa: ANN001
    payload = json.dumps(fields, default=str, sort_keys=True) if fields else ""
    _note(ctx, f"unresolved: {reason}{(' ' + payload) if payload else ''}")
    return 2


def _number(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _bars(candles: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The indicator route's bar shape, from the candle contract (``ts``)."""
    bars: List[Dict[str, Any]] = []
    for row in list(candles.get("candles") or []):
        if not isinstance(row, dict):
            continue
        bars.append(
            {
                "timestamp": row.get("ts") or row.get("timestamp") or row.get("time"),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": _number(row.get("close")),
                "volume": _number(row.get("volume"), 0.0),
                "is_complete": row.get("is_complete", True),
            }
        )
    return bars


_SERIES_KEYS = ("ema", "sma", "wma", "rsi", "value", "close", "macd")


def _tail(values: Any, offset: int) -> Optional[float]:
    if isinstance(values, dict):
        for key in _SERIES_KEYS:
            series = values.get(key)
            if isinstance(series, list) and len(series) >= offset:
                return _number(series[-offset])
        for series in values.values():
            if isinstance(series, list) and len(series) >= offset:
                return _number(series[-offset])
        return None
    if isinstance(values, list) and len(values) >= offset:
        return _number(values[-offset])
    return None


def _price(quotes: Dict[str, Any], coordinate: str) -> Optional[float]:
    exchange_wanted, _, symbol_wanted = str(coordinate).partition(":")
    exchange_wanted = exchange_wanted.strip().upper()
    symbol_wanted = (symbol_wanted or str(coordinate)).strip().upper().replace(" ", "")
    for row in list(quotes.get("quotes") or quotes.get("data") or []):
        if not isinstance(row, dict):
            continue
        exchange = str(row.get("exchange") or "").strip().upper()
        symbol = str(row.get("tradingsymbol") or row.get("symbol") or "")
        _, _, bare = symbol.partition(":")
        bare = (bare or symbol).strip().upper().replace(" ", "")
        if bare != symbol_wanted:
            continue
        if exchange_wanted and exchange and exchange != exchange_wanted:
            continue
        price = _number(row.get("last_price") or row.get("ltp"))
        if price is not None:
            return price
    return None


_TERMINAL_REQUEST_STATES = {"executed", "refused", "rejected", "dispatch_unresolved"}


def _await_request(ctx, request_id: str, deadline_seconds: float) -> Dict[str, Any]:  # noqa: ANN001
    """Wait for an AUTHORITATIVE request outcome, reporting progress while waiting.

    ``queued`` and ``dispatching`` are NOT outcomes: they mean the request is
    still moving. ``executed`` means the dispatch happened - the caller then
    inspects its own book before concluding anything about fills.
    """
    if not request_id:
        return {"status": "unknown", "detail": "no request id"}
    started = time.monotonic()
    while time.monotonic() - started < deadline_seconds:
        ctx.progress(f"waiting on request {request_id}")
        row = ctx.run.execution_request(request_id)
        status = str(row.get("status") or "")
        if status in _TERMINAL_REQUEST_STATES:
            return row
        time.sleep(2.0)
    return {"status": "timeout", "request_id": request_id}


def _await_work_settled(
    ctx,  # noqa: ANN001
    deadline_seconds: float,
    expect: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Wait until this strategy has no outstanding work AND shows the target.

    "No outstanding work" alone is not settlement: the attributed book is rebuilt
    on demand, so a read taken before the rebuild legitimately shows the previous
    (empty) book. When the caller knows the target it just submitted, the wait
    continues until the book AGREES with it, and the caller reports the observed
    book if it never does.
    """
    wanted = {str(symbol).upper(): int(quantity) for symbol, quantity in (expect or {}).items()}
    started = time.monotonic()
    last: Dict[str, Any] = {}
    while time.monotonic() - started < deadline_seconds:
        last = ctx.run.owned_work()
        pending = [row for row in list(last.get("pending") or []) if isinstance(row, dict)]
        state = str(last.get("coverage") or "")
        positions = _position_map(last)
        missing = {symbol: qty for symbol, qty in wanted.items() if positions.get(symbol) != qty}
        ctx.progress(
            f"settling: coverage={state} pending={len(pending)} "
            f"positions={positions} awaited={missing or '{}'}"
        )
        if state == "known" and not pending and not missing:
            return last
        time.sleep(2.0)
    return last


def _position_map(snapshot: Dict[str, Any]) -> Dict[str, int]:
    """The strategy's own book as ``{SYMBOL: net_quantity}``, unsigned totals."""
    totals: Dict[str, int] = {}
    for row in list(snapshot.get("positions") or []):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        if not symbol:
            continue
        totals[symbol] = totals.get(symbol, 0) + int(row.get("net_quantity") or 0)
    return totals


def main(ctx) -> int:  # noqa: ANN001 - the hosted contract is main(ctx)
    params: Dict[str, Any] = dict(ctx.params or {})

    index_symbol = str(params.get("index_symbol") or "NIFTY 50").strip()
    index_exchange = str(params.get("index_exchange") or "NSE").strip().upper()
    # Catalog coordinates are EXCHANGE:SYMBOL and space-free (``NSE:NIFTY50``).
    index_reference = (
        index_symbol if ":" in index_symbol else f"{index_exchange}:{index_symbol}"
    ).replace(" ", "")
    interval = str(params.get("interval") or "5minute")
    lookback = int(params.get("lookback") or 60)
    indicator_name = str(params.get("indicator") or "ema").lower()
    period = int(params.get("period") or 20)

    traded_symbol = str(params.get("traded_symbol") or "").strip()
    traded_exchange = str(params.get("traded_exchange") or "NSE").strip().upper()
    product = str(params.get("product") or "CNC").strip().upper()
    quantity = abs(int(params.get("quantity") or 1))
    deadline_seconds = float(params.get("deadline_seconds") or 120)

    identity = ctx.run.attribution()
    if not identity.get("attributed") or not identity.get("strategy_id"):
        return _stop(ctx, "the run has no persisted strategy binding; refusing to propose")
    strategy_id = str(identity["strategy_id"])
    account_scope = str(identity.get("account_id") or ctx.run.config.account_scope)

    _note(ctx, f"reading {index_reference} candles for the {indicator_name}({period}) signal")

    # -- 1. the index ticker ------------------------------------------------
    candles = ctx.client.get_candles(index_reference, interval=interval, lookback=lookback)
    bars = _bars(candles)
    if len(bars) < max(period, 3):
        return _stop(ctx, "not enough index candles", got=len(bars), need=max(period, 3))

    # -- 2. the indicator, computed by the platform --------------------------
    result = ctx.client.calculate_indicator(
        {"name": indicator_name, "bars": bars, "period": period}
    )
    if not result.get("ready"):
        return _stop(
            ctx,
            "the indicator is not ready yet",
            warmup_rows=result.get("warmup_rows"),
            name=result.get("name"),
        )
    latest = _tail(result.get("values"), 1)
    prior = _tail(result.get("values"), 2)
    index_price = _price(ctx.client.get_quotes([index_reference], mode="quote"), index_reference)
    if latest is None or prior is None or index_price is None:
        return _stop(
            ctx,
            "the indicator or the index price is unavailable",
            latest=latest,
            prior=prior,
            price=index_price,
        )
    rising = index_price > latest and index_price >= prior
    falling = index_price < latest and index_price <= prior
    _note(
        ctx,
        f"{index_reference} price={index_price} {indicator_name}({period})={latest} "
        f"prior={prior} -> {'rising' if rising else 'falling' if falling else 'flat'}",
    )
    if not rising:
        return _stop(
            ctx,
            "no bullish index signal",
            price=index_price,
            indicator=latest,
            prior=prior,
            direction="falling" if falling else "flat",
        )

    # -- 3. the SEPARATELY configured traded instrument ----------------------
    if not traded_symbol:
        return _stop(ctx, "no traded_symbol configured; the index is a signal, not a position")
    coordinate = f"{traded_exchange}:{traded_symbol}".replace(" ", "")
    resolved = ctx.client.resolve_ticker(coordinate)
    instrument = dict(resolved.get("instrument") or resolved)
    token = instrument.get("instrument_token") or instrument.get("token")
    tradingsymbol = str(instrument.get("tradingsymbol") or traded_symbol)
    instrument_type = str(instrument.get("instrument_type") or "").upper()
    if token is None:
        return _stop(ctx, "the traded instrument did not resolve", coordinate=coordinate)
    if instrument_type in {"INDEX", "IDX"}:
        return _stop(ctx, "the resolved instrument is an index", coordinate=coordinate)
    # The frozen plan carries the reference price admission sizes against, so it
    # is read from the quote for THIS instrument rather than assumed.
    reference_price = _price(
        ctx.client.get_quotes([coordinate], mode="quote"), coordinate
    )
    if reference_price is None or reference_price <= 0:
        return _stop(ctx, "no usable reference price for the traded instrument", coordinate=coordinate)
    _note(
        ctx,
        f"signal={index_reference} traded={traded_exchange}:{tradingsymbol} "
        f"reference_price={reference_price}",
    )

    # -- 4. one bounded proposal, then the governed request ------------------
    evaluation_id = f"idx-{ctx.run_id}-{int(index_price)}-{int(latest)}"
    submitted = ctx.run.submit_and_request_execution(
        {
            "evaluation_id": evaluation_id,
            "evaluation_kind": "run_now",
            "strategy_id": strategy_id,
            "strategy_run_id": ctx.run_id,
            "account_scope": account_scope,
            "target_kind": "single_instrument",
            "payload": {
                "instrument_token": int(token),
                "exchange": traded_exchange,
                "tradingsymbol": tradingsymbol,
                "product": product,
                "target_quantity": quantity,
                "reference_price": reference_price,
            },
        },
        idempotency_key=f"idx-{evaluation_id}",
    )
    request = dict(submitted.get("execution_request") or {})
    request_id = str(request.get("request_id") or "")
    _note(ctx, f"requested execution {request_id} status={request.get('status')}")

    final = _await_request(ctx, request_id, deadline_seconds)
    status = str(final.get("status") or "")
    if status not in _TERMINAL_REQUEST_STATES:
        return _unresolved(ctx, "the request never reached an authoritative outcome", status=status)
    if status in {"refused", "rejected"}:
        return _stop(
            ctx, "the platform refused the request", status=status, code=final.get("refusal_code")
        )
    if status == "dispatch_unresolved":
        return _unresolved(ctx, "the submission outcome is unknown", code=final.get("refusal_code"))

    # ``executed`` is a DISPATCH result. The authoritative fill/position evidence
    # is the strategy's own book and outstanding work.
    snapshot = _await_work_settled(
        ctx, deadline_seconds, expect={tradingsymbol.upper(): quantity}
    )
    pending = [row for row in list(snapshot.get("pending") or []) if isinstance(row, dict)]
    if pending:
        return _unresolved(
            ctx,
            "work is still outstanding",
            coverage=snapshot.get("coverage"),
            pending=len(pending),
        )
    if str(snapshot.get("coverage")) != "known":
        return _unresolved(
            ctx, "the attributed book is not published", coverage=snapshot.get("coverage")
        )
    positions = _position_map(snapshot)
    if positions.get(str(tradingsymbol).upper()) != int(quantity):
        return _unresolved(
            ctx,
            "the strategy's own book does not show the requested target",
            position=positions.get(str(tradingsymbol).upper()),
            expected=int(quantity),
        )
    _note(ctx, f"settled: coverage=known pending=0 positions={positions}")
    return 0
