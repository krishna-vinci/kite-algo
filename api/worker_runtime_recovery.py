from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_tz(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _runtime_recovery_patch(
    run: Dict[str, Any],
    *,
    recovery_status: str,
    action_required: bool = False,
    reason: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_state = dict(run.get("runtime_state") or {})
    payload = {
        "recovery_status": recovery_status,
        "action_required": bool(action_required),
        "checked_at": _utcnow().isoformat(),
    }
    if reason:
        payload["reason"] = reason
    if details:
        payload["details"] = details
    runtime_state["runtime_recovery"] = payload
    return runtime_state


async def load_live_run_flatness(request: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    repo = getattr(request.app.state, "algo_worker_repository", None)
    if repo is None:
        return {"is_flat": False, "remaining_legs": [], "broker_positions": [], "reason": "repository unavailable"}

    strategy_run_id = str(run.get("strategy_run_id") or "")
    account_id = str(run.get("account_scope") or "")
    if not strategy_run_id or not account_id:
        return {"is_flat": False, "remaining_legs": [], "broker_positions": [], "reason": "missing run identity"}

    remaining_legs = await repo.list_live_strategy_open_legs(strategy_run_id=strategy_run_id, account_id=account_id)
    broker_positions = await repo.list_live_strategy_broker_positions(strategy_run_id=strategy_run_id, account_id=account_id)

    refresh: Dict[str, Any] = {}
    if not remaining_legs and broker_positions:
        from api.routers.algo_workers import _load_live_kite_for_account, _live_broker_positions_for_attribution, _refresh_live_account_state, _worker_run_live_attribution_refs

        kite = await asyncio.to_thread(_load_live_kite_for_account, account_id)
        corr_id = f"worker-runtime-recovery-{strategy_run_id}"
        refs = await _worker_run_live_attribution_refs(request, run)
        fallback_positions = await _live_broker_positions_for_attribution(request, kite=kite, corr_id=corr_id, refs=refs)
        if fallback_positions:
            broker_positions = fallback_positions
        if broker_positions:
            refresh = await _refresh_live_account_state(kite=kite, account_id=account_id, corr_id=corr_id)

    is_flat = not remaining_legs and not broker_positions
    reason = "flat" if is_flat else "broker exposure remains"
    return {
        "is_flat": is_flat,
        "remaining_legs": remaining_legs,
        "broker_positions": broker_positions,
        "refresh": refresh,
        "reason": reason,
    }


async def evaluate_worker_run_settlement_status(
    *,
    run: Dict[str, Any],
    repo: Any,
    broker_flatness_loader: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
    active_basket_loader: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    strategy_run_id = str(run.get("strategy_run_id") or "")
    account_id = str(run.get("account_scope") or "")

    # Phase 1 DB guards are additive safety for modern worker runs.
    # If identity fields are missing (legacy run), skip guards and
    # fall through to broker flatness so existing recovery behavior
    # does not regress.
    have_identity = bool(strategy_run_id and account_id)

    if have_identity and (active_basket_loader and active_basket_loader(strategy_run_id)):
        return {
            "closable": False,
            "reason": "active_basket_execution",
            "details": {"active_basket_execution": True},
        }

    if have_identity:
        has_active_bracket_intent = getattr(repo, "has_active_bracket_intent", None)
        if has_active_bracket_intent and await has_active_bracket_intent(strategy_run_id=strategy_run_id):
            return {
                "closable": False,
                "reason": "active_bracket_intent",
                "details": {"active_bracket_intent": True},
            }

        has_pending_bracket_actions = getattr(repo, "has_pending_bracket_actions", None)
        if has_pending_bracket_actions and await has_pending_bracket_actions(strategy_run_id=strategy_run_id):
            return {
                "closable": False,
                "reason": "pending_bracket_action",
                "details": {"pending_bracket_action": True},
            }

    has_links = False
    if have_identity:
        has_worker_execution_links = getattr(repo, "has_worker_execution_links", None)
        has_unresolved_worker_execution = getattr(repo, "has_unresolved_worker_execution", None)
        has_links = (
            await has_worker_execution_links(strategy_run_id=strategy_run_id, account_id=account_id)
            if has_worker_execution_links
            else False
        )
        if has_links and has_unresolved_worker_execution and await has_unresolved_worker_execution(strategy_run_id=strategy_run_id, account_id=account_id):
            return {
                "closable": False,
                "reason": "unresolved_execution_links",
                "details": {"unresolved_execution_links": True, "exact_mode": True},
            }

    # Phase 2: broker/account flatness check once DB guards clear
    flatness = await broker_flatness_loader(run)
    is_flat = bool((flatness or {}).get("is_flat"))
    return {
        "closable": is_flat,
        "reason": "flat" if is_flat else str((flatness or {}).get("reason") or "live_exposure_not_flat"),
        "details": dict(flatness or {}),
        "exact_mode": has_links,
    }


class WorkerRuntimeRecoveryService:
    def __init__(
        self,
        *,
        repo: Any,
        now_fn: Callable[[], datetime] = _utcnow,
        stale_action_seconds: int,
        claimed_without_heartbeat_seconds: int,
        paper_exit_submitter: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
        live_flatness_loader: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
        active_basket_loader: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.repo = repo
        self.now_fn = now_fn
        self.stale_action_seconds = max(1, int(stale_action_seconds))
        self.claimed_without_heartbeat_seconds = max(1, int(claimed_without_heartbeat_seconds))
        self.paper_exit_submitter = paper_exit_submitter
        self.live_flatness_loader = live_flatness_loader
        self.active_basket_loader = active_basket_loader

    async def recover_stale_runs_once(self) -> Dict[str, int]:
        rows = await self.repo.list_stale_recovery_runs()
        result = {
            "scanned": len(rows),
            "stale_detected": 0,
            "closed": 0,
            "action_required": 0,
            "errored": 0,
        }
        now = _ensure_tz(self.now_fn()) or _utcnow()

        for run in rows:
            last_heartbeat = _ensure_tz(run.get("last_heartbeat_at"))
            claimed_at = _ensure_tz(run.get("worker_session_claimed_at"))

            stale = False
            if last_heartbeat is not None:
                stale = (now - last_heartbeat).total_seconds() >= self.stale_action_seconds
            elif claimed_at is not None:
                stale = (now - claimed_at).total_seconds() >= self.claimed_without_heartbeat_seconds

            if not stale:
                continue

            result["stale_detected"] += 1
            mode = str(run.get("execution_mode") or "paper").lower()
            strategy_run_id = str(run.get("strategy_run_id") or "")

            try:
                if mode == "dry_run":
                    await self.repo.update_run_status(
                        strategy_run_id,
                        "closed",
                        state_patch=_runtime_recovery_patch(run, recovery_status="closed", reason="dry_run_stale_auto_closed"),
                    )
                    result["closed"] += 1
                    continue

                if mode == "paper":
                    exit_result = await self.paper_exit_submitter(run)
                    status = str((exit_result or {}).get("status") or "").lower()
                    if status in {"success", "closed", "noop"}:
                        await self.repo.update_run_status(
                            strategy_run_id,
                            "closed",
                            state_patch=_runtime_recovery_patch(
                                run,
                                recovery_status="closed",
                                reason="paper_stale_exit_closed",
                                details={"paper_exit": exit_result},
                            ),
                        )
                        result["closed"] += 1
                    else:
                        await self.repo.update_run_runtime_state(
                            strategy_run_id,
                            _runtime_recovery_patch(
                                run,
                                recovery_status="stalled",
                                action_required=True,
                                reason="paper_stale_exit_blocked",
                                details={"paper_exit": exit_result},
                            ),
                        )
                        result["action_required"] += 1
                    continue

                if mode == "live":
                    runtime_state = dict(run.get("runtime_state") or {})
                    protection = dict(runtime_state.get("backend_protection") or {})
                    operations = dict(protection.get("operations") or {})
                    stale_owned = bool(protection.get("enabled") and operations.get("exit_on_worker_stale"))
                    if stale_owned:
                        await self.repo.update_run_runtime_state(
                            strategy_run_id,
                            _runtime_recovery_patch(run, recovery_status="classified", reason="live_stale_owned_by_protection_runtime"),
                        )
                        continue
                    await self.repo.update_run_runtime_state(
                        strategy_run_id,
                        _runtime_recovery_patch(
                            run,
                            recovery_status="action_required",
                            action_required=True,
                            reason="live_stale_unprotected",
                        ),
                    )
                    result["action_required"] += 1
                    continue
            except Exception as exc:
                result["errored"] += 1
                await self.repo.update_run_runtime_state(
                    strategy_run_id,
                    _runtime_recovery_patch(
                        run,
                        recovery_status="error",
                        action_required=True,
                        reason="recovery_exception",
                        details={"error": str(exc)},
                    ),
                )
        return result

    async def recover_exiting_runs_once(self) -> Dict[str, int]:
        rows = await self.repo.list_exiting_recovery_runs()
        result = {"scanned": len(rows), "closed": 0, "stalled": 0, "errored": 0}

        for run in rows:
            strategy_run_id = str(run.get("strategy_run_id") or "")
            mode = str(run.get("execution_mode") or "paper").lower()
            try:
                if mode in {"dry_run", "paper"}:
                    await self.repo.update_run_status(
                        strategy_run_id,
                        "closed",
                        state_patch=_runtime_recovery_patch(run, recovery_status="closed", reason="exiting_non_live_auto_closed"),
                    )
                    result["closed"] += 1
                    continue

                settlement = await evaluate_worker_run_settlement_status(
                    run=run,
                    repo=self.repo,
                    broker_flatness_loader=self.live_flatness_loader,
                    active_basket_loader=self.active_basket_loader,
                )
                if bool(settlement.get("closable")):
                    await self.repo.update_run_status(
                        strategy_run_id,
                        "closed",
                        state_patch=_runtime_recovery_patch(
                            run,
                            recovery_status="closed",
                            reason="exiting_live_flat_confirmed",
                            details=dict(settlement.get("details") or {}),
                        ),
                    )
                    result["closed"] += 1
                else:
                    await self.repo.update_run_status(
                        strategy_run_id,
                        "exiting",
                        state_patch=_runtime_recovery_patch(
                            run,
                            recovery_status="stalled",
                            action_required=True,
                            reason=str(settlement.get("reason") or "live_exposure_not_flat"),
                            details=dict(settlement.get("details") or {}),
                        ),
                    )
                    result["stalled"] += 1
            except Exception as exc:
                result["errored"] += 1
                await self.repo.update_run_runtime_state(
                    strategy_run_id,
                    _runtime_recovery_patch(
                        run,
                        recovery_status="error",
                        action_required=True,
                        reason="exiting_recovery_exception",
                        details={"error": str(exc)},
                    ),
                )

        return result


def build_worker_runtime_recovery_service(app: Any, *, stale_action_seconds: int, claimed_without_heartbeat_seconds: int) -> WorkerRuntimeRecoveryService:
    repo = getattr(app.state, "algo_worker_repository", None)
    if repo is None:
        from api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository

        repo = SqlAlchemyAlgoWorkerRepository()
        app.state.algo_worker_repository = repo

    request = SimpleNamespace(headers={}, app=app, is_disconnected=lambda: False)

    async def _paper_exit_submitter(run: Dict[str, Any]) -> Dict[str, Any]:
        paper_runtime = getattr(app.state, "paper_runtime_service", None)
        if paper_runtime is None:
            return {"status": "blocked", "reason": "paper runtime unavailable"}
        return await paper_runtime.exit_strategy(
            account_scope=str(run.get("account_scope") or ""),
            strategy_id=str(run.get("strategy_run_id") or ""),
        )

    async def _live_flatness_loader(run: Dict[str, Any]) -> Dict[str, Any]:
        return await load_live_run_flatness(request, run)

    from broker_api.basket_execution import basket_execution_store

    return WorkerRuntimeRecoveryService(
        repo=repo,
        now_fn=_utcnow,
        stale_action_seconds=stale_action_seconds,
        claimed_without_heartbeat_seconds=claimed_without_heartbeat_seconds,
        paper_exit_submitter=_paper_exit_submitter,
        live_flatness_loader=_live_flatness_loader,
        active_basket_loader=basket_execution_store.has_active_basket_execution,
    )
