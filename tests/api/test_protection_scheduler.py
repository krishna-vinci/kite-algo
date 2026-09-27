import asyncio
import unittest

from backend.api.services.protection_scheduler import ProtectionScheduler


class _Runtime:
    def __init__(self):
        self.calls = []
        self.result = {"evaluated": 1, "triggered": 0, "errors": 0}

    async def evaluate_runs(self, keys):
        self.calls.append(set(keys))
        return dict(self.result)

    def run_tokens(self):
        return {"run-a": {1, 2}, "run-b": {3}}

    def exit_in_flight_keys(self):
        return {"run-b"}


class ProtectionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticks_inside_the_debounce_window_are_coalesced(self):
        runtime = _Runtime()
        scheduler = ProtectionScheduler(runtime, debounce_ms=100)
        scheduler.refresh_tokens()

        scheduler.on_tick(1, {})
        await asyncio.sleep(0.02)
        scheduler.on_tick(1, {})
        scheduler.on_tick(1, {})
        await scheduler.drain()

        self.assertEqual(runtime.calls, [{"run-a"}])

    async def test_a_tick_targets_only_mapped_runs_and_ignores_unknown_tokens(self):
        runtime = _Runtime()
        scheduler = ProtectionScheduler(runtime, debounce_ms=0)
        scheduler.refresh_tokens()

        scheduler.on_tick(3, {})
        scheduler.on_tick(99, {})
        await scheduler.drain()

        self.assertEqual(runtime.calls, [{"run-b"}])

    async def test_order_updates_schedule_exit_in_flight_runs(self):
        runtime = _Runtime()
        scheduler = ProtectionScheduler(runtime, debounce_ms=0)

        scheduler.on_order_update({"order_id": "order-1"})
        await scheduler.drain()

        self.assertEqual(runtime.calls, [{"run-b"}])

    async def test_a_tick_during_evaluation_schedules_exactly_one_more_pass(self):
        runtime = _Runtime()
        started = asyncio.Event()
        release = asyncio.Event()

        async def evaluate(keys):
            runtime.calls.append(set(keys))
            if len(runtime.calls) == 1:
                started.set()
                await release.wait()
            return dict(runtime.result)

        runtime.evaluate_runs = evaluate
        scheduler = ProtectionScheduler(runtime, debounce_ms=0)
        scheduler.refresh_tokens()

        scheduler.on_tick(1, {})
        await started.wait()
        scheduler.on_tick(1, {})
        scheduler.on_tick(1, {})
        release.set()
        await scheduler.drain()

        self.assertEqual(runtime.calls, [{"run-a"}, {"run-a"}])

    async def test_triggered_evaluation_records_breach_to_submit_latency(self):
        runtime = _Runtime()
        runtime.result["triggered"] = 1
        scheduler = ProtectionScheduler(runtime, debounce_ms=0)
        scheduler.refresh_tokens()

        scheduler.on_tick(1, {})
        await scheduler.drain()

        self.assertIsNotNone(scheduler.last_breach_to_submit_ms)
        self.assertGreaterEqual(scheduler.last_breach_to_submit_ms, 0)

    async def test_evaluation_exceptions_do_not_escape_drain(self):
        runtime = _Runtime()

        async def fail(_keys):
            raise RuntimeError("evaluation bug")

        runtime.evaluate_runs = fail
        scheduler = ProtectionScheduler(runtime, debounce_ms=0)
        scheduler.refresh_tokens()

        scheduler.on_tick(1, {})
        await scheduler.drain()

        self.assertEqual(scheduler.last_breach_to_submit_ms, None)


if __name__ == "__main__":
    unittest.main()
