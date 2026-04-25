from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from broker_api.kite_orders import ChargesOrderInput, OrderMarginInput

from .contracts import ChargesStatus, ExecutionCostContract


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _charges_input_from_order(order: Mapping[str, Any]) -> ChargesOrderInput:
    average_price = order.get("average_price", order.get("price", 0)) or 0
    return ChargesOrderInput(
        order_id=str(order.get("order_id") or order.get("client_order_ref") or "preview"),
        exchange=order["exchange"],
        tradingsymbol=order["tradingsymbol"],
        transaction_type=order["transaction_type"],
        variety=order["variety"],
        product=order["product"],
        order_type=order["order_type"],
        quantity=int(order["quantity"]),
        average_price=float(average_price),
    )


def _contract_from_kite_payload(*, margin_required: Decimal, charges: Mapping[str, Any], raw: Mapping[str, Any]) -> ExecutionCostContract:
    total = _decimal(charges.get("total", 0))
    return ExecutionCostContract(
        margin_required=margin_required,
        charges_estimate=total,
        brokerage=_decimal(charges.get("brokerage", 0)),
        exchange_txn_charge=_decimal(charges.get("exchange_turnover_charge", 0)),
        stt=_decimal(charges.get("transaction_tax", 0)),
        stamp_duty=_decimal(charges.get("stamp_duty", 0)),
        sebi_charge=_decimal(charges.get("sebi_turnover_charge", 0)),
        gst=_decimal(charges.get("gst", 0)),
        total_charges=total,
        charges_status=ChargesStatus.BROKER_QUOTED,
        raw=dict(raw),
    )


def build_live_order_cost_contract(*, kite: Any, orders_service: Any, order: dict[str, Any], corr_id: str) -> ExecutionCostContract:
    try:
        margin_items = [OrderMarginInput(**order)]
        margin_rows = orders_service.order_margins(kite, margin_items, corr_id, mode="compact")
        charges_items = [_charges_input_from_order(order)]
        charge_rows = orders_service.charges_orders(kite, charges_items, corr_id)
    except Exception as exc:
        return ExecutionCostContract(charges_status=ChargesStatus.UNAVAILABLE, raw={"error": str(exc)})

    margin_required = _decimal(getattr(margin_rows[0], "total", 0) if margin_rows else 0)
    charges = dict(getattr(charge_rows[0], "charges", {}) if charge_rows else {})
    return _contract_from_kite_payload(
        margin_required=margin_required,
        charges=charges,
        raw={
            "margin": _dump_model(margin_rows[0]) if margin_rows else None,
            "charges": _dump_model(charge_rows[0]) if charge_rows else None,
        },
    )
