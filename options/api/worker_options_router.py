from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.routers.algo_workers import require_worker_token
from options.api.execution_router import (
    create_option_run,
    enter_option_run,
    exit_option_run,
    get_option_run_state,
    preview_option_run_entry,
    preview_option_run_exit,
)
from options.api.market_router import get_options_session_manager
from options.api.protection_router import (
    get_option_run_protection_state,
    replay_option_run_protection,
    update_option_run_protection,
)
from options.api.strategy_router import preview_option_strategy
from options.execution import OptionRunCreateRequest
from options.execution.runtime_instance import (
    OptionExecutionRuntimeInstance,
    get_option_execution_runtime_instance,
)
from options.execution.store import OptionRunStore, get_option_run_store
from options.market.service import OptionsMarketService
from options.protection.models import OptionProtectionConfigUpdateRequest, OptionProtectionReplayRequest

router = APIRouter(prefix="/api/algo-workers/worker/options", tags=["Algo Workers"])


@router.get("/underlyings/{underlying}/session")
async def get_worker_option_session(
    underlying: str,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_session(underlying)


@router.get("/underlyings/{underlying}/expiries")
async def list_worker_option_expiries(
    underlying: str,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).list_expiries(underlying)


@router.get("/underlyings/{underlying}/chain")
async def get_worker_option_chain(
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_chain(underlying, expiry)


@router.get("/underlyings/{underlying}/mini-chain")
async def get_worker_option_mini_chain(
    underlying: str,
    expiry: str | None = None,
    window: int = Query(default=5, ge=1, le=20),
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_mini_chain(underlying, expiry, window)


@router.get("/underlyings/{underlying}/greeks")
async def get_worker_option_greeks(
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_greeks(underlying, expiry)


@router.post("/underlyings/{underlying}/selection/resolve")
async def resolve_worker_option_selection(
    underlying: str,
    payload: dict,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).resolve_selection(underlying, payload)


@router.get("/underlyings/{underlying}/analytics/pcr")
async def get_worker_option_pcr(
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_pcr(underlying, expiry)


@router.get("/underlyings/{underlying}/analytics/max-pain")
async def get_worker_option_max_pain(
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_max_pain(underlying, expiry)


@router.post("/strategies/preview")
async def preview_worker_option_strategy(
    payload: dict,
    _token=Depends(require_worker_token),
):
    return await preview_option_strategy(payload)


@router.post("/runs")
async def create_worker_option_run(
    payload: OptionRunCreateRequest,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await create_option_run(payload, store)


@router.post("/runs/{strategy_run_id}/preview-entry")
async def preview_worker_option_run_entry(
    strategy_run_id: str,
    payload: dict | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await preview_option_run_entry(strategy_run_id, payload, store)


@router.post("/runs/{strategy_run_id}/enter")
async def enter_worker_option_run(
    strategy_run_id: str,
    payload: dict | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    return await enter_option_run(strategy_run_id, payload, store, runtime)


@router.post("/runs/{strategy_run_id}/preview-exit")
async def preview_worker_option_run_exit(
    strategy_run_id: str,
    payload: dict | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await preview_option_run_exit(strategy_run_id, payload, store)


@router.post("/runs/{strategy_run_id}/exit")
async def exit_worker_option_run(
    strategy_run_id: str,
    payload: dict | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    return await exit_option_run(strategy_run_id, payload, store, runtime)


@router.get("/runs/{strategy_run_id}/state")
async def get_worker_option_run_state(
    strategy_run_id: str,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await get_option_run_state(strategy_run_id, store)


@router.put("/runs/{strategy_run_id}/protection")
async def update_worker_option_run_protection(
    strategy_run_id: str,
    payload: OptionProtectionConfigUpdateRequest,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await update_option_run_protection(strategy_run_id, payload, store)


@router.get("/runs/{strategy_run_id}/protection/state")
async def get_worker_option_run_protection_state(
    strategy_run_id: str,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await get_option_run_protection_state(strategy_run_id, store)


@router.post("/runs/{strategy_run_id}/protection/replay")
async def replay_worker_option_run_protection(
    strategy_run_id: str,
    payload: OptionProtectionReplayRequest,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await replay_option_run_protection(strategy_run_id, payload, store)
