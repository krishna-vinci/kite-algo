from __future__ import annotations

from collections import defaultdict
from typing import Any

from backend.options.execution.models import OptionRunState

from .models import SUPPORTED_PROTECTION_METRIC_KEYS


def derive_protection_metrics(
    run: OptionRunState,
    *,
    metric_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    snapshot = dict(metric_snapshot or {})

    for key in SUPPORTED_PROTECTION_METRIC_KEYS:
        if key in snapshot:
            metrics[key] = snapshot.get(key)

    if "open_quantity" not in metrics:
        metrics["open_quantity"] = derive_open_quantity(run)

    metadata_metrics = _extract_metadata_metrics(run)
    for key, value in metadata_metrics.items():
        metrics.setdefault(key, value)

    return metrics


def derive_open_quantity(run: OptionRunState) -> int:
    if run.trades:
        net_by_leg: dict[str, int] = defaultdict(int)
        for trade in run.trades:
            leg_id = str(trade.get("leg_id") or "")
            if not leg_id:
                continue
            quantity = int(trade.get("quantity") or 0)
            side = str(trade.get("transaction_type") or "").upper()
            if side == "BUY":
                net_by_leg[leg_id] += quantity
            elif side == "SELL":
                net_by_leg[leg_id] -= quantity
        open_qty = sum(abs(value) for value in net_by_leg.values() if value != 0)
        if open_qty > 0:
            return int(open_qty)

    if run.completed_legs:
        return int(len(run.completed_legs))

    return 0


def _extract_metadata_metrics(run: OptionRunState) -> dict[str, Any]:
    payload = run.metadata if isinstance(run.metadata, dict) else {}
    maybe_metrics = payload.get("protection_metrics")
    if not isinstance(maybe_metrics, dict):
        return {}

    normalized: dict[str, Any] = {}
    for key in SUPPORTED_PROTECTION_METRIC_KEYS:
        if key in maybe_metrics:
            normalized[key] = maybe_metrics.get(key)
    return normalized
