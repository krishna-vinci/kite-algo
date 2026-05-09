from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from backend.api.services.protection import evaluate_backend_protection, validate_backend_protection_payload
from backend.broker_api.core.redis_events import publish_event


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat_age(last_heartbeat_at: Any, now: datetime) -> Optional[int]:
    if last_heartbeat_at is None:
        return None
    if isinstance(last_heartbeat_at, str):
        parsed = datetime.fromisoformat(last_heartbeat_at.replace("Z", "+00:00"))
    else:
        parsed = last_heartbeat_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))


class WorkerProtectionRuntime:
    def __init__(
        self,
        repo: Any,
        pnl_loader: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
        exit_submitter: Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]],
        now_fn: Callable[[], datetime] = _utcnow,
        squareoff_schedule: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.repo = repo
        self.pnl_loader = pnl_loader
        self.exit_submitter = exit_submitter
        self.now_fn = now_fn
        self.squareoff_schedule = squareoff_schedule or {}

    async def evaluate_once(self) -> Dict[str, int]:
        runs = await self.repo.list_protection_enabled_runs()
        evaluated = 0
        triggered = 0
        errors = 0
        for run in list(runs or []):
            evaluated += 1
            try:
                if await self._evaluate_run(dict(run)):
                    triggered += 1
            except Exception as exc:
                errors += 1
                await self._persist_run_error(run, exc)
        return {"evaluated": evaluated, "triggered": triggered, "errors": errors}

    async def _evaluate_run(self, run: Dict[str, Any]) -> bool:
        runtime_state = dict(run.get("runtime_state") or {})
        config = validate_backend_protection_payload(runtime_state.get("backend_protection"), live=str(run.get("execution_mode") or "").lower() == "live")
        state = dict(runtime_state.get("backend_protection_state") or {})
        now = self.now_fn()
        if self._has_recent_exit_claim(state, now):
            return False
        pnl = await self.pnl_loader(run)
        positions = list(pnl.get("legs") or pnl.get("positions") or [])
        next_state = evaluate_backend_protection(
            config,
            state=state,
            positions=positions,
            heartbeat_age_sec=_heartbeat_age(run.get("last_heartbeat_at"), now),
            now=now,
            squareoff_schedule=self.squareoff_schedule,
        )
        did_trigger = bool(next_state.get("status") == "triggered" and not state.get("exit_submitted"))
        if did_trigger:
            claim_id = str(uuid.uuid4())
            claimed_state = {
                **next_state,
                "exit_claim_id": claim_id,
                "exit_claimed_at": now.isoformat(),
                "exit_idempotency_key": self._idempotency_key(run, next_state),
            }
            claimed_result = await self._persist_state(
                run,
                runtime_state,
                state,
                claimed_state,
                expected_generation=state.get("generation"),
                expected_triggered_rule=state.get("triggered_rule") or "",
            )
            if claimed_result is None:
                return False
            await self._publish_timeline_rows(claimed_result.get("timeline_events") or [])
            try:
                exit_result = await self.exit_submitter(run, claimed_state)
            except Exception as exc:
                unknown_state = {
                    **claimed_state,
                    "status": "error",
                    "exit_submitted": True,
                    "exit_submission_status": "unknown",
                    "exit_error": str(exc),
                }
                persisted_unknown = await self._persist_state(
                    run,
                    runtime_state,
                    claimed_state,
                    unknown_state,
                    expected_generation=unknown_state.get("generation"),
                    expected_exit_claim_id=claim_id,
                )
                if persisted_unknown is None:
                    persisted_unknown = await self._persist_state(run, runtime_state, claimed_state, unknown_state)
                if persisted_unknown is not None:
                    await self._publish_timeline_rows(persisted_unknown.get("timeline_events") or [])
                return False
            if self._is_deferred_exit_result(exit_result):
                deferred_state = {
                    **claimed_state,
                    "exit_submitted": False,
                    "exit_submission_status": "deferred",
                    "exit_result": exit_result,
                }
                persisted_deferred = await self._persist_state(
                    run,
                    runtime_state,
                    claimed_state,
                    deferred_state,
                    expected_generation=deferred_state.get("generation"),
                    expected_exit_claim_id=claim_id,
                )
                if persisted_deferred is None:
                    persisted_deferred = await self._persist_state(run, runtime_state, claimed_state, deferred_state)
                if persisted_deferred is not None:
                    await self._publish_timeline_rows(persisted_deferred.get("timeline_events") or [])
                return False
            next_state = {
                **claimed_state,
                "exit_submitted": True,
                "exit_result": exit_result,
            }
            persisted_terminal = await self._persist_state(
                run,
                runtime_state,
                claimed_state,
                next_state,
                expected_generation=next_state.get("generation"),
                expected_exit_claim_id=claim_id,
            )
            if persisted_terminal is None:
                persisted_terminal = await self._persist_state(run, runtime_state, claimed_state, next_state)
            if persisted_terminal is not None:
                await self._publish_timeline_rows(persisted_terminal.get("timeline_events") or [])
            return True
        persisted_non_trigger = await self._persist_state(
            run,
            runtime_state,
            state,
            next_state,
            expected_generation=state.get("generation"),
            expected_triggered_rule=state.get("triggered_rule") or "",
            expected_exit_claim_id=state.get("exit_claim_id") or "",
        )
        if persisted_non_trigger is not None:
            await self._publish_timeline_rows(persisted_non_trigger.get("timeline_events") or [])
        return did_trigger

    def _has_recent_exit_claim(self, state: Dict[str, Any], now: datetime) -> bool:
        if state.get("exit_submitted") or not state.get("exit_claim_id"):
            return False
        claimed_at = state.get("exit_claimed_at")
        if not claimed_at:
            return True
        try:
            parsed = datetime.fromisoformat(str(claimed_at).replace("Z", "+00:00"))
        except Exception:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() < 60

    @staticmethod
    def _is_deferred_exit_result(result: Any) -> bool:
        return bool(isinstance(result, dict) and (result.get("deferred") or str(result.get("status") or "").lower() == "deferred"))

    async def _persist_state(
        self,
        run: Dict[str, Any],
        runtime_state: Dict[str, Any],
        previous_protection_state: Dict[str, Any],
        protection_state: Dict[str, Any],
        *,
        expected_generation: Any = None,
        expected_triggered_rule: Optional[str] = None,
        expected_exit_claim_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        strategy_run_id = str(run["strategy_run_id"])
        expected = int(expected_generation) if expected_generation is not None else None
        timeline_events = self._build_timeline_events(
            run,
            previous_state=previous_protection_state,
            next_state=protection_state,
        )
        if hasattr(self.repo, "update_run_backend_protection_state_with_events"):
            return await self.repo.update_run_backend_protection_state_with_events(
                strategy_run_id,
                protection_state,
                expected_generation=expected,
                expected_triggered_rule=expected_triggered_rule,
                expected_exit_claim_id=expected_exit_claim_id,
                timeline_events=timeline_events,
            )
        if hasattr(self.repo, "update_run_backend_protection_state"):
            updated = await self.repo.update_run_backend_protection_state(
                strategy_run_id,
                protection_state,
                expected_generation=expected,
                expected_triggered_rule=expected_triggered_rule,
                expected_exit_claim_id=expected_exit_claim_id,
            )
            if updated is None:
                return None
            return {"run": updated, "timeline_events": []}
        runtime_state["backend_protection_state"] = protection_state
        updated = await self.repo.update_run_runtime_state(strategy_run_id, runtime_state)
        if updated is None:
            return None
        return {"run": updated, "timeline_events": []}

    async def _persist_run_error(self, run: Dict[str, Any], exc: Exception) -> None:
        try:
            runtime_state = dict(run.get("runtime_state") or {})
            previous = dict(runtime_state.get("backend_protection_state") or {})
            if previous.get("exit_submitted"):
                return
            runtime_state["backend_protection_state"] = {
                **previous,
                "status": "error",
                "last_checked_at": self.now_fn().isoformat(),
                "error": str(exc),
            }
            persisted = await self._persist_state(
                run,
                runtime_state,
                previous,
                runtime_state["backend_protection_state"],
                expected_generation=previous.get("generation"),
                expected_triggered_rule=previous.get("triggered_rule") or "",
                expected_exit_claim_id=previous.get("exit_claim_id") or "",
            )
            if persisted is not None:
                await self._publish_timeline_rows(persisted.get("timeline_events") or [])
        except Exception:
            return

    def _build_timeline_events(
        self,
        run: Dict[str, Any],
        *,
        previous_state: Dict[str, Any],
        next_state: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        events: list[Dict[str, Any]] = []
        strategy_run_id = str(run.get("strategy_run_id") or "")
        previous_status = str(previous_state.get("status") or "")
        next_status = str(next_state.get("status") or "")
        previous_exit_submitted = bool(previous_state.get("exit_submitted"))
        next_exit_submitted = bool(next_state.get("exit_submitted"))

        if next_status == "triggered" and previous_status != "triggered":
            events.append(
                {
                    "event_kind": "protection",
                    "event_source": "backend_protection",
                    "event_type": "protection.triggered",
                    "related_resource_type": "strategy_run",
                    "related_resource_id": strategy_run_id,
                    "summary": f"Backend protection triggered: {next_state.get('triggered_rule') or 'unknown_rule'}",
                    "payload": {
                        "emission_mode": "mutation_driven",
                        "status": next_status,
                        "triggered_rule": next_state.get("triggered_rule"),
                        "action": next_state.get("action"),
                        "generation": next_state.get("generation"),
                    },
                }
            )

        if next_exit_submitted and not previous_exit_submitted:
            events.append(
                {
                    "event_kind": "protection",
                    "event_source": "backend_protection",
                    "event_type": "protection.exit_submitted",
                    "related_resource_type": "strategy_run",
                    "related_resource_id": strategy_run_id,
                    "summary": f"Backend protection exit submitted: {next_state.get('triggered_rule') or 'unknown_rule'}",
                    "payload": {
                        "emission_mode": "mutation_driven",
                        "status": next_status,
                        "triggered_rule": next_state.get("triggered_rule"),
                        "exit_submission_status": next_state.get("exit_submission_status"),
                        "exit_result": next_state.get("exit_result"),
                        "generation": next_state.get("generation"),
                    },
                }
            )

        if previous_status == "error" and next_status == "active":
            events.append(
                {
                    "event_kind": "protection",
                    "event_source": "backend_protection",
                    "event_type": "protection.blocking_changed",
                    "related_resource_type": "strategy_run",
                    "related_resource_id": strategy_run_id,
                    "summary": "Backend protection recovered from error to active",
                    "payload": {
                        "emission_mode": "mutation_driven",
                        "previous_status": previous_status,
                        "status": next_status,
                        "generation": next_state.get("generation"),
                    },
                }
            )
        return events

    async def _publish_timeline_rows(self, rows: list[Dict[str, Any]]) -> None:
        for row in list(rows or []):
            strategy_run_id = str(row.get("strategy_run_id") or "").strip()
            if not strategy_run_id:
                continue
            await publish_event(f"worker.execution.events:{strategy_run_id}", dict(row))

    def _idempotency_key(self, run: Dict[str, Any], state: Dict[str, Any]) -> str:
        generation = state.get("generation") or 1
        rule = str(state.get("triggered_rule") or "unknown")
        digest = hashlib.sha1(json.dumps(state.get("details") or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:10]
        return f"backend-protection:{run['strategy_run_id']}:g{generation}:{rule}:{digest}"


async def load_worker_run_pnl_for_protection(request: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    from backend.api.routers.worker_protection import _build_worker_run_pnl_snapshot

    return await _build_worker_run_pnl_snapshot(request, run)


async def submit_worker_protection_exit(request: Any, run: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    from backend.api.services.control_plane import exit_control_strategy

    return await exit_control_strategy(
        request,
        str(run["strategy_run_id"]),
        account_scope=str(run.get("account_scope") or "default"),
        reason=f"backend_protection:{state.get('triggered_rule') or 'unknown'}",
        dry_run=False,
        idempotency_key=str(state.get("exit_idempotency_key") or "") or None,
    )
