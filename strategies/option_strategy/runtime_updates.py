from __future__ import annotations

from typing import Any, Dict

from .compiler import compile_option_strategy_preview
from .models import StrategyProtectionPreferences


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
        "risk_controls": prefs.model_dump(mode="json"),
        "capabilities": {"can_edit_risk": True, "edit_risk_reason": None},
        "runtime_config": {
            "rules": preview.get("rules") or [],
            "precedence": preview.get("precedence") or [],
        },
    }
