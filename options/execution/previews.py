from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from paper_runtime import PaperChargesCalculator, PaperMarginEngine

from .models import OptionRunState
from .planner import build_entry_order_plan, sort_orders_buy_first


def build_entry_preview_packet(
    run: OptionRunState,
    *,
    instruments_repository: Any | None = None,
    margin_engine: PaperMarginEngine | None = None,
    charges_calculator: PaperChargesCalculator | None = None,
) -> dict[str, Any]:
    resolved_legs = [dict(leg) for leg in run.legs]
    order_plan = build_entry_order_plan(resolved_legs, product=str(run.product or ""))
    margin_packet, charges_packet = _estimate_preview_costs(
        order_plan,
        legs_by_id={str(leg.get("leg_id") or ""): leg for leg in resolved_legs},
        starting_net_positions={},
        instruments_repository=instruments_repository,
        margin_engine=margin_engine,
        charges_calculator=charges_calculator,
        include_margin=True,
    )
    return {
        "strategy_run_id": run.strategy_run_id,
        "product": run.product,
        "resolved_legs": resolved_legs,
        "order_plan": order_plan,
        "margin": margin_packet,
        "charges": charges_packet,
        "protection_preview": run.protection,
    }


def build_exit_preview_packet(
    run: OptionRunState,
    *,
    instruments_repository: Any | None = None,
    margin_engine: PaperMarginEngine | None = None,
    charges_calculator: PaperChargesCalculator | None = None,
) -> dict[str, Any]:
    open_legs = _derive_open_exit_legs(run)
    order_plan = sort_orders_buy_first(
        [
            _build_exit_order_plan_item(leg, product=run.product)
            for leg in open_legs
            if int(leg.get("open_quantity") or 0) > 0
        ]
    )
    margin_packet, charges_packet = _estimate_preview_costs(
        order_plan,
        legs_by_id={str(leg.get("leg_id") or ""): leg for leg in open_legs},
        starting_net_positions=_starting_net_positions_for_exit(open_legs, product=run.product),
        instruments_repository=instruments_repository,
        margin_engine=margin_engine,
        charges_calculator=charges_calculator,
        include_margin=False,
    )
    return {
        "strategy_run_id": run.strategy_run_id,
        "product": run.product,
        "open_legs": open_legs,
        "order_plan": order_plan,
        "margin": margin_packet,
        "charges": charges_packet,
    }


def _estimate_preview_costs(
    order_plan: list[dict[str, Any]],
    *,
    legs_by_id: dict[str, dict[str, Any]],
    starting_net_positions: dict[tuple[str, str, str], int],
    instruments_repository: Any | None,
    margin_engine: PaperMarginEngine | None,
    charges_calculator: PaperChargesCalculator | None,
    include_margin: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    margin_engine = margin_engine or PaperMarginEngine()
    charges_calculator = charges_calculator or PaperChargesCalculator()
    position_qty_by_key = {key: int(value) for key, value in (starting_net_positions or {}).items()}
    starting_margin_total = _current_margin_total(
        position_qty_by_key=position_qty_by_key,
        order_plan=order_plan,
        legs_by_id=legs_by_id,
        instruments_repository=instruments_repository,
        margin_engine=margin_engine,
    )
    current_margin_total = starting_margin_total
    peak_margin_total = starting_margin_total
    charges_total = Decimal("0")
    missing_references: list[str] = []
    per_leg: list[dict[str, Any]] = []

    for sequence_index, order in enumerate(order_plan, start=1):
        leg_id = str(order.get("leg_id") or "")
        leg = legs_by_id.get(leg_id, order)
        instrument = _lookup_instrument(instruments_repository, order)
        reference_price, reference_source = _resolve_reference_price(order, leg, instrument)
        instrument_type = str((instrument or {}).get("instrument_type") or leg.get("instrument_type") or "")
        quantity = int(order.get("quantity") or leg.get("open_quantity") or 0)
        order_key = _order_position_key(order, fallback_product=leg.get("product"))
        net_before = int(position_qty_by_key.get(order_key, 0))
        margin_before = _margin_for_net_quantity(
            margin_engine,
            net_quantity=net_before,
            product=str(order.get("product") or leg.get("product") or ""),
            reference_price=reference_price,
            instrument_type=instrument_type,
        )
        margin_after = margin_before
        margin_delta = Decimal("0")
        incremental_margin = Decimal("0")
        estimated_charges = Decimal("0")
        net_after = net_before
        if reference_price is not None and quantity > 0:
            net_after = net_before + _transaction_sign(order) * quantity
            margin_after = _margin_for_net_quantity(
                margin_engine,
                net_quantity=net_after,
                product=str(order.get("product") or leg.get("product") or ""),
                reference_price=reference_price,
                instrument_type=instrument_type,
            )
            margin_delta = margin_after - margin_before
            incremental_margin = max(margin_delta, Decimal("0"))
            position_qty_by_key[order_key] = net_after
            current_margin_total += margin_delta
            if current_margin_total > peak_margin_total:
                peak_margin_total = current_margin_total
            estimated_charges = charges_calculator.estimate(
                price=reference_price,
                quantity=quantity,
                instrument_type=instrument_type,
                exchange=str(order.get("exchange") or ""),
                product=str(order.get("product") or ""),
            )
            charges_total += estimated_charges
        else:
            missing_references.append(leg_id or str(order.get("tradingsymbol") or "unknown"))

        charges_contract = None
        if reference_price is not None and quantity > 0:
            charges_contract = charges_calculator.estimate_contract(
                price=reference_price,
                quantity=quantity,
                instrument_type=instrument_type,
                exchange=str(order.get("exchange") or ""),
                product=str(order.get("product") or ""),
            ).model_dump(mode="json")

        per_leg.append(
            {
                "leg_id": leg_id,
                "sequence_index": sequence_index,
                "tradingsymbol": order.get("tradingsymbol") or leg.get("tradingsymbol"),
                "transaction_type": order.get("transaction_type") or leg.get("transaction_type"),
                "quantity": quantity,
                "reference_price": float(reference_price) if reference_price is not None else None,
                "reference_price_source": reference_source,
                "instrument_type": instrument_type or None,
                "net_quantity_before": net_before,
                "net_quantity_after": net_after,
                "required_margin": float(incremental_margin),
                "margin_before": float(margin_before),
                "margin_after": float(margin_after),
                "margin_delta": float(margin_delta),
                "estimated_charges": float(estimated_charges),
                "cost_contract": {
                    "margin_required": float(incremental_margin),
                    "charges_estimate": float(estimated_charges),
                    **({"charges_contract": charges_contract} if charges_contract is not None else {}),
                },
            }
        )

    source = "paper_estimators"
    if missing_references and len(missing_references) == len(order_plan):
        source = "paper_estimators_unresolved"
    elif missing_references:
        source = "paper_estimators_partial"

    margin_packet = {
        "required": float(max(peak_margin_total - starting_margin_total, Decimal("0"))) if include_margin else 0.0,
        "starting_required": float(starting_margin_total),
        "final_required": float(current_margin_total),
        "peak_required": float(peak_margin_total),
        "source": source,
        "per_leg": per_leg,
    }
    charges_packet = {
        "estimated": float(charges_total),
        "source": source,
        "per_leg": per_leg,
    }
    if missing_references:
        margin_packet["unresolved_legs"] = list(missing_references)
        charges_packet["unresolved_legs"] = list(missing_references)
    return margin_packet, charges_packet


def _current_margin_total(
    *,
    position_qty_by_key: dict[tuple[str, str, str], int],
    order_plan: list[dict[str, Any]],
    legs_by_id: dict[str, dict[str, Any]],
    instruments_repository: Any | None,
    margin_engine: PaperMarginEngine,
) -> Decimal:
    total = Decimal("0")
    seen_keys: set[tuple[str, str, str]] = set()
    for order in order_plan:
        leg_id = str(order.get("leg_id") or "")
        leg = legs_by_id.get(leg_id, order)
        key = _order_position_key(order, fallback_product=leg.get("product"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        instrument = _lookup_instrument(instruments_repository, order)
        reference_price, _ = _resolve_reference_price(order, leg, instrument)
        instrument_type = str((instrument or {}).get("instrument_type") or leg.get("instrument_type") or "")
        total += _margin_for_net_quantity(
            margin_engine,
            net_quantity=position_qty_by_key.get(key, 0),
            product=key[2],
            reference_price=reference_price,
            instrument_type=instrument_type,
        )
    return total


def _order_position_key(order: dict[str, Any], *, fallback_product: Any = None) -> tuple[str, str, str]:
    return (
        str(order.get("exchange") or "").strip().upper(),
        str(order.get("tradingsymbol") or "").strip().upper(),
        str(order.get("product") or fallback_product or "").strip().upper(),
    )


def _transaction_sign(order: dict[str, Any]) -> int:
    return 1 if str(order.get("transaction_type") or "").upper() == "BUY" else -1


def _margin_for_net_quantity(
    margin_engine: PaperMarginEngine,
    *,
    net_quantity: int,
    product: str,
    reference_price: Decimal | None,
    instrument_type: str,
) -> Decimal:
    if int(net_quantity) == 0 or reference_price is None or reference_price <= 0:
        return Decimal("0")
    return margin_engine.required_margin(
        side="BUY" if int(net_quantity) > 0 else "SELL",
        product=str(product or ""),
        quantity=abs(int(net_quantity)),
        reference_price=reference_price,
        instrument_type=instrument_type,
    )


def _starting_net_positions_for_exit(
    open_legs: list[dict[str, Any]],
    *,
    product: str | None,
) -> dict[tuple[str, str, str], int]:
    positions: dict[tuple[str, str, str], int] = {}
    for leg in open_legs:
        quantity = int(leg.get("open_quantity") or 0)
        if quantity <= 0:
            continue
        key = _order_position_key(leg, fallback_product=product)
        side = str(leg.get("entry_side") or "").upper()
        positions[key] = positions.get(key, 0) + (quantity if side == "BUY" else -quantity)
    return positions


def _lookup_instrument(instruments_repository: Any | None, order: dict[str, Any]) -> dict[str, Any] | None:
    if instruments_repository is None:
        return None
    exchange = str(order.get("exchange") or "").strip().upper()
    tradingsymbol = str(order.get("tradingsymbol") or "").strip().upper()
    if not exchange or not tradingsymbol:
        return None
    try:
        instrument = instruments_repository.get_instrument_by_exchange_symbol(exchange, tradingsymbol)
    except Exception:
        return None
    return dict(instrument) if instrument else None


def _resolve_reference_price(
    order: dict[str, Any],
    leg: dict[str, Any],
    instrument: dict[str, Any] | None,
) -> tuple[Decimal | None, str]:
    ordered_candidates = (
        (order.get("price"), "order_price"),
        (leg.get("price"), "leg_price"),
        (leg.get("limit_price"), "limit_price"),
        (leg.get("ltp"), "ltp"),
        (leg.get("last_price"), "last_price"),
        ((instrument or {}).get("last_price"), "instrument_last_price"),
    )
    for raw_value, source in ordered_candidates:
        value = _to_decimal(raw_value)
        if value is not None and value > 0:
            return value, source
    return None, "unavailable"


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _build_exit_order_plan_item(leg: dict[str, Any], *, product: str | None) -> dict[str, Any]:
    order_type = str(leg.get("exit_order_type") or leg.get("order_type") or "MARKET").upper()
    item: dict[str, Any] = {
        "leg_id": leg["leg_id"],
        "exchange": leg.get("exchange") or "NFO",
        "tradingsymbol": leg.get("tradingsymbol"),
        "quantity": leg["open_quantity"],
        "transaction_type": "SELL" if leg.get("entry_side") == "BUY" else "BUY",
        "variety": leg.get("exit_variety") or leg.get("variety") or "regular",
        "order_type": order_type,
        "product": product,
    }
    exit_price = leg.get("exit_price") if leg.get("exit_price") is not None else leg.get("limit_price")
    if order_type == "LIMIT" and exit_price is not None:
        item["price"] = exit_price
    if order_type == "MARKET" and leg.get("market_protection") is not None:
        item["market_protection"] = leg.get("market_protection")
    return item


def _derive_open_exit_legs(run: OptionRunState) -> list[dict[str, Any]]:
    # Prefer trade-derived open quantity when available.
    if run.trades:
        signed_qty_by_leg: dict[str, int] = defaultdict(int)
        meta_by_leg: dict[str, dict[str, Any]] = {
            str(leg.get("leg_id") or ""): dict(leg)
            for leg in run.legs
            if str(leg.get("leg_id") or "")
        }
        for trade in run.trades:
            leg_id = str(trade.get("leg_id") or "")
            if not leg_id:
                continue
            qty = int(trade.get("quantity") or 0)
            side = str(trade.get("transaction_type") or "").upper()
            if side == "BUY":
                signed_qty_by_leg[leg_id] += qty
            elif side == "SELL":
                signed_qty_by_leg[leg_id] -= qty
            meta_by_leg.setdefault(leg_id, {})
            meta_by_leg[leg_id].setdefault("tradingsymbol", trade.get("tradingsymbol"))
            meta_by_leg[leg_id].setdefault(
                "entry_side",
                trade.get("entry_side") or ("BUY" if signed_qty_by_leg[leg_id] >= 0 else "SELL"),
            )

        open_legs = []
        for leg_id, net in signed_qty_by_leg.items():
            if net == 0:
                continue
            entry_side = "BUY" if net > 0 else "SELL"
            meta = meta_by_leg.get(leg_id, {})
            open_legs.append(
                {
                    "leg_id": leg_id,
                    "exchange": meta.get("exchange") or "NFO",
                    "tradingsymbol": meta.get("tradingsymbol"),
                    "entry_side": meta.get("entry_side") or entry_side,
                    "open_quantity": abs(int(net)),
                    "instrument_type": meta.get("instrument_type"),
                    "ltp": meta.get("ltp"),
                    "last_price": meta.get("last_price"),
                    "exit_order_type": meta.get("exit_order_type"),
                    "exit_price": meta.get("exit_price"),
                    "limit_price": meta.get("limit_price"),
                    "exit_variety": meta.get("exit_variety"),
                    "market_protection": meta.get("market_protection"),
                }
            )
        return open_legs

    # For B3 phase fallback: use completed entry legs when trades are unavailable.
    completed = set(run.completed_legs)
    legs = run.legs if completed else []
    if completed:
        legs = [leg for leg in run.legs if str(leg.get("leg_id") or "") in completed]

    open_legs: list[dict[str, Any]] = []
    for index, leg in enumerate(legs):
        quantity = int(leg.get("quantity") or 0)
        if quantity <= 0:
            continue
        transaction_type = str(leg.get("transaction_type") or "").upper()
        if transaction_type not in {"BUY", "SELL"}:
            continue
        open_legs.append(
            {
                "leg_id": str(leg.get("leg_id") or f"leg_{index + 1}"),
                "exchange": leg.get("exchange") or "NFO",
                "tradingsymbol": leg.get("tradingsymbol"),
                "entry_side": transaction_type,
                "open_quantity": quantity,
                "instrument_type": leg.get("instrument_type"),
                "ltp": leg.get("ltp"),
                "last_price": leg.get("last_price"),
                "exit_order_type": leg.get("exit_order_type"),
                "exit_price": leg.get("exit_price"),
                "limit_price": leg.get("limit_price"),
                "exit_variety": leg.get("exit_variety"),
                "market_protection": leg.get("market_protection"),
            }
        )
    return open_legs
