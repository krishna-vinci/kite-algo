from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover - import avoids runtime options-model coupling
    from backend.options.execution.models import OptionRunState


TickLoader = Callable[[int], Awaitable[dict[str, Any] | None]]
TokenResolver = Callable[[str, str], Awaitable[int | None]]


async def derive_live_option_protection_metrics(
    run: OptionRunState,
    *,
    index_token_resolver: TokenResolver,
    index_tick_loader: TickLoader,
    option_tick_loader: TickLoader,
    now: datetime,
    option_token_resolver: TokenResolver | None = None,
    max_age_seconds: float = 10.0,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Derive fresh option metrics from the run's own open legs and fills.

    ``combined_premium`` is signed option points for one minimum-quantity unit of
    the current open structure: each short contributes ``+LTP`` and each long
    contributes ``-LTP``, weighted by that leg's open quantity divided by the
    smallest open leg quantity. The entry value uses the same sign convention on
    confirmed entry-fill VWAPs. ``strategy_mtm`` is the signed open-quantity
    sum of ``entry VWAP - LTP`` for shorts and ``LTP - entry VWAP`` for longs,
    positive when the structure is profitable.

    A metric is omitted when any input it needs is absent, non-positive, or older
    than ``max_age_seconds``. The returned error map says why each omission
    happened; it is diagnostic metadata and is never evaluated as a metric.
    """

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    errors: dict[str, str] = {}
    positions = _open_positions(run)
    if not positions:
        errors["open_positions"] = "run has no open option legs"

    index_ltp = await _fresh_tick_price(
        await index_token_resolver(_underlying(run), "NSE"),
        index_tick_loader,
        now,
        max_age_seconds,
    )
    if index_ltp is None:
        errors["index_ltp"] = "underlying index LTP is missing or older than 10 seconds"

    premium_inputs: list[tuple[float, float, float]] = []
    mtm = 0.0
    option_ltps: dict[str, float] = {}
    for index, position in enumerate(positions):
        try:
            token = await _position_token(
                position,
                index,
                option_token_resolver,
            )
        except (TypeError, ValueError):
            token = None
        ltp = await _fresh_tick_price(
            token,
            option_tick_loader,
            now,
            max_age_seconds,
        )
        symbol = str(position.get("symbol") or f"leg_{index + 1}")
        if ltp is None:
            errors[f"option_ltp:{symbol}"] = "option LTP is missing or older than 10 seconds"
            continue
        option_ltps[symbol] = ltp
        entry_price = float(position["entry_price"])
        exposure_sign = float(position["exposure_sign"])
        quantity = float(position["quantity"])
        premium_inputs.append((quantity, exposure_sign, ltp))
        mtm -= quantity * exposure_sign * (ltp - entry_price)

    entry_premium = _entry_combined_premium(run, positions)
    metrics: dict[str, Any] = {"open_quantity": sum(int(item["quantity"]) for item in positions)}
    if index_ltp is not None:
        metrics["index_ltp"] = index_ltp

    unit_scale = min((float(item["quantity"]) for item in positions if float(item["quantity"]) > 0), default=0.0)
    premium_missing = (
        not positions
        or unit_scale <= 0
        or len(option_ltps) != len({str(item.get("symbol") or f"leg_{index + 1}") for index, item in enumerate(positions)})
        or entry_premium is None
    )
    if not premium_missing:
        current_premium = sum(
            (quantity / unit_scale) * exposure_sign * option_ltps[str(position.get("symbol"))]
            for position, (quantity, exposure_sign, _ltp) in zip(positions, premium_inputs)
        )
        metrics["combined_premium"] = round(float(current_premium), 6)
        metrics["combined_premium_change_pct"] = round(
            100.0 * (float(current_premium) - float(entry_premium)) / abs(float(entry_premium)),
            6,
        )
    else:
        errors["combined_premium"] = "open legs or confirmed entry fill prices are incomplete"
        errors["combined_premium_change_pct"] = errors["combined_premium"]

    if positions and len(option_ltps) == len(positions):
        metrics["strategy_mtm"] = round(float(mtm), 6)
    elif positions:
        errors["strategy_mtm"] = "one or more open option LTPs are missing or stale"

    return metrics, errors


async def _position_token(
    position: dict[str, Any],
    index: int,
    token_resolver: TokenResolver | None,
) -> int | None:
    token = position.get("instrument_token")
    if token is not None:
        try:
            return int(token)
        except (TypeError, ValueError):
            return None
    symbol = str(position.get("symbol") or f"leg_{index + 1}")
    if token_resolver is None:
        return None
    resolved = await token_resolver(str(position.get("exchange") or "NFO"), symbol)
    if resolved is not None:
        return int(resolved)
    raise ValueError(f"option leg has no instrument token: {symbol}")


async def _fresh_tick_price(
    token: int | None,
    tick_loader: TickLoader,
    now: datetime,
    max_age_seconds: float,
) -> float | None:
    if token is None:
        return None
    try:
        tick = await tick_loader(int(token))
    except Exception:
        return None
    if not isinstance(tick, dict):
        return None
    try:
        price = float(tick.get("last_price"))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    observed_at = _tick_time(tick)
    if observed_at is None:
        return None
    age = (now.astimezone(timezone.utc) - observed_at).total_seconds()
    if age < 0 or age > max_age_seconds:
        return None
    return price


def _tick_time(tick: dict[str, Any]) -> datetime | None:
    for key in ("received_at", "exchange_timestamp", "last_trade_time"):
        value = tick.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return None


def _open_positions(run: "OptionRunState") -> list[dict[str, Any]]:
    leg_by_id = {str(leg.get("leg_id") or ""): leg for leg in run.legs if isinstance(leg, dict)}
    net_by_leg: dict[str, int] = defaultdict(int)
    if run.trades:
        for trade in run.trades:
            if not isinstance(trade, dict):
                continue
            leg_id = str(trade.get("leg_id") or "")
            if not leg_id:
                continue
            quantity = int(trade.get("quantity") or 0)
            side = str(trade.get("transaction_type") or "").upper()
            net_by_leg[leg_id] += quantity if side == "BUY" else -quantity if side == "SELL" else 0
    else:
        completed = {str(item) for item in (run.completed_legs or [])}
        for leg in run.legs:
            if not isinstance(leg, dict) or str(leg.get("leg_id") or "") in completed:
                continue
            side = str(leg.get("transaction_type") or "").upper()
            quantity = int(leg.get("quantity") or 0)
            net_by_leg[str(leg.get("leg_id") or "")] += quantity if side == "BUY" else -quantity

    positions: list[dict[str, Any]] = []
    for leg_id, raw_net in net_by_leg.items():
        if raw_net == 0:
            continue
        leg = leg_by_id.get(leg_id, {})
        entry_side = str(leg.get("transaction_type") or ("BUY" if raw_net > 0 else "SELL")).upper()
        entry_sign = 1 if entry_side == "BUY" else -1
        if raw_net * entry_sign < 0:
            # A leg cannot hold the opposite of its own declared side; treat an
            # inconsistent ledger as unreadable rather than inventing a position.
            continue
        entry_price = _entry_vwap(run, leg_id, entry_side, leg)
        if entry_price is None:
            continue
        positions.append(
            {
                "leg_id": leg_id,
                "symbol": leg.get("tradingsymbol"),
                "exchange": leg.get("exchange") or "NFO",
                "instrument_token": leg.get("instrument_token"),
                "entry_side": entry_side,
                "exposure_sign": -entry_sign,
                "quantity": abs(raw_net),
                "entry_price": entry_price,
            }
        )
    return positions


def _entry_vwap(run: "OptionRunState", leg_id: str, entry_side: str, leg: dict[str, Any]) -> float | None:
    fills = [
        trade
        for trade in (run.trades or [])
        if isinstance(trade, dict)
        and str(trade.get("leg_id") or "") == leg_id
        and str(trade.get("transaction_type") or "").upper() == entry_side
        and (
            not trade.get("phase")
            or str(trade.get("phase")) == "entry"
            or str(trade.get("phase")) == "adjust"
        )
    ]
    total_quantity = 0
    total_value = 0.0
    for fill in fills:
        try:
            quantity = int(fill.get("quantity") or 0)
            price = float(fill.get("price") or fill.get("average_price") or fill.get("fill_price"))
        except (TypeError, ValueError):
            continue
        if quantity <= 0 or price <= 0:
            continue
        total_quantity += quantity
        total_value += quantity * price
    if total_quantity > 0:
        return total_value / total_quantity
    for key in ("price", "ltp"):
        try:
            price = float(leg.get(key))
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def _entry_combined_premium(run: "OptionRunState", positions: list[dict[str, Any]]) -> float | None:
    # Build directly from fills so a partially exited current book still compares
    # against the original confirmed entry, not the remaining quantity.
    leg_by_id = {str(leg.get("leg_id") or ""): leg for leg in run.legs if isinstance(leg, dict)}
    values: list[float] = []
    quantities: list[int] = []
    for position in positions:
        leg_id = str(position.get("leg_id") or "")
        leg = leg_by_id.get(leg_id, {})
        side = str(position.get("entry_side") or "")
        entry_price = _entry_vwap(run, leg_id, side, leg)
        if entry_price is None:
            return None
        fills = [
            trade
            for trade in (run.trades or [])
            if isinstance(trade, dict)
            and str(trade.get("leg_id") or "") == leg_id
            and str(trade.get("transaction_type") or "").upper() == side
        ]
        quantity = sum(int(trade.get("quantity") or 0) for trade in fills) if fills else int(leg.get("quantity") or 0)
        values.append(float(position.get("exposure_sign")) * entry_price)
        quantities.append(quantity)
    if not values or min(quantities) <= 0:
        return None
    scale = min(quantities)
    return sum(value * (quantity / scale) for value, quantity in zip(values, quantities))


def _underlying(run: "OptionRunState") -> str:
    protection = run.protection if isinstance(run.protection, dict) else {}
    return str(protection.get("underlying") or run.metadata.get("underlying") or "")


def open_option_positions(run: "OptionRunState") -> list[dict[str, Any]]:
    """Public view of the open-leg evidence used by live metric derivation."""

    return _open_positions(run)
