"""Nightly fundamentals scheduler tests (injected clocks, no real sleeps)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

# Pre-existing import-order quirk: broker_api <-> broker_api.core resolve
# cyclically unless something inside the package initializes first. Importing
# this module before backend.app.schedulers avoids the partial-import error.
import backend.broker_api.core  # noqa: F401
from backend.app import schedulers


IST = timezone(timedelta(hours=5, minutes=30))


def _run_scheduler(*, now_sequence, sync_results=None, sync_error=None):
    """Drive the scheduler loop through one refresh window; returns captures."""
    sleeps = []
    sync_calls = []
    statuses = []
    clock = {"index": 0}

    def now_fn():
        value = now_sequence[min(clock["index"], len(now_sequence) - 1)]
        clock["index"] += 1
        return value

    async def sleep_fn(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError  # end the loop after the second window

    async def sync_fn(index_key):
        sync_calls.append(index_key)
        if sync_error is not None:
            raise sync_error
        return (sync_results or {}).get(index_key, {"symbols_requested": 2})

    async def main():
        task = asyncio.create_task(
            schedulers._schedule_fundamentals_nightly_refresh(
                now_fn=now_fn,
                sleep_fn=sleep_fn,
                sync_fn=sync_fn,
                heartbeat_enabled=False,
            )
        )
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(main())
    return sleeps, sync_calls


def test_scheduler_runs_nightly_at_0200_ist_over_every_supported_scope():
    start = datetime(2026, 9, 5, 1, 30, tzinfo=IST)  # 30 minutes before the window
    sleeps, sync_calls = _run_scheduler(
        now_sequence=[
            start,
            datetime(2026, 9, 5, 2, 0, tzinfo=IST),
            datetime(2026, 9, 5, 2, 0, tzinfo=IST),
        ],
    )
    assert sleeps[0] == 1800
    # The adapter registry is read dynamically at window time: currently Nifty50 + Nifty500.
    assert sync_calls == ["Nifty50", "Nifty500"]


def test_scheduler_after_window_sleeps_to_next_day():
    start = datetime(2026, 9, 5, 6, 0, tzinfo=IST)
    sleeps, sync_calls = _run_scheduler(
        now_sequence=[
            start,
            datetime(2026, 9, 6, 2, 0, tzinfo=IST),
            datetime(2026,9, 6, 2, 0, tzinfo=IST),
        ],
    )
    assert sleeps[0] == 20 * 3600  # 06:00 -> next day 02:00
    assert len(sync_calls) == 2  # one pass per index per window, no rapid retries


def test_scheduler_continues_remaining_indexes_after_one_fails():
    start = datetime(2026, 9, 5, 2, 0, tzinfo=IST)

    async def failing_first(index_key):
        if index_key == "Nifty50":
            raise RuntimeError("screener unreachable")
        return {"symbols_requested": 500}

    sleeps, sync_calls = _run_scheduler(
        now_sequence=[
            start,
            datetime(2026, 9, 6, 2, 0, tzinfo=IST),
            datetime(2026, 9, 6, 2, 0, tzinfo=IST),
        ],
        sync_error=RuntimeError("screener unreachable"),
    )
    # With Nifty50 failing, the scheduler still attempts Nifty500 within the
    # same window and the loop continues to the next daily window (no abort).
    assert sync_calls == ["Nifty50", "Nifty500"]
    assert len(sleeps) >= 2


def test_scheduler_window_remains_degraded_when_any_index_fails(monkeypatch):
    statuses = []
    monkeypatch.setattr(
        schedulers,
        "set_component_status",
        lambda component, status, **kwargs: statuses.append(
            (component, status, kwargs.get("detail"), kwargs.get("meta"))
        ),
    )

    _run_scheduler(
        now_sequence=[
            datetime(2026, 9, 5, 2, 0, tzinfo=IST),
            datetime(2026, 9, 6, 2, 0, tzinfo=IST),
            datetime(2026, 9, 6, 2, 0, tzinfo=IST),
        ],
        sync_error=RuntimeError("screener unreachable"),
    )

    completions = [item for item in statuses if "window completed" in str(item[2])]
    assert completions[-1][1] == "degraded"
    assert completions[-1][3] == {"failed_scopes": ["Nifty50", "Nifty500"]}


def test_scheduler_cancellation_stops_cleanly():
    from backend.app.monitor import get_components

    async def never(seconds):
        await asyncio.Event().wait()

    async def main():
        task = asyncio.create_task(
            schedulers._schedule_fundamentals_nightly_refresh(
                now_fn=lambda: datetime(2026, 9, 5, 1, 0, tzinfo=IST),
                sleep_fn=never,
                sync_fn=lambda index_key: None,
                heartbeat_enabled=False,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pytest.fail("scheduler should swallow cancellation and exit cleanly")

    asyncio.run(main())
    assert get_components()["fundamentals_scheduler"]["status"] == "stopped"
