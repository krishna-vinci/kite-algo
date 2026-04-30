from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from options.execution.store import OptionRunStore, get_option_run_store
from options.protection.models import (
    OptionProtectionConfigUpdateRequest,
    OptionProtectionMetricSnapshotModel,
    OptionProtectionReplayRequest,
)
from options.protection.replay import replay_option_protection
from options.protection.runtime import evaluate_option_protection_state, normalize_protection_config

router = APIRouter(prefix="/api/options", tags=["Options"])


def _get_run_or_404(store: OptionRunStore, strategy_run_id: str):
    try:
        return store.get_run(strategy_run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "OPTION_RUN_NOT_FOUND",
                "message": f"Option run not found: {strategy_run_id}",
            },
        ) from exc


def _normalize_or_422(protection: dict[str, Any] | None):
    try:
        return normalize_protection_config(protection)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "OPTION_PROTECTION_INVALID_CONFIG",
                "message": str(exc),
            },
        ) from exc


def _snapshot_to_dict(snapshot: OptionProtectionMetricSnapshotModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(snapshot, OptionProtectionMetricSnapshotModel):
        return snapshot.model_dump(mode="json", exclude_none=True)
    if isinstance(snapshot, dict):
        try:
            return OptionProtectionMetricSnapshotModel.model_validate(snapshot).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "OPTION_PROTECTION_INVALID_METRIC_SNAPSHOT",
                    "message": str(exc),
                },
            ) from exc
    raise HTTPException(
        status_code=422,
        detail={
            "code": "OPTION_PROTECTION_INVALID_METRIC_SNAPSHOT",
            "message": "Metric snapshot must be an object",
        },
    )


@router.get("/runs/{strategy_run_id}/protection")
async def get_option_run_protection(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    protection_payload = run.protection if isinstance(run.protection, dict) else None
    return {
        "strategy_run_id": strategy_run_id,
        "protection": protection_payload,
    }


@router.put("/runs/{strategy_run_id}/protection")
async def update_option_run_protection(
    strategy_run_id: str,
    payload: OptionProtectionConfigUpdateRequest,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    normalized = _normalize_or_422(payload.model_dump(mode="json"))
    run.protection = normalized
    store.save_run(run)
    return {
        "strategy_run_id": strategy_run_id,
        "protection": run.protection,
    }


@router.get("/runs/{strategy_run_id}/protection/state")
async def get_option_run_protection_state(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    _normalize_or_422(run.protection if isinstance(run.protection, dict) else None)
    state = evaluate_option_protection_state(run=run, metric_snapshot=None)
    return {
        "strategy_run_id": strategy_run_id,
        "triggered": state["triggered"],
        "matched_rule": state["matched_rule"],
        "metrics": state["metrics"],
        "recommended_exit_orders": state["recommended_exit_orders"],
    }


@router.post("/runs/{strategy_run_id}/protection/replay")
async def replay_option_run_protection(
    strategy_run_id: str,
    payload: OptionProtectionReplayRequest,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)

    if not payload.metric_snapshots:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "OPTION_PROTECTION_REPLAY_SNAPSHOTS_REQUIRED",
                "message": "metric_snapshots must contain at least one snapshot",
            },
        )

    protection = payload.protection.model_dump(mode="json") if payload.protection is not None else run.protection
    normalized = _normalize_or_422(protection if isinstance(protection, dict) else None)

    snapshots = [_snapshot_to_dict(snapshot) for snapshot in payload.metric_snapshots]
    replay_packet = replay_option_protection(run=run, metric_snapshots=snapshots, protection=normalized)
    return {
        "strategy_run_id": strategy_run_id,
        **replay_packet,
    }
