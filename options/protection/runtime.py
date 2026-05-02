from __future__ import annotations

from copy import deepcopy
from typing import Any

from options.execution.models import OptionRunState

from .evaluator import evaluate_option_rules
from .exit_builder import build_grouped_exit_orders
from .metrics import derive_protection_metrics


def normalize_protection_config(protection: Any) -> dict[str, Any]:
    if protection is None:
        return {"rules": [], "precedence": []}
    if not isinstance(protection, dict):
        raise ValueError("Protection config must be an object")

    raw_rules = protection.get("rules")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise ValueError("Protection rules must be a list")

    normalized_rules: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise ValueError(f"Protection rule at index {index} must be an object")
        metric = str(raw.get("metric") or "").strip()
        operator = str(raw.get("operator") or "").strip().lower()
        if metric not in {
            "index_ltp",
            "combined_premium",
            "combined_premium_change_pct",
            "strategy_mtm",
            "open_quantity",
        }:
            raise ValueError(f"Unsupported protection metric: {metric or '<missing>'}")
        if operator not in {"gte", "lte"}:
            raise ValueError(f"Unsupported protection operator: {operator or '<missing>'}")
        if "threshold" not in raw:
            raise ValueError(f"Protection rule at index {index} is missing threshold")
        threshold_value = raw.get("threshold")
        if threshold_value is None:
            raise ValueError(f"Protection rule at index {index} is missing threshold")
        try:
            threshold = float(threshold_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Protection rule at index {index} has invalid threshold") from exc

        normalized_rules.append(
            {
                "key": raw.get("key") or f"rule_{index + 1}",
                "metric": metric,
                "operator": operator,
                "threshold": threshold,
                "role": raw.get("role") or "exit",
                "action": raw.get("action") or "exit",
            }
        )

    raw_precedence = protection.get("precedence")
    if raw_precedence is None:
        precedence: list[str] = [str(rule.get("role") or "exit") for rule in normalized_rules]
    else:
        if not isinstance(raw_precedence, list):
            raise ValueError("Protection precedence must be a list")
        precedence = [str(item) for item in raw_precedence]

    return {"rules": normalized_rules, "precedence": precedence}


def evaluate_option_protection_state(
    *,
    run: OptionRunState,
    protection: dict[str, Any] | None = None,
    metric_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = normalize_protection_config(protection if protection is not None else run.protection)
    metrics = derive_protection_metrics(run, metric_snapshot=metric_snapshot)

    rules = list(config.get("rules") or [])
    if not rules:
        return {
            "triggered": False,
            "matched_rule": None,
            "metrics": metrics,
            "recommended_exit_orders": [],
        }

    matched = evaluate_option_rules(metrics=metrics, rules=rules, precedence=config.get("precedence") or [])
    if matched is None:
        return {
            "triggered": False,
            "matched_rule": None,
            "metrics": metrics,
            "recommended_exit_orders": [],
        }

    open_quantity = metrics.get("open_quantity")
    has_open_quantity = False
    try:
        has_open_quantity = float(open_quantity or 0) > 0
    except (TypeError, ValueError):
        has_open_quantity = False

    recommended_exit_orders: list[dict[str, Any]] = []
    if has_open_quantity:
        recommended_exit_orders = _build_run_exit_orders(run)

    return {
        "triggered": True,
        "matched_rule": deepcopy(matched) if isinstance(matched, dict) else matched,
        "metrics": metrics,
        "recommended_exit_orders": recommended_exit_orders,
    }


def _build_run_exit_orders(run: OptionRunState) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []

    if run.trades:
        leg_meta = {str(leg.get("leg_id") or ""): leg for leg in run.legs if leg.get("leg_id")}
        net_by_leg: dict[str, int] = {}
        for trade in run.trades:
            leg_id = str(trade.get("leg_id") or "")
            if not leg_id:
                continue
            quantity = int(trade.get("quantity") or 0)
            side = str(trade.get("transaction_type") or "").upper()
            if side == "BUY":
                net_by_leg[leg_id] = net_by_leg.get(leg_id, 0) + quantity
            elif side == "SELL":
                net_by_leg[leg_id] = net_by_leg.get(leg_id, 0) - quantity

        for leg_id, net_quantity in net_by_leg.items():
            if net_quantity == 0:
                continue
            leg = leg_meta.get(leg_id, {})
            trade = next((item for item in run.trades if str(item.get("leg_id") or "") == leg_id), {})
            positions.append(
                {
                    "exchange": leg.get("exchange") or trade.get("exchange") or "NFO",
                    "tradingsymbol": leg.get("tradingsymbol") or trade.get("tradingsymbol"),
                    "net_quantity": net_quantity,
                    "product": run.product,
                    "apply_market_protection": True,
                    "exit_order_type": leg.get("exit_order_type"),
                    "exit_price": leg.get("exit_price"),
                    "limit_price": leg.get("limit_price"),
                    "exit_variety": leg.get("exit_variety"),
                    "market_protection": leg.get("market_protection"),
                }
            )
    else:
        completed = {str(leg_id) for leg_id in run.completed_legs}
        candidate_legs = [
            leg for leg in run.legs if not completed or str(leg.get("leg_id") or "") in completed
        ]
        for leg in candidate_legs:
            quantity = int(leg.get("quantity") or 0)
            if quantity <= 0:
                continue
            side = str(leg.get("transaction_type") or "").upper()
            if side == "BUY":
                net_quantity = quantity
            elif side == "SELL":
                net_quantity = -quantity
            else:
                continue
            positions.append(
                {
                    "exchange": leg.get("exchange") or "NFO",
                    "tradingsymbol": leg.get("tradingsymbol"),
                    "net_quantity": net_quantity,
                    "product": run.product,
                    "apply_market_protection": True,
                    "exit_order_type": leg.get("exit_order_type"),
                    "exit_price": leg.get("exit_price"),
                    "limit_price": leg.get("limit_price"),
                    "exit_variety": leg.get("exit_variety"),
                    "market_protection": leg.get("market_protection"),
                }
            )

    orders, _skipped = build_grouped_exit_orders(
        positions,
        product_override=run.product,
        exit_order_type="MARKET",
    )
    return orders
