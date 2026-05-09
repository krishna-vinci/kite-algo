from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def evaluate_option_rules(*, metrics: Dict[str, Any], rules: Iterable[Any], precedence: Iterable[Any]) -> Optional[Any]:
    role_order = [str(role) for role in (precedence or [])]
    normalized_rules = list(rules or [])

    for role in role_order:
        for rule in normalized_rules:
            rule_role = _get_rule_value(rule, "role")
            if str(rule_role) != role:
                continue

            metric_key = _get_rule_value(rule, "metric")
            metric_lookup_key = metric_key.value if hasattr(metric_key, "value") else metric_key
            value = metrics.get(metric_lookup_key)
            if value is None:
                continue

            operator = str(_get_rule_value(rule, "operator") or "")
            threshold = _get_rule_value(rule, "threshold")

            if operator == "lte" and float(value) <= float(threshold):
                return rule
            if operator == "gte" and float(value) >= float(threshold):
                return rule

    return None


def _get_rule_value(rule: Any, key: str) -> Any:
    if isinstance(rule, dict):
        return rule.get(key)
    return getattr(rule, key, None)
