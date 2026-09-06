"""Owned, server-side leases for scoped run mutations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any, AsyncIterator, Awaitable, Callable


LOGGER = logging.getLogger(__name__)


class SessionError(RuntimeError):
    pass


class SessionOutcomeUnknownError(SessionError):
    """The worker call returned but the lease was lost before confirmation."""


class RunLease:
    def __init__(self, manager: "RunSessionManager", run_id: str, nonce: str, heartbeat_interval: float) -> None:
        self.manager = manager
        self.run_id = run_id
        self._nonce = nonce
        self._heartbeat_interval = heartbeat_interval
        self._lost = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    async def start(self) -> None:
        if hasattr(self.manager.client, "run_heartbeat"):
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                try:
                    await self.manager.client.run_heartbeat(self.run_id, session_nonce=self._nonce, status="healthy")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # heartbeat loss is a hard mutation stop
                    self._lost.set()
                    LOGGER.warning("worker lease heartbeat lost for run %s: %s", self.run_id, type(exc).__name__)
                    return
        except asyncio.CancelledError:
            return

    def ensure_alive(self) -> None:
        if self.lost:
            raise SessionError("worker run lease heartbeat was lost; no further mutation is allowed")

    async def call(self, method: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        self.ensure_alive()
        kwargs.setdefault("session_nonce", self._nonce)
        result = await method(*args, **kwargs)
        if self.lost:
            raise SessionOutcomeUnknownError(
                "worker run lease heartbeat was lost after the mutation returned; outcome requires reconciliation"
            )
        return result

    async def close(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        try:
            await self.manager.client.release_session(self.run_id, session_nonce=self._nonce)
        except Exception as exc:
            # A successful mutation must not be turned into a retry instruction
            # solely because cleanup failed.  Record a redacted diagnostic.
            LOGGER.warning("worker lease release failed for run %s: %s", self.run_id, type(exc).__name__)


class RunSessionManager:
    def __init__(self, client: Any, *, heartbeat_interval: float = 10.0) -> None:
        self.client = client
        self.heartbeat_interval = max(0.5, min(float(heartbeat_interval), 10.0))
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, run_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(run_id, asyncio.Lock())

    @asynccontextmanager
    async def lease(self, run_id: str) -> AsyncIterator[RunLease]:
        normalized = str(run_id).strip()
        if not normalized:
            raise SessionError("strategy_run_id is required for a run mutation")
        for method_name in ("claim_session", "run_heartbeat", "release_session"):
            if not callable(getattr(self.client, method_name, None)):
                raise SessionError(f"worker client does not support {method_name}; mutation refused")
        lock = await self._lock_for(normalized)
        async with lock:
            claimed = await self.client.claim_session(normalized)
            nonce = None
            if isinstance(claimed, dict):
                # The public SDK returns the backend's exact field name.  The
                # shorter alias remains accepted for older/fake clients.
                nonce = claimed.get("worker_session_nonce") or claimed.get("session_nonce")
            if not nonce:
                raise SessionError("worker did not return a lease; mutation refused")
            lease = RunLease(self, normalized, str(nonce), self.heartbeat_interval)
            await lease.start()
            try:
                yield lease
            finally:
                await lease.close()


__all__ = ["SessionError", "SessionOutcomeUnknownError", "RunLease", "RunSessionManager"]
