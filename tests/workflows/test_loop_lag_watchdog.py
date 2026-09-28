from backend.app.loop_lag import ActivityTracker, LoopLagWatchdog


def test_loop_lag_watchdog_fires_after_three_high_minutes_and_pauses_screeners():
    monotonic = [0.0]
    alerts = []
    pauses = []
    tracker = ActivityTracker(clock=lambda: monotonic[0])
    watchdog = LoopLagWatchdog(
        activity_tracker=tracker,
        pause_screeners=pauses.append,
        monotonic=lambda: monotonic[0],
        alert=lambda **payload: alerts.append(payload),
    )

    with tracker.track("alerts-screener"):
        watchdog.record(300.0, minute="2026-09-28T10:00Z")
        monotonic[0] += 60.0
        watchdog.record(300.0, minute="2026-09-28T10:01Z")
        monotonic[0] += 60.0
        watchdog.record(300.0, minute="2026-09-28T10:02Z")
        monotonic[0] += 60.0
        assert watchdog.record(300.0, minute="2026-09-28T10:03Z") is True

    assert len(alerts) == 1
    assert alerts[0]["key"] == "loop-lag:2026-09-28T10:02Z"
    assert pauses == [600.0]
    assert watchdog.snapshot()["p99"] == 300.0
