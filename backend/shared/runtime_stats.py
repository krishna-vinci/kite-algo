"""Lightweight process + consumer-lag instrumentation.

Two dependency-free pieces, per the go-refactor contract (agent-context/
go-refactor-proposal.md, "Instrumentation gate"):

``LagRecorder``       sliding-window summary of consumer lag in seconds
                      (Go runtime ``received_at`` stamp -> local processing).
``sample_process``    one CPU/RSS reading of the current interpreter.
``run_stats_sampler`` logs both into a single ``runtime_stats`` line every
                      interval; finance-app and the alerts worker each run one.

The sampler never raises: a metrics logger must not be able to take its
process down.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional


class LagRecorder:
    """Sliding-window lag summary in seconds.

    Samples outside ``[0, 3600)`` are dropped so clock glitches and frozen
    snapshot re-publishes cannot poison the percentiles. Confined to the event
    loop by convention; no locking.
    """

    def __init__(self, window: int = 6000) -> None:
        self._samples: Deque[float] = deque(maxlen=window)

    def record(self, lag_s: float) -> None:
        if 0.0 <= lag_s < 3600.0:
            self._samples.append(lag_s)

    def snapshot(self) -> Dict[str, object]:
        samples: List[float] = list(self._samples)
        if not samples:
            return {"count": 0, "p50_s": None, "p99_s": None, "max_s": None}
        samples.sort()

        def pct(fraction: float) -> float:
            index = min(len(samples) - 1, int(round(fraction * (len(samples) - 1))))
            return round(samples[index], 4)

        return {
            "count": len(samples),
            "p50_s": pct(0.50),
            "p99_s": pct(0.99),
            "max_s": round(samples[-1], 4),
        }


def sample_process() -> Dict[str, float]:
    """Cumulative CPU seconds and RSS MiB of the current process."""
    page_bytes = os.sysconf("SC_PAGE_SIZE")
    with open("/proc/self/statm") as handle:
        rss_mib = int(handle.readline().split()[1]) * page_bytes / (1024 * 1024)
    return {
        "cpu_s": round(time.process_time(), 3),
        "rss_mib": round(rss_mib, 1),
    }


async def run_stats_sampler(
    logger: logging.Logger,
    interval_s: float = 60.0,
    stop: Optional[asyncio.Event] = None,
    extras: Optional[Callable[[], Optional[Dict[str, object]]]] = None,
    component: str = "python",
) -> None:
    """Log one ``runtime_stats`` line every ``interval_s`` until ``stop`` is set.

    ``extras`` is called just before each log line so callers can fold in live
    counters (e.g. tick-lag snapshots); its failures are logged, never raised.
    """
    owned_stop = stop is None
    if stop is None:
        stop = asyncio.Event()
    previous_cpu = time.process_time()
    previous_at = asyncio.get_running_loop().time()
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            if owned_stop:
                raise
            return
        try:
            now_cpu = time.process_time()
            now_at = asyncio.get_running_loop().time()
            cpu_pct = round(
                (now_cpu - previous_cpu) / max(now_at - previous_at, 1e-9) * 100.0, 2
            )
            previous_cpu, previous_at = now_cpu, now_at
            payload: Dict[str, object] = {
                "component": component,
                **sample_process(),
                "cpu_pct_window": cpu_pct,
            }
            if extras is not None:
                try:
                    extra = extras()
                    if extra:
                        payload.update(extra)
                except Exception:
                    logger.debug("runtime_stats extras failed", exc_info=True)
            logger.info("runtime_stats %s", payload)
        except Exception:
            logger.debug("runtime_stats sample failed", exc_info=True)
