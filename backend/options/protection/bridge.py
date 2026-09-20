"""The bridge between compiler rule vocabulary and the enforced runtime (D-3).

Two vocabularies grew up apart. The option-compiler speaks ``MetricKind`` —
``index_price``, ``combined_premium_points``, ``basket_mtm_rupees`` — because that is
what a structure author reasons about. The enforced protection runtime speaks in the
terms it can actually compute from a live book: an index level, a combined premium,
and per-position and basket P&L percentages.

They must meet in exactly ONE place. If the compiler's vocabulary leaked into the
runtime, or the runtime's leaked into the compiler, every future rule would have to
be written twice and the two copies would drift — and the drift would be discovered
by a rule that triggers on paper and not live. So this module is the only adapter,
and it is bidirectional on purpose: a rule is written once and a trigger can be
reported back in the vocabulary its author used.

Translation that cannot be done is refused rather than guessed. An unknown metric or
an unknown direction is a rule the runtime would silently never fire, and a
protective rule that never fires is worse than one that errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

#: Compiler ``MetricKind`` value → the runtime metric that carries it.
COMPILER_TO_RUNTIME: Dict[str, str] = {
    # The underlying's price: the runtime's index level, which is also what the
    # square-off schedule resolves against.
    "index_price": "index_ltp",
    # The structure's net premium in points, which the runtime reads off the legs.
    "combined_premium_points": "combined_premium",
    # The structure's mark-to-market in rupees. The runtime aggregates this as a
    # percentage, which is the only form it can compute without a capital base.
    "basket_mtm_rupees": "basket_mtm_rupees",
}

#: The reverse map, so a runtime trigger can be reported in compiler vocabulary.
RUNTIME_TO_COMPILER: Dict[str, str] = {
    runtime: compiler for compiler, runtime in COMPILER_TO_RUNTIME.items()
}

#: Directions a threshold can take, in either vocabulary.
DIRECTIONS = ("above", "below")

#: Roles, mirroring the compiler's ``RuleRole`` without importing it: the bridge
#: must not depend on the module it is decoupling from.
ROLES = ("emergency_guard", "hard_stop", "profit_target", "trailing_stop")


class BridgeRefusal(Exception):
    """A rule the runtime could never evaluate, so translating it would be a lie."""

    reason_code = "RULE_NOT_BRIDGEABLE"

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}


@dataclass(frozen=True)
class BridgedRule:
    """One compiler rule expressed in the runtime's terms."""

    runtime_metric: str
    compiler_metric: str
    direction: str
    threshold: float
    role: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.runtime_metric,
            "direction": self.direction,
            "threshold": self.threshold,
            "role": self.role,
            "compiler_metric": self.compiler_metric,
        }


def runtime_metric_for(metric_kind: Any) -> str:
    """The runtime metric a compiler ``MetricKind`` is carried by."""
    key = str(getattr(metric_kind, "value", metric_kind) or "")
    runtime = COMPILER_TO_RUNTIME.get(key)
    if runtime is None:
        raise BridgeRefusal(
            {
                "compiler_metric": key,
                "bridgeable": sorted(COMPILER_TO_RUNTIME),
                "message": "This metric has no runtime carrier, so a rule on it could never fire",
            }
        )
    return runtime


def compiler_metric_for(runtime_metric: Any) -> str:
    """The compiler metric a runtime metric reports back as."""
    key = str(runtime_metric or "")
    compiler = RUNTIME_TO_COMPILER.get(key)
    if compiler is None:
        raise BridgeRefusal(
            {
                "runtime_metric": key,
                "bridgeable": sorted(RUNTIME_TO_COMPILER),
            }
        )
    return compiler


def _as_threshold(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise BridgeRefusal({"threshold": value, "reason": str(exc)}) from exc


def _as_direction(value: Any) -> str:
    # ``str(member)`` on a ``str``-Enum yields "RuleRole.NAME", not the value, so
    # the enum's own value is what must be read.
    direction = str(getattr(value, "value", value) or "").strip().lower()
    if direction not in DIRECTIONS:
        raise BridgeRefusal({"direction": direction, "allowed": list(DIRECTIONS)})
    return direction


def _as_role(value: Any) -> str:
    role = str(getattr(value, "value", value) or "").strip().lower()
    if role not in ROLES:
        raise BridgeRefusal({"role": role, "allowed": list(ROLES)})
    return role


def bridge_rule(rule: Mapping[str, Any]) -> BridgedRule:
    """Translate one compiler rule into the shape the runtime evaluates.

    Every field is validated rather than defaulted: a rule whose direction or role
    could not be read is a rule the runtime would never fire, and a protective rule
    that silently never fires is the worst failure this system can have.
    """
    if not isinstance(rule, Mapping):
        raise BridgeRefusal({"reason": "rule is not an object"})
    metric = rule.get("metric")
    return BridgedRule(
        runtime_metric=runtime_metric_for(metric),
        compiler_metric=str(getattr(metric, "value", metric) or ""),
        direction=_as_direction(rule.get("direction")),
        threshold=_as_threshold(rule.get("threshold")),
        role=_as_role(rule.get("role")),
    )


def bridge_rules(rules: Any) -> List[BridgedRule]:
    if not isinstance(rules, (list, tuple)):
        raise BridgeRefusal({"reason": "rules must be a list"})
    return [bridge_rule(rule) for rule in rules]


def report_trigger(trigger: Mapping[str, Any]) -> Dict[str, Any]:
    """Restate a runtime trigger in the vocabulary its author wrote it in.

    The runtime reports ``metric``; an operator reading a report should not have to
    keep the mapping in their head, and a report that cannot be translated says so
    rather than dropping the field.
    """
    payload = dict(trigger or {})
    runtime_metric = str(payload.get("metric") or "")
    try:
        payload["compiler_metric"] = compiler_metric_for(runtime_metric)
    except BridgeRefusal:
        payload["compiler_metric"] = None
    return payload


def bridge_coverage() -> Dict[str, str]:
    """The whole mapping, for the test that pins it in both directions."""
    return dict(COMPILER_TO_RUNTIME)
