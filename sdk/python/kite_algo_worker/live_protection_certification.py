from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional


IMMEDIATE_THRESHOLD_FACTOR = 0.8
MIN_IMMEDIATE_THRESHOLD_PCT = 0.0001
FLATTEN_SUCCESS_STATUSES = frozenset({"closed", "completed", "exited", "flattened", "ok", "success"})
TERMINAL_RUN_STATUSES = frozenset({"closed", "completed", "exited"})


def normalize_leg_side(side: Any, *, net_quantity: Any = None) -> Optional[str]:
    normalized = str(side or "").strip().upper()
    if normalized in {"BUY", "LONG"}:
        return "BUY"
    if normalized in {"SELL", "SHORT"}:
        return "SELL"
    try:
        quantity = int(net_quantity) if net_quantity is not None else 0
    except Exception:
        quantity = 0
    if quantity > 0:
        return "BUY"
    if quantity < 0:
        return "SELL"
    return None


def choose_immediate_threshold(
    current_pnl_pct: float | int | None,
    *,
    rule_kind: Literal["stoploss", "target"],
    factor: float = IMMEDIATE_THRESHOLD_FACTOR,
    min_threshold_pct: float = MIN_IMMEDIATE_THRESHOLD_PCT,
) -> Optional[float]:
    if current_pnl_pct is None:
        return None
    pnl_pct = float(current_pnl_pct)
    if factor <= 0 or factor >= 1:
        raise ValueError("factor must be between 0 and 1")
    if min_threshold_pct <= 0:
        raise ValueError("min_threshold_pct must be > 0")
    if rule_kind == "stoploss":
        if pnl_pct >= 0:
            return None
        magnitude = abs(pnl_pct)
    elif rule_kind == "target":
        if pnl_pct <= 0:
            return None
        magnitude = pnl_pct
    else:
        raise ValueError("rule_kind must be 'stoploss' or 'target'")
    return round(max(min_threshold_pct, magnitude * factor), 4)


def flatten_result_succeeded(flatten_result: Mapping[str, Any] | None) -> bool:
    if not flatten_result:
        return False
    status = str(flatten_result.get("status") or "").strip().lower()
    if not status:
        return False
    return status in FLATTEN_SUCCESS_STATUSES


@dataclass(frozen=True)
class CertificationScenarioVerdict:
    scenario: str
    status: Literal["passed", "failed"]
    reason: str
    expected_rule: Optional[str] = None
    observed_rule: Optional[str] = None
    flatten_required: bool = False
    flatten_attempted: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_scenario_verdict(
    scenario: str,
    *,
    expected_rule: Optional[str] = None,
    observed_facts: Mapping[str, Any] | None = None,
) -> CertificationScenarioVerdict:
    facts = dict(observed_facts or {})
    error = facts.get("error")
    observed_rule = facts.get("triggered_rule")
    flatten_required = bool(facts.get("flatten_required"))
    flatten_attempted = bool(facts.get("flatten_attempted"))

    if error:
        return CertificationScenarioVerdict(
            scenario=scenario,
            status="failed",
            reason=str(error),
            expected_rule=expected_rule,
            observed_rule=observed_rule,
            flatten_required=flatten_required,
            flatten_attempted=flatten_attempted,
            details=facts,
        )

    if facts.get("cleanup_error"):
        return CertificationScenarioVerdict(
            scenario=scenario,
            status="failed",
            reason=f"cleanup safety check failed: {facts['cleanup_error']}",
            expected_rule=expected_rule,
            observed_rule=observed_rule,
            flatten_required=flatten_required,
            flatten_attempted=flatten_attempted,
            details=facts,
        )

    if flatten_required and not facts.get("flatten_confirmed"):
        return CertificationScenarioVerdict(
            scenario=scenario,
            status="failed",
            reason="emergency flatten required but KITE_ALGO_CONFIRM_FLATTEN=YES was not set",
            expected_rule=expected_rule,
            observed_rule=observed_rule,
            flatten_required=True,
            flatten_attempted=False,
            details=facts,
        )

    if flatten_attempted and not flatten_result_succeeded(facts.get("flatten_result")):
        return CertificationScenarioVerdict(
            scenario=scenario,
            status="failed",
            reason="emergency flatten was attempted but did not report a successful terminal status",
            expected_rule=expected_rule,
            observed_rule=observed_rule,
            flatten_required=flatten_required,
            flatten_attempted=True,
            details=facts,
        )

    if expected_rule is None:
        version_before = facts.get("version_before")
        version_after = facts.get("version_after")
        generation_before = facts.get("generation_before")
        generation_after = facts.get("generation_after")
        mutated = bool(facts.get("mutated"))
        if version_before is not None and version_after is not None:
            mutated = mutated or int(version_after) > int(version_before)
        if generation_before is not None and generation_after is not None:
            mutated = mutated and int(generation_after) > int(generation_before)
        if mutated:
            return CertificationScenarioVerdict(
                scenario=scenario,
                status="passed",
                reason="protection patch advanced runtime generation/version",
                details=facts,
            )
        return CertificationScenarioVerdict(
            scenario=scenario,
            status="failed",
            reason="protection patch did not advance runtime generation/version",
            details=facts,
        )

    run_status = str(facts.get("run_status") or "").strip().lower()
    broker_flat = bool(facts.get("broker_flat"))
    worker_orders_visible = bool(facts.get("worker_orders_visible"))
    worker_trades_visible = bool(facts.get("worker_trades_visible"))

    if (
        observed_rule == expected_rule
        and run_status in TERMINAL_RUN_STATUSES
        and broker_flat
        and worker_orders_visible
        and worker_trades_visible
        and not flatten_attempted
    ):
        return CertificationScenarioVerdict(
            scenario=scenario,
            status="passed",
            reason="observed expected protection trigger and clean terminal exit",
            expected_rule=expected_rule,
            observed_rule=observed_rule,
            flatten_required=flatten_required,
            flatten_attempted=flatten_attempted,
            details=facts,
        )

    if observed_rule != expected_rule and facts.get("threshold_pct") is None and facts.get("current_pnl_pct") is not None:
        reason = "current pnl sign could not support an immediate threshold for the requested rule"
    elif observed_rule == expected_rule and run_status not in TERMINAL_RUN_STATUSES:
        reason = "expected rule triggered but run did not reach a clean terminal state"
    elif observed_rule == expected_rule and not broker_flat:
        reason = "expected rule triggered but broker exposure was not confirmed flat"
    elif observed_rule == expected_rule and not worker_orders_visible:
        reason = "expected rule triggered but worker order visibility was incomplete"
    elif observed_rule == expected_rule and not worker_trades_visible:
        reason = "expected rule triggered but worker trade visibility was incomplete"
    elif flatten_attempted:
        reason = "expected rule triggered but required emergency flatten fallback"
    else:
        reason = "observed protection trigger did not match expectation"
    return CertificationScenarioVerdict(
        scenario=scenario,
        status="failed",
        reason=reason,
        expected_rule=expected_rule,
        observed_rule=observed_rule,
        flatten_required=flatten_required,
        flatten_attempted=flatten_attempted,
        details=facts,
    )


__all__ = [
    "CertificationScenarioVerdict",
    "FLATTEN_SUCCESS_STATUSES",
    "IMMEDIATE_THRESHOLD_FACTOR",
    "MIN_IMMEDIATE_THRESHOLD_PCT",
    "TERMINAL_RUN_STATUSES",
    "normalize_leg_side",
    "choose_immediate_threshold",
    "flatten_result_succeeded",
    "summarize_scenario_verdict",
]
