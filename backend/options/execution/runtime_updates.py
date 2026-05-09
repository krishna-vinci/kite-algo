from __future__ import annotations

from typing import Any, Dict

from backend.options.strategy import StrategyProtectionPreferences, compile_option_strategy_preview


def _risk_schema_from_preview(preview: Dict[str, Any]) -> list[Dict[str, Any]]:
    schema: list[Dict[str, Any]] = []
    for key, descriptor in dict(preview.get("inputs") or {}).items():
        value = dict(descriptor or {})
        if value.get("visible") is False:
            continue
        schema.append(
            {
                "key": key,
                "label": value.get("label") or key,
                "type": "number",
                "unit": value.get("unit"),
                "group": value.get("group"),
                "required": bool(value.get("required")),
                "recommended": bool(value.get("recommended")),
                "value": value.get("value"),
            }
        )
    return schema


def build_option_run_summary_fields(canonical_strategy: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [
        {
            "key": field["key"],
            "label": field["label"],
            "value": field.get("value"),
            "unit": field.get("unit"),
            "group": field.get("group"),
        }
        for field in _risk_schema_from_preview(canonical_strategy)
        if field.get("value") not in (None, "")
    ]


def build_option_run_capabilities(
    run: Dict[str, Any],
    *,
    canonical_strategy: Dict[str, Any] | None = None,
    is_open: bool,
    mode: str,
) -> Dict[str, Any]:
    canonical = dict(canonical_strategy or run.get("canonical_strategy") or {})
    editable = bool(is_open) and str(mode or "") == "paper"
    return {
        "can_edit_risk": editable,
        "edit_risk_reason": None if editable else "Only open paper strategies support runtime risk edits",
        "can_exit_strategy": editable,
        "exit_reason": None if editable else "Only open monitored paper strategies support strategy-level exit",
        "allowed_actions": ["edit_risk", "exit_strategy"] if editable else [],
        "risk_schema": _risk_schema_from_preview(canonical) if editable else [],
    }


def apply_protection_patch(run: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    canonical = dict(run.get("canonical_strategy") or {})
    existing = dict(canonical.get("protection_preferences") or {})
    existing.update(patch)
    prefs = StrategyProtectionPreferences.from_payload(existing)

    preview = compile_option_strategy_preview(
        underlying=str(run["underlying"]).upper(),
        template_id=canonical.get("template_id"),
        strategy_type=canonical.get("inferred_structure") or canonical.get("user_intent") or "custom",
        current_spot=canonical.get("current_spot"),
        legs=run.get("selected_legs") or [],
        protection_preferences=prefs,
    ).model_dump(mode="json")

    preview["protection_preferences"] = prefs.model_dump(mode="json")
    return {
        "canonical_strategy": preview,
        "summary_fields": build_option_run_summary_fields(preview),
        "capabilities": build_option_run_capabilities(run, canonical_strategy=preview, is_open=True, mode=str(run.get("execution_mode") or "paper")),
        "runtime_config": {
            "rules": preview.get("rules") or [],
            "precedence": preview.get("precedence") or [],
        },
    }
