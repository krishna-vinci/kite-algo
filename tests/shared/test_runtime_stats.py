import asyncio
import logging

from backend.shared.runtime_stats import (
    LagRecorder,
    run_stats_sampler,
    sample_process,
)


def test_lag_recorder_percentiles_and_bounds():
    recorder = LagRecorder()
    assert recorder.snapshot() == {
        "count": 0,
        "p50_s": None,
        "p99_s": None,
        "max_s": None,
    }
    for value in (0.001, 0.002, 0.003, 0.5, 2.0):
        recorder.record(value)
    snapshot = recorder.snapshot()
    assert snapshot["count"] == 5
    assert snapshot["p50_s"] == 0.003
    assert snapshot["p99_s"] == 2.0
    assert snapshot["max_s"] == 2.0
    # Clock glitches and frozen-snapshot artifacts must not poison the window.
    recorder.record(-1.0)
    recorder.record(9999.0)
    assert recorder.snapshot()["count"] == 5


def test_sample_process_shape():
    stats = sample_process()
    assert stats["cpu_s"] >= 0.0
    assert stats["rss_mib"] > 0.0


def test_run_stats_sampler_logs_and_stops(caplog):
    """Drives the coroutine directly: no pytest-asyncio needed in the runtime image."""

    async def scenario():
        calls = {"extras": 0}

        def extras():
            calls["extras"] += 1
            return {"tick_lag": {"count": 1, "p50_s": 0.1}}

        sampler_logger = logging.getLogger("backend.shared.test_sampler")
        task = asyncio.create_task(
            run_stats_sampler(
                sampler_logger,
                interval_s=0.01,
                extras=extras,
                component="test",
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return calls

    calls = asyncio.run(scenario())
    assert calls["extras"] >= 1


def test_run_stats_sampler_stops_on_event():
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_stats_sampler(
                logging.getLogger("backend.shared.test_sampler"),
                interval_s=0.01,
                stop=stop,
                component="test",
            )
        )
        await asyncio.sleep(0.03)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)  # clean return, no cancel needed

    asyncio.run(scenario())
