from datetime import date, datetime, timezone

import pytest

from backend.broker_api.market.exchange_calendar import CSV_SCHEMA_VERSION, CalendarSchemaMigrationRequired, assess_daily_completeness, dry_run_import, get_calendar_sessions, import_calendar_csv, parse_canonical_csv, sha256_text


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
