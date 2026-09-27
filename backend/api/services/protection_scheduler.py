"""Event-driven scheduling for backend protection evaluations.

Ticks and order updates select runs and trigger evaluation; the scheduler never
submits an order itself. The existing five-second protection loop remains the
watchdog for runs that receive no market or broker events.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Mapping
from typing import Any


logger = logging.getLogger(__name__)


class ProtectionScheduler:
    def __init__(
        self,
        runtime: Any,
        *,
        debounce_ms: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self.runtime = runtime
        self.debounce_ms = max(
            0.0,
            float(
                debounce_ms
                if debounce_ms is not None
                else os.getenv("PROTECTION_TICK_DEBOUNCE_MS", "250")
            ),
        )
        self.clock = clock
        self.last_breach_to_submit_ms: float | None = None

        self._token_runs: dict[int, set[str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._evaluating: set[str] = set()
        self._dirty: set[str] = set()
        self._received_at: dict[str, float] = {}
        self._refresh_generation = 0

    def refresh_tokens(self) -> None:
        """Rebuild token routing without blocking the caller's event loop."""

        self._refresh_generation += 1
        generation = self._refresh_generation
        try:
            token_result = self.runtime.run_tokens()
        except Exception:  # noqa: BLE001 - refresh failure cannot break watchdog
            logger.warning("protection token refresh failed", exc_info=True)
            return
        if not inspect.isawaitable(token_result):
            self._apply_token_map(token_result)
            return

        async def finish_refresh(
            pending: Awaitable[Mapping[str, set[int]]],
        ) -> None:
            try:
                tokens_by_run = await pending
                if generation == self._refresh_generation:
                    self._apply_token_map(tokens_by_run)
            except Exception:  # noqa: BLE001 - refresh failure is retried by watchdog
                logger.warning("protection token refresh failed", exc_info=True)

        task = asyncio.create_task(finish_refresh(token_result))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    def _apply_token_map(self, tokens_by_run: Any) -> None:
        routed: dict[int, set[str]] = {}
        if isinstance(tokens_by_run, Mapping):
            for key, tokens in tokens_by_run.items():
                run = str(key)
                if not run:
                    continue
                for token in tokens or set():
                    try:
                        routed.setdefault(int(token), set()).add(run)
                    except (TypeError, ValueError):
                        continue
        self._token_runs = routed

    def on_tick(self, token: int, tick: dict) -> None:
        """Schedule only runs that consume this tick."""

        del tick
        try:
            keys = self._token_runs.get(int(token), set())
        except (TypeError, ValueError):
            return
        received_at = self.clock()
        for key in keys:
            self.schedule(key, received_at=received_at)

    def on_order_update(self, update: dict) -> None:
        """Prompt every exit whose broker outcome may have changed."""

        del update
        try:
            keys = self.runtime.exit_in_flight_keys()
        except Exception:  # noqa: BLE001 - a listener never breaks order ingestion
            logger.warning("protection exit-in-flight lookup failed", exc_info=True)
            return
        for key in keys:
            self.schedule(str(key))

    def schedule(
        self,
        key: str,
        *,
        received_at: float | None = None,
    ) -> None:
        run = str(key)
        if not run:
            return
        self._received_at.setdefault(
            run, self.clock() if received_at is None else float(received_at)
        )
        task = self._tasks.get(run)
        if task is not None and not task.done():
            if run in self._evaluating:
                self._dirty.add(run)
            return
        self._tasks[run] = asyncio.create_task(self._run_after_debounce(run))

    async def _run_after_debounce(self, key: str) -> None:
        try:
            await asyncio.sleep(self.debounce_ms / 1000.0)
            while True:
                received_at = self._received_at.pop(key, self.clock())
                self._evaluating.add(key)
                try:
                    result = await self.runtime.evaluate_runs({key})
                except Exception:  # noqa: BLE001 - event loops must survive evaluation
                    logger.warning(
                        "scheduled protection evaluation failed run=%s",
                        key,
                        exc_info=True,
                    )
                    return
                finally:
                    self._evaluating.discard(key)

                if int((result or {}).get("triggered") or 0) >= 1:
                    latency_ms = max(0.0, (self.clock() - received_at) * 1000.0)
                    self.last_breach_to_submit_ms = latency_ms
                    logger.info(
                        "protection breach_to_submit_ms=%.0f run=%s",
                        latency_ms,
                        key,
                    )
                if key not in self._dirty:
                    return
                self._dirty.discard(key)
                # A tick received during evaluation gets exactly one immediate pass.
        finally:
            self._dirty.discard(key)
            current = asyncio.current_task()
            if self._tasks.get(key) is current:
                self._tasks.pop(key, None)

    async def drain(self) -> None:
        """Wait until all currently scheduled refreshes and evaluations finish."""

        while True:
            tasks = {
                *self._refresh_tasks,
                *(task for task in self._tasks.values() if not task.done()),
            }
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
