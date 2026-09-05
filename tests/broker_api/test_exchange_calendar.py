from datetime import date, datetime, timedelta, timezone

import pytest

from backend.broker_api.market.exchange_calendar import CSV_SCHEMA_VERSION, CalendarSchemaMigrationRequired, assess_daily_completeness, dry_run_import, get_calendar_sessions, get_calendar_status, import_calendar_csv, parse_canonical_csv, sha256_text


CSV = """session_date,session_type,opens_at,closes_at,verified
2026-08-28,REGULAR,09:15:00,15:30:00,true
2026-08-29,HOLIDAY,,,true
2026-08-30,SPECIAL,18:15:00,19:15:00,true
"""


def test_canonical_import_requires_audited_source_metadata_and_dry_run():
    official_source_checksum = "a" * 64
    preview = dry_run_import(CSV, source_reference="https://www.nseindia.com/example-circular", official_source_document_sha256=official_source_checksum, parser_version=CSV_SCHEMA_VERSION, actor="operator@example", reason="official circular import")
    assert preview["dry_run"] is True
    assert preview["session_count"] == 3
    assert preview["sessions"][2]["session_type"] == "SPECIAL"
    assert preview["official_source_document_sha256"] == official_source_checksum
    assert preview["canonical_csv_sha256"] == sha256_text(CSV)
    assert preview["canonical_csv_sha256"] != official_source_checksum


class _MissingSchemaCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return None

    def fetchone(self):
        return (None, None)


class _MissingSchemaConnection:
    def cursor(self):
        return _MissingSchemaCursor()


def test_apply_and_read_fail_clearly_when_alembic_schema_is_absent():
    connection = _MissingSchemaConnection()
    with pytest.raises(CalendarSchemaMigrationRequired, match="EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED"):
        import_calendar_csv(connection, CSV, exchange="NSE", segment="CM", source_reference="https://www.nseindia.com/example-circular", official_source_document_sha256="b" * 64, parser_version=CSV_SCHEMA_VERSION, actor="operator@example", reason="official circular import", apply=True)
    with pytest.raises(CalendarSchemaMigrationRequired, match="EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED"):
        get_calendar_sessions(connection, exchange="NSE", segment="CM", from_date=date(2026, 8, 28), to_date=date(2026, 8, 28))


def test_unverified_or_ambiguous_rows_are_rejected():
    try:
        parse_canonical_csv("session_date,session_type,opens_at,closes_at,verified\n2026-08-29,REGULAR,,,true\n", schema_version=CSV_SCHEMA_VERSION)
    except ValueError as exc:
        assert "tradable" in str(exc)
    else:
        raise AssertionError("invalid regular session was accepted")


def test_daily_completeness_uses_imported_sessions_not_weekdays():
    calendar = {"calendar_version": 7, "sessions": [
        {"session_date": "2026-08-28", "session_type": "REGULAR", "closes_at": "15:30:00"},
        {"session_date": "2026-08-29", "session_type": "HOLIDAY", "closes_at": None},
        {"session_date": "2026-08-30", "session_type": "SPECIAL", "closes_at": "19:15:00"},
    ]}
    result = assess_daily_completeness([{"timestamp": "2026-08-28T15:30:00+05:30"}, {"timestamp": "2026-08-30T19:15:00+05:30"}], calendar, now=datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc), ingestion_status="completed")
    assert result["complete"] is True
    assert result["expected_sessions"] == 2
    assert result["missing_sessions"] == []


def test_daily_completeness_fails_on_missing_or_nonfinal_session():
    calendar = {"calendar_version": 7, "sessions": [{"session_date": "2026-08-28", "session_type": "REGULAR", "closes_at": "15:30:00"}]}
    result = assess_daily_completeness([], calendar, now=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc), ingestion_status="triggered")
    assert result["complete"] is False
    assert "MISSING_SESSIONS" in result["completeness_reasons"]


def test_daily_completeness_accepts_complete_cached_read_with_ingestion_disabled():
    calendar = {"calendar_version": 7, "sessions": [{"session_date": "2026-08-28", "session_type": "REGULAR", "closes_at": "15:30:00"}]}
    result = assess_daily_completeness(
        [{"timestamp": "2026-08-28T15:30:00+05:30"}],
        calendar,
        now=datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc),
        ingestion_status="disabled",
    )
    assert result["complete"] is True
    assert result["last_candle_final"] is True
    assert result["completeness_reasons"] == []


# ---------------------------------------------------------------------------
# Calendar status envelope (kite-algo-worker SDK 0.7.6)
# ---------------------------------------------------------------------------

class _StatusCursor:
    def __init__(self, results):
        self._results = results
        self._row = None

    def execute(self, sql, _params=None):
        self._row = None
        for fragment, row in self._results:
            if fragment in sql:
                self._row = row
                return
        raise AssertionError(f"unexpected SQL in calendar status: {sql}")

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _StatusConnection:
    def __init__(self, results):
        self._results = results

    def cursor(self):
        return _StatusCursor(self._results)


def _status_connection(*, calendar_schema=True, refresh_schema=True, refresh_row=None, version=3, coverage=(date(2026, 1, 1), date(2026, 12, 31))):
    return _StatusConnection([
        ("exchange_calendar_source_documents'), to_regclass", ("public.exchange_calendar_source_documents", "public.exchange_calendar_sessions") if calendar_schema else (None, None)),
        ("exchange_calendar_refresh_state')", ("public.exchange_calendar_refresh_state",) if refresh_schema else (None,)),
        ("SELECT last_attempt_at", refresh_row),
        ("SELECT MAX(calendar_version)", (version,)),
        ("SELECT MIN(session_date), MAX(session_date)", coverage if version is not None else None),
    ])


def _refresh_row():
    return (
        datetime(2026, 9, 4, 0, 15, 10, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 0, 16, 12, tzinfo=timezone.utc),
        None,
        None,
        "d" * 64,
        datetime(2026, 9, 5, 0, 15, 0, tzinfo=timezone.utc),
    )


def test_calendar_status_locks_schema_v1_envelope_with_healthy_coverage():
    conn = _status_connection(refresh_row=_refresh_row())
    status = get_calendar_status(conn, "NSE", "CM", now=datetime(2026, 9, 5, 0, 15, tzinfo=timezone.utc))
    assert status["schema_version"] == 1
    assert status["source"] == "exchange_calendar_refresh"
    assert status["exchange"] == "NSE"
    assert status["segment"] == "CM"
    assert status["active_calendar_version"] == 3
    assert status["coverage_start"] == "2026-01-01"
    assert status["coverage_end"] == "2026-12-31"
    assert status["complete"] is True
    # 2026-12-31 is more than 45 days after 2026-09-05.
    assert status["expiry_warning"] is False
    assert status["last_success_at"] == "2026-09-04T00:16:12+00:00"
    assert status["observed_source_sha256"] == "d" * 64
    assert status["retrieved_at"].startswith("2026-09-05T00:15:00")


def test_calendar_status_warns_when_coverage_expires_within_warning_window():
    conn = _status_connection(coverage=(date(2026, 1, 1), date(2026, 10, 1)))
    status = get_calendar_status(conn, "NSE", "CM", now=datetime(2026, 9, 5, tzinfo=timezone.utc))
    # 2026-10-01 is inside the 45-day window ending 2026-10-20.
    assert status["expiry_warning"] is True
    assert status["complete"] is True

    conn = _status_connection(coverage=(date(2026, 1, 1), date(2026, 10, 20)))
    status = get_calendar_status(conn, "NSE", "CM", now=datetime(2026, 9, 5, tzinfo=timezone.utc), warning_days=45)
    assert status["expiry_warning"] is False


def test_calendar_status_is_truthful_when_calendar_data_is_missing():
    conn = _status_connection(version=None)
    status = get_calendar_status(conn, "NSE", "CM", now=datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert status["active_calendar_version"] is None
    assert status["coverage_start"] is None
    assert status["coverage_end"] is None
    assert status["complete"] is False
    assert status["expiry_warning"] is False


def test_calendar_status_normalizes_identity_and_reports_fresh_state_when_row_missing():
    conn = _status_connection(refresh_row=None)
    status = get_calendar_status(conn, "nse", "cm", now=datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert status["exchange"] == "NSE"
    assert status["segment"] == "CM"
    assert status["last_attempt_at"] is None
    assert status["last_failure_at"] is None
    assert status["next_attempt_at"] is None


def test_calendar_status_fails_closed_when_any_calendar_schema_is_missing():
    with pytest.raises(CalendarSchemaMigrationRequired, match="EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED"):
        get_calendar_status(_status_connection(calendar_schema=False), "NSE", "CM")
    with pytest.raises(CalendarSchemaMigrationRequired, match="EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED"):
        get_calendar_status(_status_connection(refresh_schema=False), "NSE", "CM")
