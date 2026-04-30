from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from options.execution import (
    OptionRunCreateRequest,
    get_option_run_store,
    mark_cleanup_required,
    mark_closed,
    mark_entering,
    mark_entry_previewed,
    mark_exit_previewed,
    mark_exiting,
    mark_partial_entry,
    mark_partial_exit,
)
from options.execution.previews import build_entry_preview_packet, build_exit_preview_packet
from options.execution.runtime_instance import (
    OptionExecutionRuntimeInstance,
    get_option_execution_runtime_instance,
)
from options.execution.store import OptionRunStore
from options.execution.updates import summarize_order_results

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


def _invalid_transition(exc: Exception):
    raise HTTPException(
        status_code=409,
        detail={
            "code": "OPTION_RUN_INVALID_TRANSITION",
            "message": str(exc),
        },
    )


def _normalize_injected_orders(results: list[dict], *, phase: str) -> list[dict]:
    normalized: list[dict] = []
    for item in results:
        entry = dict(item)
        entry.setdefault("status", "filled")
        entry["status"] = str(entry.get("status") or "").lower()
        entry["phase"] = phase
        normalized.append(entry)
    return normalized


def _normalize_injected_trades(results: list[dict], *, phase: str) -> list[dict]:
    normalized: list[dict] = []
    for item in results:
        entry = dict(item)
        entry["phase"] = phase
        normalized.append(entry)
    return normalized


@router.post("/runs")
async def create_option_run(
    payload: OptionRunCreateRequest,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = store.create_run(payload)
    return run.__dict__


@router.get("/runs")
async def list_option_runs(store: OptionRunStore = Depends(get_option_run_store)):
    return {"runs": [run.__dict__ for run in store.list_runs()]}


@router.get("/runs/{strategy_run_id}")
async def get_option_run(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return run.__dict__


@router.post("/runs/{strategy_run_id}/preview-entry")
async def preview_option_run_entry(
    strategy_run_id: str,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
):
    _ = payload
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_entry_previewed(run)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)
    return build_entry_preview_packet(run)


@router.post("/runs/{strategy_run_id}/enter")
async def enter_option_run(
    strategy_run_id: str,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    payload = payload or {}
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_entering(run)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)

    order_plan = runtime.build_entry_plan(run)
    if payload.get("order_results") is None and payload.get("trade_results") is None:
        order_results, trade_results = runtime.default_entry_results(run)
    else:
        order_results = _normalize_injected_orders(payload.get("order_results") or [], phase="entry")
        trade_results = _normalize_injected_trades(payload.get("trade_results") or [], phase="entry")

    if order_results:
        run = store.record_orders(strategy_run_id, order_results)
    if trade_results:
        run = store.record_trades(strategy_run_id, trade_results)

    summary = summarize_order_results(order_plan, order_results)
    if summary.failed_legs:
        try:
            run = mark_partial_entry(
                run,
                completed_legs=summary.completed_legs,
                failed_legs=[],
                pending_legs=summary.pending_legs or summary.failed_legs,
            )
            run.failed_legs = list(summary.failed_legs)
            run = mark_cleanup_required(run)
        except ValueError as exc:
            _invalid_transition(exc)
    else:
        try:
            run = mark_partial_entry(
                run,
                completed_legs=summary.completed_legs,
                failed_legs=[],
                pending_legs=summary.pending_legs,
            )
        except ValueError as exc:
            _invalid_transition(exc)
    store.save_run(run)

    return {
        "strategy_run_id": run.strategy_run_id,
        "status": run.status,
        "completed_legs": list(run.completed_legs),
        "failed_legs": list(run.failed_legs),
        "pending_legs": list(run.pending_legs),
        "orders": list(order_results),
        "trades": list(trade_results),
    }


@router.post("/runs/{strategy_run_id}/preview-exit")
async def preview_option_run_exit(
    strategy_run_id: str,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
):
    _ = payload
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_exit_previewed(run)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)
    return build_exit_preview_packet(run)


@router.post("/runs/{strategy_run_id}/exit")
async def exit_option_run(
    strategy_run_id: str,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    payload = payload or {}
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_exiting(run, pending_legs=run.completed_legs)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)

    order_plan = runtime.build_exit_plan(run)
    if payload.get("order_results") is None and payload.get("trade_results") is None:
        order_results, trade_results = runtime.default_exit_results(run)
    else:
        order_results = _normalize_injected_orders(payload.get("order_results") or [], phase="exit")
        trade_results = _normalize_injected_trades(payload.get("trade_results") or [], phase="exit")

    if order_results:
        run = store.record_orders(strategy_run_id, order_results)
    if trade_results:
        run = store.record_trades(strategy_run_id, trade_results)

    summary = summarize_order_results(order_plan, order_results)
    if summary.failed_legs or summary.pending_legs:
        try:
            run = mark_partial_exit(
                run,
                remaining_open_legs=summary.failed_legs + summary.pending_legs,
                failed_legs=[],
            )
        except ValueError as exc:
            _invalid_transition(exc)
        run.failed_legs = list(summary.failed_legs)
    else:
        try:
            run = mark_closed(run)
        except ValueError as exc:
            _invalid_transition(exc)
    store.save_run(run)

    return {
        "strategy_run_id": run.strategy_run_id,
        "status": run.status,
        "completed_legs": list(run.completed_legs),
        "failed_legs": list(run.failed_legs),
        "pending_legs": list(run.pending_legs),
        "orders": list(order_results),
        "trades": list(trade_results),
    }


@router.get("/runs/{strategy_run_id}/orders")
async def list_option_run_orders(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return {"strategy_run_id": strategy_run_id, "orders": list(run.orders)}


@router.get("/runs/{strategy_run_id}/trades")
async def list_option_run_trades(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return {"strategy_run_id": strategy_run_id, "trades": list(run.trades)}


@router.get("/runs/{strategy_run_id}/state")
async def get_option_run_state(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return {
        "strategy_run_id": strategy_run_id,
        "state": {
            "status": run.status,
            "completed_legs": list(run.completed_legs),
            "failed_legs": list(run.failed_legs),
            "pending_legs": list(run.pending_legs),
            "product": run.product,
        },
    }
