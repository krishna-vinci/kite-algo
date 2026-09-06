from __future__ import annotations

import asyncio

import pytest

from kite_algo_mcp.sessions import RunSessionManager, SessionError


class FakeWorker:
    def __init__(self, *, nonce: str = "server-only"):
        self.nonce = nonce
        self.claims: list[str] = []
        self.releases: list[tuple[str, str]] = []
        self.heartbeats: list[str] = []

    async def claim_session(self, run_id: str):
        self.claims.append(run_id)
        return {"session_nonce": self.nonce}

    async def run_heartbeat(self, run_id: str, *, session_nonce: str, status: str):
        assert session_nonce == self.nonce
        self.heartbeats.append(run_id)

    async def release_session(self, run_id: str, *, session_nonce: str):
        self.releases.append((run_id, session_nonce))


@pytest.mark.asyncio
async def test_same_run_is_serialized_and_nonce_never_escapes_lease() -> None:
    worker = FakeWorker()
    manager = RunSessionManager(worker, heartbeat_interval=0.5)
    active = 0
    maximum = 0

    async def work():
        nonlocal active, maximum
        async with manager.lease("run-1") as lease:
            assert not hasattr(lease, "session_nonce")
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(work(), work())
    assert maximum == 1
    assert worker.claims == ["run-1", "run-1"]
    assert all(nonce == "server-only" for _, nonce in worker.releases)


@pytest.mark.asyncio
async def test_different_runs_can_progress_independently_and_release_errors_are_cleanup_only() -> None:
    worker = FakeWorker()
    manager = RunSessionManager(worker, heartbeat_interval=0.5)
    entered: list[str] = []

    async def work(run_id: str):
        async with manager.lease(run_id):
            entered.append(run_id)
            await asyncio.sleep(0.01)

    await asyncio.gather(work("a"), work("b"))
    assert set(entered) == {"a", "b"}

    class ReleaseFails(FakeWorker):
        async def release_session(self, run_id: str, *, session_nonce: str):
            raise RuntimeError("cleanup")

    async with RunSessionManager(ReleaseFails(), heartbeat_interval=0.5).lease("ok"):
        pass


@pytest.mark.asyncio
async def test_missing_claim_nonce_refuses_mutation() -> None:
    class NoNonce(FakeWorker):
        async def claim_session(self, run_id: str):
            return {}

    with pytest.raises(SessionError, match="did not return a lease"):
        async with RunSessionManager(NoNonce()).lease("run"):
            pass
