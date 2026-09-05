import asyncio
import unittest
from datetime import date, datetime, timedelta

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from backend.broker_api.market.daily_candle_finalization import (
    IST,
    _load_kite_client,
    decide_finalization_window,
    finalize_daily_candles,
)


def _session(day: str, session_type: str = "REGULAR", close: str | None = "15:30:00"):
    return {
        "session_date": day,
        "session_type": session_type,
        "opens_at": None if session_type == "HOLIDAY" else "09:15:00",
        "closes_at": None if session_type == "HOLIDAY" else close,
        "verified": True,
    }


class FinalizationWindowTests(unittest.TestCase):
    def test_regular_session_runs_only_after_close_plus_delay(self):
        sessions = [_session("2026-09-04")]
        before = decide_finalization_window(
            sessions,
            now=datetime(2026, 9, 4, 15, 40, tzinfo=IST),
            require_current_session=True,
        )
        after = decide_finalization_window(
            sessions,
            now=datetime(2026, 9, 4, 15, 45, tzinfo=IST),
            require_current_session=True,
        )
        self.assertEqual(before.action, "wait")
        self.assertEqual(after.action, "run")

    def test_holiday_scheduler_skips(self):
        result = decide_finalization_window(
            [_session("2026-09-05", "HOLIDAY")],
            now=datetime(2026, 9, 5, 16, 0, tzinfo=IST),
            require_current_session=True,
        )
        self.assertEqual(result.action, "skip")
        self.assertEqual(result.reason, "NO_TRADING_SESSION_TODAY")

    def test_special_session_honors_its_actual_close(self):
        sessions = [_session("2026-11-08", "SPECIAL", "18:00:00")]
        result = decide_finalization_window(
            sessions,
            now=datetime(2026, 11, 8, 16, 0, tzinfo=IST),
            require_current_session=True,
        )
        self.assertEqual(result.action, "wait")
        self.assertEqual(result.final_at, datetime(2026, 11, 8, 18, 15, tzinfo=IST))

    def test_manual_recovery_uses_latest_final_session_on_weekend(self):
        result = decide_finalization_window(
            [
                _session("2026-09-04"),
                _session("2026-09-05", "HOLIDAY"),
            ],
            now=datetime(2026, 9, 5, 12, 0, tzinfo=IST),
            require_current_session=False,
        )
        self.assertEqual(result.action, "run")
        self.assertEqual(result.session_date, date(2026, 9, 4))


class _FakeIngestion:
    def __init__(self, outcomes):
        self.outcomes = {token: list(values) for token, values in outcomes.items()}
        self.calls = []

    async def ingest_historical_data(self, token, interval, **kwargs):
        self.calls.append((token, interval, kwargs))
        return self.outcomes[token].pop(0)


class DailyCandleFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_force_refreshes_tail_sessions_and_rate_limits_globally(self):
        ingestion = _FakeIngestion({
            101: [{"status": "success", "fetched": 3, "inserted": 1, "updated": 2}],
            202: [{"status": "success", "fetched": 3, "inserted": 1, "updated": 2}],
        })
        sleeps = []

        result = await finalize_daily_candles(
            [
                {"instrument_token": 101, "tradingsymbol": "AAA"},
                {"instrument_token": 202, "tradingsymbol": "BBB"},
            ],
            session_date=date(2026, 9, 4),
            tail_sessions=[date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)],
            ingestion=ingestion,
            sleep_fn=lambda delay: _record_sleep(sleeps, delay),
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(sleeps, [0.36])
        first_kwargs = ingestion.calls[0][2]
        self.assertTrue(first_kwargs["force_refresh"])
        self.assertEqual(first_kwargs["from_date"].astimezone(IST).date(), date(2026, 9, 2))
        self.assertEqual(first_kwargs["to_date"].astimezone(IST).date(), date(2026, 9, 5))

    async def test_retries_are_bounded_and_failure_is_reported(self):
        ingestion = _FakeIngestion({
            101: [
                {"status": "error", "error": "timeout-1"},
                {"status": "no_data", "message": "timeout-2"},
                {"status": "error", "error": "timeout-3"},
            ],
        })
        sleeps = []

        result = await finalize_daily_candles(
            [{"instrument_token": 101, "tradingsymbol": "AAA"}],
            session_date=date(2026, 9, 4),
            tail_sessions=[date(2026, 9, 4)],
            ingestion=ingestion,
            retry_delays=(2.0, 10.0),
            sleep_fn=lambda delay: _record_sleep(sleeps, delay),
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["failures"][0]["attempts"], 3)
        self.assertEqual(sleeps, [2.0, 10.0])


class _FakeSession:
    def close(self):
        return None


class _FakeKite:
    def __init__(self):
        self.profile_calls = 0

    def profile(self):
        self.profile_calls += 1
        raise RuntimeError("invalid token")


class AuthenticationValidationTests(unittest.TestCase):
    def test_invalid_system_token_fails_before_universe_ingestion(self):
        from unittest.mock import patch

        kite = _FakeKite()
        with (
            patch("backend.broker_api.market.daily_candle_finalization.SessionLocal", return_value=_FakeSession()),
            patch("backend.broker_api.market.daily_candle_finalization.get_system_access_token", return_value="expired"),
            patch("backend.broker_api.market.daily_candle_finalization.build_kite_client", return_value=kite),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid token"):
                _load_kite_client()
        self.assertEqual(kite.profile_calls, 1)


async def _record_sleep(target, delay):
    target.append(delay)
    await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
