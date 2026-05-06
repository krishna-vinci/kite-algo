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
