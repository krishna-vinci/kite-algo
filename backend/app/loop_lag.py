from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Iterator, Optional

from backend.app.monitor import heartbeat
from backend.platform.owner_alerts import alert_owner_nowait


logger = logging.getLogger(__name__)


class ActivityTracker:
    """Thread-safe view of alert work currently consuming CPU."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._active: Dict[str, tuple[int, float]] = {}

    @contextmanager
    def track(self, name: str) -> Iterator[None]:
        started = self._clock()
        with self._lock:
            count, first_started = self._active.get(name, (0, started))
            self._active[name] = (count + 1, min(first_started, started))
        try:
            yield
        finally:
            with self._lock:
                count, first_started = self._active.get(name, (0, started))
                if count <= 1:
                    self._active.pop(name, None)
                else:
                    self._active[name] = (count - 1, first_started)

    def top_active(self) -> Optional[str]:
        with self._lock:
            if not self._active:
                return None
            return min(self._active.items(), key=lambda item: item[1][1])[0]


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(0, math.ceil((percentile_value / 100.0) * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


class LoopLagWatchdog:
    """Measure event-loop drift and react to sustained one-minute p99 lag."""

    def __init__(
        self,
        *,
        activity_tracker: Optional[ActivityTracker] = None,
        pause_screeners: Optional[Callable[[float], Any]] = None,
        threshold_ms: float = 250.0,
        required_minutes: int = 3,
        sample_interval_s: float = 1.0,
        window_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        alert: Callable[..., Any] = alert_owner_nowait,
    ) -> None:
        self.activity_tracker = activity_tracker
        self.pause_screeners = pause_screeners
        self.threshold_ms = float(threshold_ms)
        self.required_minutes = max(1, int(required_minutes))
        self.sample_interval_s = float(sample_interval_s)
        self.window_seconds = float(window_seconds)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._alert = alert
        self._samples: Deque[tuple[float, float]] = deque()
        self._minute: Optional[str] = None
        self._minute_samples: list[float] = []
        self._consecutive_high_minutes = 0
        self._last_alert_minute: Optional[str] = None

    def snapshot(self) -> Dict[str, float]:
        values = [lag for _at, lag in self._samples]
        return {
            "p50": round(percentile(values, 50), 3),
            "p99": round(percentile(values, 99), 3),
        }

    def record(self, lag_ms: float, *, minute: Optional[str] = None) -> bool:
        now_mono = self._monotonic()
        self._samples.append((now_mono, max(0.0, float(lag_ms))))
        cutoff = now_mono - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        minute_key = minute or self._wall_clock().strftime("%Y-%m-%dT%H:%MZ")
        fired = False
        if self._minute is None:
            self._minute = minute_key
        elif minute_key != self._minute:
            fired = self._finalize_minute(self._minute, self._minute_samples)
            self._minute = minute_key
            self._minute_samples = []
        self._minute_samples.append(max(0.0, float(lag_ms)))
        heartbeat("app", meta={"loop_lag_ms": self.snapshot()})
        return fired

    def _finalize_minute(self, minute: str, samples: list[float]) -> bool:
        minute_p99 = percentile(samples, 99)
        if minute_p99 > self.threshold_ms:
            self._consecutive_high_minutes += 1
        else:
            self._consecutive_high_minutes = 0
            return False
        if self._consecutive_high_minutes < self.required_minutes:
            return False
        top_task = self._top_task_name()
        logger.warning(
            "App event loop lagging: p99=%.1fms for %d consecutive minutes; top task=%s",
            minute_p99,
            self._consecutive_high_minutes,
            top_task,
        )
        if top_task in {"alerts-evaluation", "alerts-screener"} and self.pause_screeners:
            self.pause_screeners(600.0)
            logger.warning("Paused embedded alerts screener scheduler for 10 minutes")
        if self._last_alert_minute != minute:
            self._last_alert_minute = minute
            self._alert(
                key=f"loop-lag:{minute}",
                title="App event loop lagging",
                message=(
                    f"Event-loop p99 lag was {minute_p99:.1f} ms for "
                    f"{self._consecutive_high_minutes} consecutive minutes; "
                    f"top task: {top_task}."
                ),
            )
        return True

    def _top_task_name(self) -> str:
        if self.activity_tracker is not None:
            active = self.activity_tracker.top_active()
            if active:
                return active
        current = asyncio.current_task()
        candidates = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        if not candidates:
            return "unknown"
        return max(candidates, key=lambda task: len(task.get_stack())).get_name()

    async def run(self, stop: asyncio.Event) -> None:
        expected = self._monotonic() + self.sample_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.sample_interval_s)
                break
            except asyncio.TimeoutError:
                pass
            now = self._monotonic()
            self.record(max(0.0, now - expected) * 1000.0)
            expected = now + self.sample_interval_s
