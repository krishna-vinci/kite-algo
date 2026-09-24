from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.routers.worker_protection import (
    observe_worker_option_protection_timeline_state,
    validate_worker_run_safety_token,
)
from backend.api.routers.worker_shared import (
    _repo,
    require_active_worker_run_session,
    require_worker_token,
)
from backend.api.services.hosted_attempt import (
    assert_hosted_discretionary_mutation_allowed,
    assert_hosted_run_binding,
    enforce_hosted_attempt_authority,
    enforce_hosted_read_authority,
    hosted_job_for_token,
)
from backend.options.api.execution_router import (
    create_option_run,
    enter_option_run,
    exit_option_run,
    get_option_run_state,
    preview_option_run_entry,
    preview_option_run_exit,
)
from backend.options.api.market_router import get_options_session_manager
from backend.options.api.protection_router import (
    get_option_run_protection_state,
    replay_option_run_protection,
    update_option_run_protection,
)
from backend.options.api.strategy_router import preview_option_strategy
from backend.options.execution import OptionRunCreateRequest
from backend.options.execution.models import OptionRunActionRequest
from backend.options.execution.runtime_instance import (
    OptionExecutionRuntimeInstance,
    get_option_execution_runtime_instance,
)
from backend.options.execution.store import OptionRunStore, get_option_run_store
from backend.options.market.service import OptionsMarketService
from backend.options.protection.models import OptionProtectionConfigUpdateRequest, OptionProtectionReplayRequest

router = APIRouter(prefix="/api/algo-workers/worker/options", tags=["Algo Workers"])


async def _guard_options_mutation(
    request,
    token,
    strategy_run_id: str,
    *,
    required_action: str,
    operation: str,
    action_payload: object = None,
):
    """Authorize an options mutation: operation permission + run binding + mode.

    Two independent gates:

    - **Operation permission (hosted only).** A hosted child must hold the action
      the mutation needs (``intents:submit`` to enter/exit, ``risk:update`` to
      change protection). A data-only token therefore cannot trade or alter
      protection. External tokens keep their established behavior.
    - **Identity + mode.** The caller must be bound to the worker run (an options
      id with no worker run, or another token's run, is refused), the attempt
      authority must be live, and hosted execution must be **paper**. A
      ``dry_run`` hosted mutation is rejected; previews (which do not mutate) are
      unaffected.

    Returns ``(run, hosted_job)`` so callers can apply hosted-only payload rules.
    """
    hosted_job = await hosted_job_for_token(request, token)
    if hosted_job is not None and required_action not in set(token.allowed_actions or []):
        raise HTTPException(
            status_code=403,
            detail={
                "rejection_reason": "HOSTED_OPERATION_NOT_PERMITTED",
                "operation": operation,
                "required_action": required_action,
            },
        )
    run = await _repo(request).get_run(strategy_run_id)
    if hosted_job is not None:
        assert_hosted_run_binding(run, token)
        await enforce_hosted_attempt_authority(request, token, run)
        if str(run.get("execution_mode") or "").lower() != "paper":
            raise HTTPException(
                status_code=409,
                detail={
                    "rejection_reason": "HOSTED_OPTIONS_MUTATION_PAPER_ONLY",
                    "execution_mode": str(run.get("execution_mode") or ""),
                },
            )
    if run is not None:
        await require_active_worker_run_session(request, run)
    if hosted_job is not None:
        # Phase 2: a hosted options CREATE/ENTER/EXIT/PROTECTION mutation is
        # discretionary exposure, so it goes through the governed execution
        # request contract or is refused by name. This sits AFTER the operation,
        # binding and mode gates so those refusals keep their existing codes and
        # meanings; external tokens never reach this branch.
        #
        # Caller-injected execution results are refused first, because that is a
        # MORE specific diagnosis of the same refusal and its named code is part
        # of the established contract.
        _reject_hosted_execution_injection(hosted_job, action_payload)
        assert_hosted_discretionary_mutation_allowed(run, operation=operation)
    return run, hosted_job


def _reject_hosted_execution_injection(hosted_job, action_payload) -> None:
    """Refuse caller-supplied execution results on the hosted path.

    The hosted path must never let a child inject ``order_results``/
    ``trade_results`` (the deterministic test seam) as if they were real fills.
    External callers are unaffected.
    """
    if hosted_job is None:
        return
    if getattr(action_payload, "order_results", None) or getattr(action_payload, "trade_results", None):
        raise HTTPException(
            status_code=403,
            detail={"rejection_reason": "HOSTED_EXECUTION_INJECTION_FORBIDDEN"},
        )


async def _hosted_read_guard(request: Request, token) -> None:
    """Bind the market/options **read** routes to a live hosted attempt.

    The option chain, Greek, expiry, selection, PCR and max-pain reads are
    token-only by external contract, so they do not otherwise carry a worker
    action. A hosted child reaches them only when **both** hold, and only while
    its persisted attempt is live:

    - the ``data`` capability lens the market reads use (``market:read``), and
    - a live persisted attempt (not stopped, fenced, expired or revoked).

    An external token is unaffected: no action is required of it and no attempt
    is looked up. This is a read check only - entering, exiting and protection
    mutations keep their existing ``intents:submit``/``risk:update`` gates, so
    this never unlocks direct options mutation.
    """
    hosted_job = await enforce_hosted_read_authority(request, token)
    if hosted_job is None:
        return
    if "market:read" not in set(getattr(token, "allowed_actions", None) or []):
        raise HTTPException(
            status_code=403,
            detail={
                "rejection_reason": "HOSTED_OPERATION_NOT_PERMITTED",
                "operation": "options.read",
                "required_action": "market:read",
            },
        )


@router.get("/underlyings/{underlying}/session")
async def get_worker_option_session(
    request: Request,
    underlying: str,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).get_session(underlying)


@router.get("/underlyings/{underlying}/expiries")
async def list_worker_option_expiries(
    request: Request,
    underlying: str,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).list_expiries(underlying)


@router.get("/underlyings/{underlying}/chain")
async def get_worker_option_chain(
    request: Request,
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).get_chain(underlying, expiry)


@router.get("/underlyings/{underlying}/mini-chain")
async def get_worker_option_mini_chain(
    request: Request,
    underlying: str,
    expiry: str | None = None,
    window: int = Query(default=5, ge=1, le=20),
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).get_mini_chain(underlying, expiry, window)


@router.get("/underlyings/{underlying}/greeks")
async def get_worker_option_greeks(
    request: Request,
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).get_greeks(underlying, expiry)


@router.post("/underlyings/{underlying}/selection/resolve")
async def resolve_worker_option_selection(
    request: Request,
    underlying: str,
    payload: dict,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).resolve_selection(underlying, payload)


@router.get("/underlyings/{underlying}/analytics/pcr")
async def get_worker_option_pcr(
    request: Request,
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).get_pcr(underlying, expiry)


@router.get("/underlyings/{underlying}/analytics/max-pain")
async def get_worker_option_max_pain(
    request: Request,
    underlying: str,
    expiry: str | None = None,
    _token=Depends(require_worker_token),
    manager=Depends(get_options_session_manager),
):
    await _hosted_read_guard(request, _token)
    return OptionsMarketService(manager).get_max_pain(underlying, expiry)


@router.post("/strategies/preview")
async def preview_worker_option_strategy(
    request: Request,
    payload: dict,
    _token=Depends(require_worker_token),
):
    await _hosted_read_guard(request, _token)
    return await preview_option_strategy(payload)


@router.post("/runs")
async def create_worker_option_run(
    payload: OptionRunCreateRequest,
    request: Request,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    hosted_job = await hosted_job_for_token(request, _token)
    if hosted_job is not None:
        # A hosted child may only create an options run pinned to the worker run
        # bound to its attempt, needs the trading action, and must be in paper.
        if "intents:submit" not in set(_token.allowed_actions or []):
            raise HTTPException(
                status_code=403,
                detail={
                    "rejection_reason": "HOSTED_OPERATION_NOT_PERMITTED",
                    "operation": "options.create_run",
                    "required_action": "intents:submit",
                },
            )
        requested = str(payload.strategy_run_id or "")
        run = await _repo(request).get_run(requested) if requested else None
        assert_hosted_run_binding(run, _token)
        await enforce_hosted_attempt_authority(request, _token, run)
        if str(run.get("execution_mode") or "").lower() != "paper":
            raise HTTPException(
                status_code=409,
                detail={
                    "rejection_reason": "HOSTED_OPTIONS_MUTATION_PAPER_ONLY",
                    "execution_mode": str(run.get("execution_mode") or ""),
                },
            )
        assert_hosted_discretionary_mutation_allowed(run, operation="options.create_run")
    return await create_option_run(payload, store)


@router.post("/runs/{strategy_run_id}/preview-entry")
async def preview_worker_option_run_entry(
    strategy_run_id: str,
    request: Request,
    payload: dict | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await preview_option_run_entry(strategy_run_id, request, payload, store)


@router.post("/runs/{strategy_run_id}/enter")
async def enter_worker_option_run(
    strategy_run_id: str,
    request: Request,
    payload: OptionRunActionRequest | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    action_payload = payload or OptionRunActionRequest()
    _, hosted_job = await _guard_options_mutation(
        request,
        _token,
        strategy_run_id,
        required_action="intents:submit",
        operation="options.enter",
        action_payload=action_payload,
    )
    _reject_hosted_execution_injection(hosted_job, action_payload)
    if action_payload.safety_token:
        await validate_worker_run_safety_token(request, strategy_run_id, action_payload.safety_token)
    return await enter_option_run(
        strategy_run_id,
        request,
        action_payload.model_dump(exclude_none=True),
        store,
        runtime,
    )


@router.post("/runs/{strategy_run_id}/preview-exit")
async def preview_worker_option_run_exit(
    strategy_run_id: str,
    request: Request,
    payload: dict | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    return await preview_option_run_exit(strategy_run_id, request, payload, store)


@router.post("/runs/{strategy_run_id}/exit")
async def exit_worker_option_run(
    strategy_run_id: str,
    request: Request,
    payload: OptionRunActionRequest | None = None,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    action_payload = payload or OptionRunActionRequest()
    _, hosted_job = await _guard_options_mutation(
        request,
        _token,
        strategy_run_id,
        required_action="intents:submit",
        operation="options.exit",
        action_payload=action_payload,
    )
    _reject_hosted_execution_injection(hosted_job, action_payload)
    if action_payload.safety_token:
        await validate_worker_run_safety_token(request, strategy_run_id, action_payload.safety_token)
    return await exit_option_run(
        strategy_run_id,
        action_payload.model_dump(exclude_none=True),
        store,
        runtime,
    )


@router.get("/runs/{strategy_run_id}/state")
async def get_worker_option_run_state(
    request: Request,
    strategy_run_id: str,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    await _hosted_read_guard(request, _token)
    return await get_option_run_state(strategy_run_id, store)


@router.put("/runs/{strategy_run_id}/protection")
async def update_worker_option_run_protection(
    strategy_run_id: str,
    request: Request,
    payload: OptionProtectionConfigUpdateRequest,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    await _guard_options_mutation(
        request, _token, strategy_run_id, required_action="risk:update", operation="options.protection"
    )
    return await update_option_run_protection(strategy_run_id, payload, store)


@router.get("/runs/{strategy_run_id}/protection/state")
async def get_worker_option_run_protection_state(
    strategy_run_id: str,
    request: Request,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    await _hosted_read_guard(request, _token)
    state = await get_option_run_protection_state(strategy_run_id, store)
    worker_run = await _repo(request).get_run(strategy_run_id)
    if worker_run is not None:
        _snapshot, _events = await observe_worker_option_protection_timeline_state(
            request,
            strategy_run_id,
            worker_run=worker_run,
        )
    return state


@router.post("/runs/{strategy_run_id}/protection/replay")
async def replay_worker_option_run_protection(
    strategy_run_id: str,
    request: Request,
    payload: OptionProtectionReplayRequest,
    _token=Depends(require_worker_token),
    store: OptionRunStore = Depends(get_option_run_store),
):
    hosted_job = await hosted_job_for_token(request, _token)
    if hosted_job is not None:
        # Replaying protection re-evaluates the rules and can submit an exit, so
        # for a hosted child it is a governed mutation like enter/exit/protection.
        run = await _repo(request).get_run(str(strategy_run_id))
        assert_hosted_run_binding(run, _token)
        await enforce_hosted_attempt_authority(request, _token, run)
        assert_hosted_discretionary_mutation_allowed(
            run, operation="options.protection_replay"
        )
    return await replay_option_run_protection(strategy_run_id, payload, store)
