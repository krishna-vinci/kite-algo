from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import OptionRunState
from .planner import build_entry_order_plan


def build_entry_preview_packet(run: OptionRunState) -> dict[str, Any]:
    resolved_legs = [dict(leg) for leg in run.legs]
    order_plan = build_entry_order_plan(resolved_legs, product=str(run.product or ""))
    return {
        "strategy_run_id": run.strategy_run_id,
        "product": run.product,
        "resolved_legs": resolved_legs,
        "order_plan": order_plan,
        "margin": {"required": 0.0, "source": "deterministic_stub"},
        "charges": {"estimated": 0.0, "source": "deterministic_stub"},
        "protection_preview": run.protection,
    }


def build_exit_preview_packet(run: OptionRunState) -> dict[str, Any]:
    open_legs = _derive_open_exit_legs(run)
    return {
        "strategy_run_id": run.strategy_run_id,
        "product": run.product,
        "open_legs": open_legs,
        "order_plan": [
            {
                "leg_id": leg["leg_id"],
                "tradingsymbol": leg.get("tradingsymbol"),
                "quantity": leg["open_quantity"],
                "transaction_type": "SELL" if leg.get("entry_side") == "BUY" else "BUY",
                "product": run.product,
            }
            for leg in open_legs
            if int(leg.get("open_quantity") or 0) > 0
        ],
        "margin": {"required": 0.0, "source": "deterministic_stub"},
        "charges": {"estimated": 0.0, "source": "deterministic_stub"},
    }


def _derive_open_exit_legs(run: OptionRunState) -> list[dict[str, Any]]:
    # Prefer trade-derived open quantity when available.
    if run.trades:
        signed_qty_by_leg: dict[str, int] = defaultdict(int)
        meta_by_leg: dict[str, dict[str, Any]] = {}
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
            if leg_id not in meta_by_leg:
                meta_by_leg[leg_id] = {
                    "tradingsymbol": trade.get("tradingsymbol"),
                    "entry_side": trade.get("entry_side")
                    or ("BUY" if signed_qty_by_leg[leg_id] >= 0 else "SELL"),
                }

        open_legs = []
        for leg_id, net in signed_qty_by_leg.items():
            if net == 0:
                continue
            entry_side = "BUY" if net > 0 else "SELL"
            meta = meta_by_leg.get(leg_id, {})
            open_legs.append(
                {
                    "leg_id": leg_id,
                    "tradingsymbol": meta.get("tradingsymbol"),
                    "entry_side": meta.get("entry_side") or entry_side,
                    "open_quantity": abs(int(net)),
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
                "tradingsymbol": leg.get("tradingsymbol"),
                "entry_side": transaction_type,
                "open_quantity": quantity,
            }
        )
    return open_legs
