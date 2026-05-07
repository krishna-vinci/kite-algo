from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.worker_runtime_recovery import WorkerRuntimeRecoveryService


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class FakeRecoveryRepo:
    def __init__(self, run: dict):
        self.run = dict(run)
        self.updated_statuses: list[tuple[str, str]] = []
        self.runtime_state_patches: list[dict] = []

    async def list_stale_recovery_runs(self):
        return [dict(self.run)] if str(self.run.get("status")) in {"open", "paused"} else []

    async def list_exiting_recovery_runs(self):
        return [dict(self.run)] if str(self.run.get("status")) == "exiting" else []

    async def update_run_status(self, strategy_run_id, status, *, state_patch=None):
        self.updated_statuses.append((strategy_run_id, status))
        self.run["status"] = status
        state = dict(self.run.get("runtime_state") or {})
        if state_patch:
            state.update(state_patch)
        self.run["runtime_state"] = state
        return dict(self.run)

    async def update_run_runtime_state(self, strategy_run_id, runtime_state):
        _ = strategy_run_id
        self.runtime_state_patches.append(dict(runtime_state))
        self.run["runtime_state"] = dict(runtime_state)
        return dict(self.run)

    async def has_worker_execution_links(self, *, strategy_run_id, account_id):
        _ = (strategy_run_id, account_id)
        return bool(self.run.get("has_worker_execution_links"))

    async def has_unresolved_worker_execution(self, *, strategy_run_id, account_id):
        _ = (strategy_run_id, account_id)
        return bool(self.run.get("has_unresolved_worker_execution"))

    async def has_active_bracket_intent(self, *, strategy_run_id):
        _ = strategy_run_id
        return bool(self.run.get("has_active_bracket_intent"))

    async def has_pending_bracket_actions(self, *, strategy_run_id):
        _ = strategy_run_id
        return bool(self.run.get("has_pending_bracket_actions"))


def test_stale_paper_run_without_backend_protection_is_exited():
    repo = FakeRecoveryRepo(
        run={
            "strategy_run_id": "run-paper",
            "execution_mode": "paper",
            "status": "open",
            "worker_session_nonce": "nonce-1",
            "worker_session_claimed_at": dt("2026-05-06T09:00:00Z"),
            "last_heartbeat_at": dt("2026-05-06T09:01:00Z"),
            "runtime_state": {},
        }
    )
    paper_exit = AsyncMock(return_value={"status": "closed"})
    service = WorkerRuntimeRecoveryService(
        repo=repo,
        now_fn=lambda: dt("2026-05-06T09:10:00Z"),
        stale_action_seconds=180,
        claimed_without_heartbeat_seconds=120,
        paper_exit_submitter=paper_exit,
        live_flatness_loader=AsyncMock(),
    )

    result = asyncio.run(service.recover_stale_runs_once())

    assert result["stale_detected"] == 1
    paper_exit.assert_called_once()
    assert repo.updated_statuses[-1] == ("run-paper", "closed")


def test_exiting_live_run_closes_when_flatness_is_proven():
    repo = FakeRecoveryRepo(
        run={
            "strategy_run_id": "run-live",
            "execution_mode": "live",
            "status": "exiting",
            "runtime_state": {"live_exit": {"idempotency_key": "exit-1"}},
        }
    )
    service = WorkerRuntimeRecoveryService(
        repo=repo,
        now_fn=lambda: dt("2026-05-06T09:10:00Z"),
        stale_action_seconds=180,
        claimed_without_heartbeat_seconds=120,
        paper_exit_submitter=AsyncMock(),
        live_flatness_loader=AsyncMock(return_value={"is_flat": True, "remaining_legs": []}),
    )

    result = asyncio.run(service.recover_exiting_runs_once())

    assert result["closed"] == 1
    assert repo.updated_statuses[-1][1] == "closed"


def test_exiting_live_run_remains_stalled_when_flatness_not_proven():
    repo = FakeRecoveryRepo(
        run={
            "strategy_run_id": "run-live",
            "execution_mode": "live",
            "status": "exiting",
            "runtime_state": {"live_exit": {"idempotency_key": "exit-1"}},
        }
    )
    service = WorkerRuntimeRecoveryService(
        repo=repo,
        now_fn=lambda: dt("2026-05-06T09:10:00Z"),
        stale_action_seconds=180,
        claimed_without_heartbeat_seconds=120,
        paper_exit_submitter=AsyncMock(),
        live_flatness_loader=AsyncMock(
            return_value={
                "is_flat": False,
                "remaining_legs": [{"tradingsymbol": "XYZ"}],
                "reason": "broker exposure remains",
            }
        ),
    )

    result = asyncio.run(service.recover_exiting_runs_once())

    assert result["stalled"] == 1
    assert repo.updated_statuses[-1][1] == "exiting"
    assert repo.run["runtime_state"]["runtime_recovery"]["recovery_status"] == "stalled"


def test_exiting_live_run_defers_when_active_basket_exists():
    repo = FakeRecoveryRepo(
        run={
            "strategy_run_id": "run-live",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "exiting",
            "runtime_state": {},
        }
    )
    service = WorkerRuntimeRecoveryService(
        repo=repo,
        now_fn=lambda: dt("2026-05-06T09:10:00Z"),
        stale_action_seconds=180,
        claimed_without_heartbeat_seconds=120,
        paper_exit_submitter=AsyncMock(),
        live_flatness_loader=AsyncMock(return_value={"is_flat": True}),
        active_basket_loader=lambda run_id: run_id == "run-live",
    )

    result = asyncio.run(service.recover_exiting_runs_once())

    assert result["stalled"] == 1
    assert repo.updated_statuses[-1] == ("run-live", "exiting")
    assert repo.run["runtime_state"]["runtime_recovery"]["reason"] == "active_basket_execution"


def test_settlement_status_defers_before_broker_refresh_when_active_bracket_exists():
    repo = FakeRecoveryRepo(
        run={
            "strategy_run_id": "run-live",
            "execution_mode": "live",
            "status": "exiting",
            "account_scope": "kite:AB1234",
            "runtime_state": {},
            "has_active_bracket_intent": True,
        }
    )
    loader = AsyncMock(return_value={"is_flat": True})
    service = WorkerRuntimeRecoveryService(
        repo=repo,
        now_fn=lambda: dt("2026-05-06T09:10:00Z"),
        stale_action_seconds=180,
        claimed_without_heartbeat_seconds=120,
        paper_exit_submitter=AsyncMock(),
        live_flatness_loader=loader,
    )

    result = asyncio.run(service.recover_exiting_runs_once())

    assert result["stalled"] == 1
    assert repo.run["runtime_state"]["runtime_recovery"]["reason"] == "active_bracket_intent"
    loader.assert_not_called()


def test_settlement_status_defers_when_unresolved_execution_links_exist():
    repo = FakeRecoveryRepo(
        run={
            "strategy_run_id": "run-live",
            "execution_mode": "live",
            "status": "exiting",
            "account_scope": "kite:AB1234",
            "runtime_state": {},
            "has_worker_execution_links": True,
            "has_unresolved_worker_execution": True,
        }
    )
    loader = AsyncMock(return_value={"is_flat": True})
    service = WorkerRuntimeRecoveryService(
        repo=repo,
        now_fn=lambda: dt("2026-05-06T09:10:00Z"),
        stale_action_seconds=180,
        claimed_without_heartbeat_seconds=120,
        paper_exit_submitter=AsyncMock(),
        live_flatness_loader=loader,
    )

    result = asyncio.run(service.recover_exiting_runs_once())

    assert result["stalled"] == 1
    assert repo.run["runtime_state"]["runtime_recovery"]["reason"] == "unresolved_execution_links"
    loader.assert_not_called()
