from __future__ import annotations

from typing import Any

from options.execution.models import OptionRunState

from .runtime import evaluate_option_protection_state, normalize_protection_config


def replay_option_protection(
    *,
    run: OptionRunState,
    metric_snapshots: list[dict[str, Any]],
    protection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = normalize_protection_config(protection if protection is not None else run.protection)
    events: list[dict[str, Any]] = []
    for index, snapshot in enumerate(metric_snapshots):
        state = evaluate_option_protection_state(
            run=run,
            protection=config,
            metric_snapshot=snapshot,
        )
        events.append(
            {
                "index": index,
                "triggered": bool(state.get("triggered")),
                "matched_rule": state.get("matched_rule"),
                "metrics": state.get("metrics") or {},
                "recommended_exit_orders": state.get("recommended_exit_orders") or [],
            }
        )
    return {"events": events}
