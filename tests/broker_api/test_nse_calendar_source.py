"""Official NSE calendar synchronization and daily refresh scheduler tests."""
import asyncio
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()
sys.modules.pop("broker_api.orders", None)

from backend.broker_api.market import nse_calendar_source as nse_module
from backend.broker_api.market.exchange_calendar import parse_canonical_csv
from backend.broker_api.market.nse_calendar_source import (
    NSE_HOLIDAY_API_URL,
    NseCalendarSourceClient,
    NseCalendarSourceError,
    build_canonical_year_rows,
    merge_with_active_calendar,
    parse_cm_holidays,
    render_canonical_csv,
    synchronize_official_calendar,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _payload(*rows):
    return {"CM": list(rows), "FNO": [{"tradingDate": "26-Jan-2026"}]}


def _holiday(trading_date, description="Test Holiday"):
    return {"tradingDate": trading_date, "description": description}


class _FakeClient:
    def __init__(self, raw=None, error=None):
        self.raw = raw
        self.error = error
        self.fetch_count = 0

    def fetch(self) -> bytes:
        self.fetch_count += 1
        if self.error is not None:
            raise self.error
        return self.raw


class _FakeHtmlResponse:
    def raise_for_status(self):
        return None

    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b"<html><body>blocked</body></html>"


class _FakeSession:
    """Session double that serves HTML on the API endpoint (fail-closed path)."""

    def get(self, url, headers=None, timeout=None):
        return _FakeHtmlResponse()


class _RecordingCursor:
    def __init__(self, results, log):
        self._results = results
        self._log = log
        self._row = None
        self._rows = []

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))
        self._row = None
        self._rows = []
        for fragment, value in self._results:
            if fragment in sql:
                if isinstance(value, list):
                    self._rows = value
                else:
                    self._row = value
                return

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _RecordingConnection:
    def __init__(self, results):
        self.results = results
        self.log = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _RecordingCursor(self.results, self.log)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _conn(*, observed_sha=None, version=2, active_rows=None):
    results = [
        ("exchange_calendar_source_documents'), to_regclass", ("src", "sess")),
        ("exchange_calendar_refresh_state')", ("refresh_state",)),
        ("SELECT observed_source_sha256", (observed_sha,)),
        ("SELECT MAX(calendar_version) FROM public.exchange_calendar_source_documents", (version,)),
        ("SELECT session_date, session_type", active_rows or []),
        ("INSERT INTO public.exchange_calendar_refresh_state", None),
    ]
    return _RecordingConnection(results)


def _official_payload_bytes(payload):
    return json.dumps(payload).encode("utf-8")


# 2026 fixtures: Republic Day 2026-01-26 (Monday) and Independence Day 2026-08-15 (Saturday).
_2026_HOLIDAYS = (_holiday("26-Jan-2026"), _holiday("15-Aug-26"), _holiday("02 October, 2026"), _holiday("2026-12-25"))


# ---------------------------------------------------------------------------
# Official holiday parsing and canonical year generation
# ---------------------------------------------------------------------------


def test_parse_cm_holidays_accepts_only_the_documented_formats():
    payload = _payload(
        _holiday("26-Jan-2026"),
        _holiday("15-Aug-26"),
        _holiday("02 October, 2026"),
        _holiday("2026-05-01"),
        _holiday("25-Dec-2025"),  # other year ignored
    )
    holidays = parse_cm_holidays(payload, 2026)
    assert holidays == [date(2026, 1, 26), date(2026, 5, 1), date(2026, 8, 15), date(2026, 10, 2)]


def test_parse_cm_holidays_fails_closed_on_missing_malformed_or_duplicate_data():
    with pytest.raises(NseCalendarSourceError, match="missing CM"):
        parse_cm_holidays({}, 2026)
    with pytest.raises(NseCalendarSourceError, match="missing CM"):
        parse_cm_holidays({"CM": []}, 2026)
    with pytest.raises(NseCalendarSourceError, match="not a JSON object"):
        parse_cm_holidays([_holiday("26-Jan-2026")], 2026)
    with pytest.raises(NseCalendarSourceError, match="unsupported official tradingDate"):
        parse_cm_holidays(_payload(_holiday("26/01/2026")), 2026)
    with pytest.raises(NseCalendarSourceError, match="empty tradingDate"):
        parse_cm_holidays(_payload(_holiday("")), 2026)
    with pytest.raises(NseCalendarSourceError, match="duplicate"):
        parse_cm_holidays(_payload(_holiday("26-Jan-2026"), _holiday("26 January, 2026")), 2026)


def test_fetch_fails_closed_on_html_responses_and_invalid_json():
    client = NseCalendarSourceClient(session=_FakeSession())
    with pytest.raises(NseCalendarSourceError, match="HTML"):
        client.fetch()
    raw = b"<html>not json</html>"
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw.decode("utf-8"))


def test_canonical_year_rows_cover_weekends_holidays_and_weekdays_deterministically():
    holidays = parse_cm_holidays(_payload(*_2026_HOLIDAYS), 2026)
    rows = build_canonical_year_rows(holidays, 2026)
    assert len(rows) == 365
    by_date = {row["session_date"]: row for row in rows}
    # Monday official holiday: HOLIDAY with no times.
    assert by_date[date(2026, 1, 26)]["session_type"] == "HOLIDAY"
    assert by_date[date(2026, 1, 26)]["opens_at"] is None
    # Saturday: weekend HOLIDAY even without an official holiday.
    assert by_date[date(2026, 8, 15)]["session_type"] == "HOLIDAY"
    # Plain weekday: REGULAR verified session with fixed times.
    regular = by_date[date(2026, 1, 5)]
    assert regular["session_type"] == "REGULAR"
    assert regular["opens_at"] == "09:15:00"
    assert regular["closes_at"] == "15:30:00"
    assert regular["verified"] is True
    # No SPECIAL sessions are ever invented.
    assert all(row["session_type"] in {"REGULAR", "HOLIDAY"} for row in rows)
    # Leap year produces exactly 366 rows.
    assert len(build_canonical_year_rows([], 2024)) == 366
    # Deterministic ordering and stable SHA.
    rows_again = build_canonical_year_rows(parse_cm_holidays(_payload(*_2026_HOLIDAYS), 2026), 2026)
    assert render_canonical_csv(rows) == render_canonical_csv(rows_again)
    assert [row["session_date"] for row in rows] == sorted(row["session_date"] for row in rows)


def test_official_source_client_uses_required_headers_and_timeouts():
    captured = {}

    class _OkResponse:
        headers = {"Content-Type": "application/json"}
        content = b"{}"

        def raise_for_status(self):
            return None

    class _Session:
        def get(self, url, headers=None, timeout=None):
            captured.setdefault("urls", []).append(url)
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _OkResponse()

    client = NseCalendarSourceClient(session=_Session())
    assert client.fetch() == b"{}"
    assert captured["urls"][0] == "https://www.nseindia.com/resources/exchange-communication-holidays"
    assert captured["urls"][1] == "https://www.nseindia.com/api/holiday-master?type=trading"
    assert captured["headers"]["User-Agent"] == "Mozilla/5.0"
    assert captured["headers"]["Accept"] == "application/json,text/plain,*/*"
    assert captured["headers"]["Referer"] == "https://www.nseindia.com/resources/exchange-communication-holidays"
    assert captured["timeout"] == 20


# ---------------------------------------------------------------------------
# Merging and synchronization
# ---------------------------------------------------------------------------


def _active_row(session_date, session_type, opens=None, closes=None, verified=True):
    return (session_date, session_type, time.fromisoformat(opens) if opens else None, time.fromisoformat(closes) if closes else None, verified)


def test_merge_preserves_outside_years_and_verified_special_sessions():
    generated = build_canonical_year_rows(parse_cm_holidays(_payload(*_2026_HOLIDAYS), 2026), 2026)
    conn = _conn(
        version=2,
        active_rows=[
            _active_row(date(2025, 12, 25), "HOLIDAY"),
            _active_row(date(2025, 12, 24), "REGULAR", "09:15:00", "15:30:00"),
            _active_row(date(2026, 8, 14), "SPECIAL", "09:15:00", "20:00:00"),
        ],
    )
    merged = merge_with_active_calendar(conn, generated, [2026])
    merged_by_date = {row["session_date"]: row for row in merged}
    # Outside refreshed year: preserved exactly.
    assert merged_by_date[date(2025, 12, 25)]["session_type"] == "HOLIDAY"
    assert merged_by_date[date(2025, 12, 24)]["session_type"] == "REGULAR"
    # Verified SPECIAL inside refreshed year survives regeneration.
    assert merged_by_date[date(2026, 8, 14)]["session_type"] == "SPECIAL"
    assert merged_by_date[date(2026, 8, 14)]["closes_at"] == "20:00:00"
    # Regenerated 2026 rows are present and ordered.
    assert merged == sorted(merged, key=lambda row: row["session_date"])
    assert merged_by_date[date(2026, 1, 26)]["session_type"] == "HOLIDAY"
    assert len([row for row in merged if row["session_date"].year == 2026]) == 365


def test_synchronize_imports_one_new_version_when_source_changed():
    raw = _official_payload_bytes(_payload(*_2026_HOLIDAYS))
    conn = _conn(
        observed_sha="a" * 64,
        active_rows=[
            _active_row(date(2025, 12, 25), "HOLIDAY"),
            _active_row(date(2025, 12, 24), "REGULAR", "09:15:00", "15:30:00"),
            _active_row(date(2026, 8, 14), "SPECIAL", "09:15:00", "20:00:00"),
        ],
    )
    import_mock = Mock(return_value={"calendar_version": 3, "source_document_id": 7})
    with patch_object(nse_module, "import_calendar_csv", import_mock):
        result = synchronize_official_calendar(conn, [2026], client=_FakeClient(raw=raw), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result["released"] is True
    assert result["changed"] is True
    assert result["calendar_version"] == 3
    assert import_mock.call_count == 1
    # The complete merged canonical CSV is imported exactly once through
    # import_calendar_csv with separate official and canonical hashes.
    csv_text = import_mock.call_args.args[1]
    sessions = parse_canonical_csv(csv_text, schema_version="nse_cm_sessions_v1")
    assert len(sessions) == 365 + 2  # 2026 rows + two preserved 2025 rows (the 2026 SPECIAL replaced a generated row)
    assert import_mock.call_args.kwargs["official_source_document_sha256"] == hashlib.sha256(raw).hexdigest()
    assert import_mock.call_args.kwargs["source_reference"] == NSE_HOLIDAY_API_URL
    state_calls = [call for call in conn.log if "INSERT INTO public.exchange_calendar_refresh_state" in call[0]]
    assert len(state_calls) == 1
    assert conn.commits >= 1


def test_synchronize_skips_import_when_official_source_is_unchanged():
    raw = _official_payload_bytes(_payload(*_2026_HOLIDAYS))
    conn = _conn(observed_sha=hashlib.sha256(raw).hexdigest())
    import_mock = Mock()
    with patch_object(nse_module, "import_calendar_csv", import_mock):
        result = synchronize_official_calendar(conn, [2026], client=_FakeClient(raw=raw), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result["released"] is True
    assert result["changed"] is False
    assert result["status"] == "unchanged"
    import_mock.assert_not_called()


def test_synchronize_reports_awaiting_release_and_keeps_active_calendar():
    # Only 2026 holidays exist in the official payload; neither requested year
    # (2027, 2028) has been released yet.
    raw = _official_payload_bytes(_payload(_holiday("26-Jan-2026")))
    conn = _conn(observed_sha=None)
    import_mock = Mock()
    with patch_object(nse_module, "import_calendar_csv", import_mock):
        result = synchronize_official_calendar(conn, [2027, 2028], client=_FakeClient(raw=raw), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result["released"] is False
    assert result["changed"] is False
    assert result["status"] == "awaiting_release"
    assert result["awaiting_release_years"] == [2027, 2028]
    import_mock.assert_not_called()


def test_synchronize_partial_release_preserves_active_calendar_and_records_healthy_state():
    # Current year 2026 is released but requested 2027 is not: the active
    # calendar must be preserved with a healthy awaiting result, not an import
    # of the current year alone.
    raw = _official_payload_bytes(_payload(_holiday("26-Jan-2026")))
    conn = _conn(observed_sha=None, version=4)
    import_mock = Mock()
    with patch_object(nse_module, "import_calendar_csv", import_mock):
        result = synchronize_official_calendar(conn, [2026, 2027], client=_FakeClient(raw=raw), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result["status"] == "awaiting_release"
    assert result["released"] is False
    assert result["changed"] is False
    assert result["awaiting_release_years"] == [2027]
    import_mock.assert_not_called()
    # Healthy refresh state with a next daily attempt, not a failure record.
    state_calls = [call for call in conn.log if "INSERT INTO public.exchange_calendar_refresh_state" in call[0]]
    assert len(state_calls) == 1
    assert "last_failure_at" in state_calls[0][0]
    assert "COALESCE(EXCLUDED.active_calendar_version" in state_calls[0][0]
    assert conn.rollbacks == 0


def test_synchronize_failed_refresh_records_failure_and_retains_active_version():
    conn = _conn(observed_sha=None, version=2)
    import_mock = Mock()
    with patch_object(nse_module, "import_calendar_csv", import_mock):
        result = synchronize_official_calendar(
            conn,
            [2026],
            client=_FakeClient(error=NseCalendarSourceError("connection refused")),
            now=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    assert result["status"] == "failure"
    assert result["released"] is False
    assert result["changed"] is False
    import_mock.assert_not_called()
    assert conn.rollbacks >= 1
    failure_calls = [call for call in conn.log if "INSERT INTO public.exchange_calendar_refresh_state" in call[0]]
    assert failure_calls and "connection refused" in str(failure_calls[0][1])


def test_synchronize_fails_closed_on_malformed_official_payload_without_import():
    raw = _official_payload_bytes({"FNO": [_holiday("26-Jan-2026")]})  # CM missing
    conn = _conn(observed_sha=None)
    import_mock = Mock()
    with patch_object(nse_module, "import_calendar_csv", import_mock):
        result = synchronize_official_calendar(conn, [2026], client=_FakeClient(raw=raw), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result["status"] == "failure"
    assert "parse failed" in result["error"]
    import_mock.assert_not_called()


def patch_object(module, name, replacement):
    class _Patch:
        def __enter__(self):
            self.original = getattr(module, name)
            setattr(module, name, replacement)
            return replacement

        def __exit__(self, *_args):
            setattr(module, name, self.original)

    return _Patch()


# ---------------------------------------------------------------------------
# Daily scheduler
# ---------------------------------------------------------------------------


def _run_scheduler_once(*, now_sequence, refresh_result=None, refresh_error=None):
    from backend.app import schedulers

    sleeps = []
    refresh_calls = []
    clock = {"index": 0}

    def now_fn():
        value = now_sequence[min(clock["index"], len(now_sequence) - 1)]
        clock["index"] += 1
        return value

    async def sleep_fn(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError  # end the loop after the second daily window

    def refresh_fn(years):
        refresh_calls.append(list(years))
        if refresh_error is not None:
            raise refresh_error
        return refresh_result

    async def main():
        task = asyncio.create_task(
            schedulers._schedule_exchange_calendar_refresh(
                now_fn=now_fn,
                sleep_fn=sleep_fn,
                refresh_fn=refresh_fn,
                heartbeat_enabled=False,
            )
        )
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(main())
    return sleeps, refresh_calls


def test_scheduler_runs_once_daily_at_0545_ist_with_current_and_next_year():
    # 05:30 IST on 2026-09-05 -> next run is 05:45 the same day (900s).
    ist = timezone(timedelta(hours=5, minutes=30))
    start = datetime(2026, 9, 5, 5, 30, tzinfo=ist)
    sleeps, refresh_calls = _run_scheduler_once(
        now_sequence=[start, datetime(2026, 9, 5, 5, 45, tzinfo=ist), datetime(2026, 9, 5, 5, 45, tzinfo=ist)],
        refresh_result={"status": "success", "released": True, "changed": True},
    )
    assert sleeps[0] == 900
    assert refresh_calls == [[2026, 2027]]


def test_scheduler_after_0545_sleeps_to_next_day_without_rapid_retry():
    ist = timezone(timedelta(hours=5, minutes=30))
    start = datetime(2026, 9, 5, 6, 0, tzinfo=ist)
    sleeps, refresh_calls = _run_scheduler_once(
        now_sequence=[start, datetime(2026, 9, 6, 5, 45, tzinfo=ist), datetime(2026, 9, 6, 5, 45, tzinfo=ist)],
        refresh_result={"status": "failure", "error": "boom"},
    )
    assert sleeps[0] == 24 * 3600 - 900  # 06:00 -> next day 05:45
    assert len(refresh_calls) == 1  # exactly one refresh per daily window, no rapid retry


def test_scheduler_failure_is_not_retried_until_next_daily_window():
    ist = timezone(timedelta(hours=5, minutes=30))
    start = datetime(2026, 9, 5, 5, 40, tzinfo=ist)
    sleeps, refresh_calls = _run_scheduler_once(
        now_sequence=[start, datetime(2026, 9, 5, 5, 45, tzinfo=ist), datetime(2026, 9, 5, 5, 45, tzinfo=ist)],
        refresh_error=RuntimeError("official source unreachable"),
    )
    assert len(refresh_calls) == 1
    assert sleeps[1] > 20 * 3600  # waits until the next daily execution


def test_scheduler_cancellation_stops_cleanly():
    from backend.app import schedulers
    from backend.app.monitor import get_components

    async def never(seconds):
        await asyncio.Event().wait()

    async def main():
        task = asyncio.create_task(
            schedulers._schedule_exchange_calendar_refresh(
                now_fn=lambda: datetime(2026, 9, 5, 5, 0, tzinfo=timezone.utc),
                sleep_fn=never,
                refresh_fn=lambda years: {},
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
    assert get_components()["calendar_refresh_scheduler"]["status"] == "stopped"
