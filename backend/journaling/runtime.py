from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from .models import ProjectionState
from .service import JournalService


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JournalRuntimeWorker:
    def __init__(
        self,
        service: Optional[JournalService] = None,
        *,
        projector_name: str = "journal-runtime-worker",
        benchmark_ids: Iterable[str] = ("NIFTY50",),
        benchmark_refresh_interval: timedelta = timedelta(hours=6),
        run_refresh_interval: timedelta = timedelta(minutes=10),
        poll_interval_seconds: float = 60.0,
        run_refresh_limit: int = 50,
    ) -> None:
        self.service = service or JournalService()
        self.projector_name = projector_name
        self.benchmark_ids = tuple(benchmark_ids)
        self.benchmark_refresh_interval = benchmark_refresh_interval
        self.run_refresh_interval = run_refresh_interval
        self.poll_interval_seconds = max(1.0, float(poll_interval_seconds))
        self.run_refresh_limit = max(1, int(run_refresh_limit))
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    def load_state(self) -> Dict[str, Any]:
        existing = self.service.repository.get_projection_state(self.projector_name)
        return dict(existing.cursor) if existing else {}

    def save_state(self, cursor: Dict[str, Any]) -> None:
        self.service.repository.set_projection_state(
            ProjectionState(projector_name=self.projector_name, cursor=cursor, updated_at=_utcnow())
        )

    def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or _utcnow()
        cursor = self.load_state()
        result: Dict[str, Any] = {
            "benchmarks_refreshed": False,
            "runs_refreshed": False,
            "cursor_before": dict(cursor),
        }

        last_benchmark_refresh = self._parse_dt(cursor.get("last_benchmark_refresh_at"))
        if last_benchmark_refresh is None or now - last_benchmark_refresh >= self.benchmark_refresh_interval:
            result["benchmark_results"] = self.service.refresh_due_benchmarks(benchmark_ids=self.benchmark_ids)
            cursor["last_benchmark_refresh_at"] = now.isoformat()
            result["benchmarks_refreshed"] = True

        last_run_refresh = self._parse_dt(cursor.get("last_run_refresh_at"))
        if last_run_refresh is None or now - last_run_refresh >= self.run_refresh_interval:
            result["run_results"] = self.service.refresh_recent_run_metrics(limit=self.run_refresh_limit)
            cursor["last_run_refresh_at"] = now.isoformat()
            result["runs_refreshed"] = True

        cursor["last_tick_at"] = now.isoformat()
        self.save_state(cursor)
        result["cursor_after"] = dict(cursor)
        return result

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Journal runtime worker iteration failed: %s", exc, exc_info=True)
            await asyncio.sleep(self.poll_interval_seconds)

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
