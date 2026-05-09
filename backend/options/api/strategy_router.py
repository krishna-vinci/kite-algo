from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.options.strategy import StrategyProtectionPreferences, compile_option_strategy_preview

router = APIRouter(prefix="/api/options", tags=["Options"])


@router.post("/strategies/preview")
async def preview_option_strategy(payload: dict):
    try:
        preview = compile_option_strategy_preview(
            underlying=str(payload.get("underlying") or "").upper(),
            template_id=payload.get("template_id"),
            strategy_type=str(payload.get("strategy_type") or payload.get("template_id") or "manual"),
            current_spot=payload.get("current_spot"),
            legs=payload.get("legs") or payload.get("selected_strikes") or [],
            protection_preferences=StrategyProtectionPreferences.from_payload(payload.get("protection_config")),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"strategy": preview.model_dump(mode="json")}
