from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class ExecutionUpdateSummary:
    completed_legs: list[str]
    failed_legs: list[str]
    pending_legs: list[str]


def summarize_order_results(
    order_plan: Iterable[dict[str, Any]],
    order_results: Iterable[dict[str, Any]],
) -> ExecutionUpdateSummary:
    expected = [str(order.get("leg_id") or "") for order in order_plan if str(order.get("leg_id") or "")]

    status_by_leg: dict[str, str] = {}
    for result in order_results:
        leg_id = str(result.get("leg_id") or "")
        if not leg_id:
            continue
        status_by_leg[leg_id] = str(result.get("status") or "").lower()

    completed: list[str] = []
    failed: list[str] = []
    pending: list[str] = []

    for leg_id in expected:
        status = status_by_leg.get(leg_id, "pending")
        if status in {"filled", "complete", "completed", "success"}:
            completed.append(leg_id)
        elif status in {"rejected", "failed", "cancelled", "canceled"}:
            failed.append(leg_id)
        else:
            pending.append(leg_id)

    return ExecutionUpdateSummary(completed_legs=completed, failed_legs=failed, pending_legs=pending)
