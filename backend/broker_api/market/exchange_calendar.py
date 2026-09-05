"""Audited canonical exchange calendar import; it never downloads NSE data."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List

CSV_SCHEMA_VERSION = "nse_cm_sessions_v1"
REQUIRED_COLUMNS = {"session_date", "session_type", "opens_at", "closes_at", "verified"}


class CalendarUnavailable(RuntimeError):
    pass


class CalendarSchemaMigrationRequired(CalendarUnavailable):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_canonical_csv(text: str, *, schema_version: str) -> List[Dict[str, Any]]:
    if schema_version != CSV_SCHEMA_VERSION:
        raise ValueError("unsupported calendar parser/schema version")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        raise ValueError("canonical calendar CSV columns are missing")
    sessions: List[Dict[str, Any]] = []
    dates: set[date] = set()
    for raw in reader:
        session_date = date.fromisoformat(str(raw["session_date"]).strip())
        if session_date in dates:
            raise ValueError("duplicate session_date")
        dates.add(session_date)
        session_type = str(raw["session_type"]).strip().upper()
        if session_type not in {"REGULAR", "HOLIDAY", "SPECIAL"}:
            raise ValueError("unsupported session_type")
        verified = str(raw["verified"]).strip().lower() in {"1", "true", "yes"}
        if not verified:
            raise ValueError("all imported sessions must be verified")
        opens_at, closes_at = str(raw["opens_at"] or "").strip(), str(raw["closes_at"] or "").strip()
        if session_type == "HOLIDAY":
            if opens_at or closes_at:
                raise ValueError("holiday must not provide session times")
        else:
            if not opens_at or not closes_at or time.fromisoformat(opens_at) >= time.fromisoformat(closes_at):
                raise ValueError("tradable session requires ordered open and close times")
        sessions.append({"session_date": session_date, "session_type": session_type, "opens_at": opens_at or None, "closes_at": closes_at or None, "verified": True})
    if not sessions:
        raise ValueError("calendar import cannot be empty")
    return sorted(sessions, key=lambda item: item["session_date"])


def _require_sha256(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return normalized


def dry_run_import(text: str, *, source_reference: str, official_source_document_sha256: str, parser_version: str, actor: str, reason: str) -> Dict[str, Any]:
    if not all(str(value).strip() for value in (source_reference, official_source_document_sha256, parser_version, actor, reason)):
        raise ValueError("source_reference, official_source_document_sha256, parser_version, actor and reason are required")
    official_checksum = _require_sha256(official_source_document_sha256, field_name="official_source_document_sha256")
    canonical_checksum = sha256_text(text)
    sessions = parse_canonical_csv(text, schema_version=parser_version)
    return {"dry_run": True, "source_reference": source_reference, "official_source_document_sha256": official_checksum, "canonical_csv_sha256": canonical_checksum, "parser_version": parser_version, "actor": actor, "reason": reason, "session_count": len(sessions), "from": sessions[0]["session_date"].isoformat(), "to": sessions[-1]["session_date"].isoformat(), "sessions": sessions}


def require_exchange_calendar_schema(conn: Any) -> None:
    """Fail closed if Alembic has not created the calendar tables.

    Read and import paths intentionally never create or commit schema objects.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.exchange_calendar_source_documents'), to_regclass('public.exchange_calendar_sessions')")
        row = cur.fetchone() or (None, None)
    if not all(row):
        raise CalendarSchemaMigrationRequired("EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED")


def _require_exchange_calendar_refresh_state_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.exchange_calendar_refresh_state')")
        row = cur.fetchone()
    if not row or not row[0]:
        raise CalendarSchemaMigrationRequired("EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED")


def get_calendar_status(conn: Any, exchange: str, segment: str, *, now: datetime | None = None, warning_days: int = 45) -> Dict[str, Any]:
    """Schema-version-1 calendar health envelope for one exchange segment.

    Coverage is derived exclusively from the active immutable calendar version.
    Missing calendar data yields a truthful incomplete status; it never infers
    sessions. Missing migration/schema fails closed with
    CalendarSchemaMigrationRequired.
    """
    exchange_text = str(exchange or "").strip().upper()
    segment_text = str(segment or "").strip().upper()
    if not exchange_text or not segment_text:
        raise ValueError("exchange and segment are required")
    now = now or datetime.now(timezone.utc)
    retrieved_at = now.astimezone(timezone.utc).isoformat()
    require_exchange_calendar_schema(conn)
    _require_exchange_calendar_refresh_state_schema(conn)

    refresh: Dict[str, Any] = {
        "last_attempt_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "last_error": None,
        "observed_source_sha256": None,
        "next_attempt_at": None,
    }
    with conn.cursor() as cur:
        cur.execute(
            """SELECT last_attempt_at, last_success_at, last_failure_at, last_error,
                      observed_source_sha256, next_attempt_at
                 FROM public.exchange_calendar_refresh_state WHERE exchange=%s AND segment=%s""",
            (exchange_text, segment_text),
        )
        state_row = cur.fetchone()
        if state_row is not None:
            refresh = {
                "last_attempt_at": state_row[0].astimezone(timezone.utc).isoformat() if state_row[0] else None,
                "last_success_at": state_row[1].astimezone(timezone.utc).isoformat() if state_row[1] else None,
                "last_failure_at": state_row[2].astimezone(timezone.utc).isoformat() if state_row[2] else None,
                "last_error": state_row[3],
                "observed_source_sha256": state_row[4],
                "next_attempt_at": state_row[5].astimezone(timezone.utc).isoformat() if state_row[5] else None,
            }
        cur.execute(
            "SELECT MAX(calendar_version) FROM public.exchange_calendar_source_documents WHERE exchange=%s AND segment=%s",
            (exchange_text, segment_text),
        )
        version_row = cur.fetchone()
        active_version = int(version_row[0]) if version_row and version_row[0] is not None else None
        coverage_start = coverage_end = None
        if active_version is not None:
            cur.execute(
                """SELECT MIN(session_date), MAX(session_date) FROM public.exchange_calendar_sessions
                    WHERE exchange=%s AND segment=%s AND calendar_version=%s""",
                (exchange_text, segment_text, active_version),
            )
            coverage_row = cur.fetchone()
            if coverage_row is not None:
                coverage_start = coverage_row[0].isoformat() if coverage_row[0] else None
                coverage_end = coverage_row[1].isoformat() if coverage_row[1] else None

    complete = active_version is not None and coverage_start is not None and coverage_end is not None and date.fromisoformat(coverage_end) >= now.date()
    expiry_warning = bool(coverage_end and date.fromisoformat(coverage_end) < (now.date() + timedelta(days=warning_days)))
    return {
        "schema_version": 1,
        "source": "exchange_calendar_refresh",
        "source_as_of": refresh.get("last_success_at"),
        "retrieved_at": retrieved_at,
        "exchange": exchange_text,
        "segment": segment_text,
        "active_calendar_version": active_version,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "complete": complete,
        "expiry_warning": expiry_warning,
        "last_attempt_at": refresh["last_attempt_at"],
        "last_success_at": refresh["last_success_at"],
        "last_failure_at": refresh["last_failure_at"],
        "last_error": refresh["last_error"],
        "observed_source_sha256": refresh["observed_source_sha256"],
        "next_attempt_at": refresh["next_attempt_at"],
    }


def import_calendar_csv(conn: Any | None, text: str, *, exchange: str, segment: str, source_reference: str, official_source_document_sha256: str, parser_version: str, actor: str, reason: str, apply: bool) -> Dict[str, Any]:
    preview = dry_run_import(text, source_reference=source_reference, official_source_document_sha256=official_source_document_sha256, parser_version=parser_version, actor=actor, reason=reason)
    if not apply:
        return preview
    if conn is None:
        raise CalendarSchemaMigrationRequired("EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED")
    require_exchange_calendar_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(calendar_version),0) FROM public.exchange_calendar_source_documents WHERE exchange=%s AND segment=%s", (exchange, segment))
        previous = int(cur.fetchone()[0])
        version = previous + 1
        cur.execute("""INSERT INTO public.exchange_calendar_source_documents (exchange,segment,official_source_reference,official_source_document_sha256,canonical_csv_sha256,parser_version,calendar_version,actor,reason,supersedes_calendar_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING source_document_id""", (exchange, segment, source_reference, preview["official_source_document_sha256"], preview["canonical_csv_sha256"], parser_version, version, actor, reason, previous or None))
        document_id = int(cur.fetchone()[0])
        for item in preview["sessions"]:
            cur.execute("""INSERT INTO public.exchange_calendar_sessions (exchange,segment,session_date,calendar_version,session_type,opens_at,closes_at,verified,source_document_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)""", (exchange, segment, item["session_date"], version, item["session_type"], item["opens_at"], item["closes_at"], document_id))
    conn.commit()
    return {**preview, "dry_run": False, "calendar_version": version, "source_document_id": document_id}


def get_calendar_sessions(conn: Any, *, exchange: str, segment: str, from_date: date, to_date: date) -> Dict[str, Any]:
    require_exchange_calendar_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(calendar_version) FROM public.exchange_calendar_source_documents WHERE exchange=%s AND segment=%s", (exchange, segment))
        version = (cur.fetchone() or [None])[0]
        if version is None:
            raise CalendarUnavailable("CALENDAR_UNAVAILABLE")
        cur.execute("""SELECT s.session_date,s.session_type,s.opens_at,s.closes_at,s.verified,d.official_source_reference,d.official_source_document_sha256,d.canonical_csv_sha256,d.imported_at
            FROM public.exchange_calendar_sessions s JOIN public.exchange_calendar_source_documents d ON d.source_document_id=s.source_document_id
            WHERE s.exchange=%s AND s.segment=%s AND s.calendar_version=%s AND s.session_date BETWEEN %s AND %s ORDER BY s.session_date""", (exchange, segment, version, from_date, to_date))
        rows = cur.fetchall()
    if len(rows) != (to_date - from_date).days + 1 or not all(row[4] for row in rows):
        raise CalendarUnavailable("CALENDAR_RANGE_UNCOVERED")
    return {"schema_version": 1, "source": "operator_imported_official_nse_document", "source_as_of": rows[-1][8].astimezone(timezone.utc).isoformat(), "retrieved_at": datetime.now(timezone.utc).isoformat(), "exchange": exchange, "segment": segment, "calendar_version": int(version), "official_source_document_sha256": rows[-1][6], "canonical_csv_sha256": rows[-1][7], "sessions": [{"session_date": row[0].isoformat(), "session_type": row[1], "opens_at": row[2].isoformat() if row[2] else None, "closes_at": row[3].isoformat() if row[3] else None, "verified": row[4], "source_reference": row[5]} for row in rows]}


def assess_daily_completeness(candles: List[Dict[str, Any]], calendar: Dict[str, Any], *, now: datetime | None = None, finality_delay_seconds: int = 900, ingestion_status: str = "completed") -> Dict[str, Any]:
    """Assess daily candles exclusively against imported verified sessions."""
    now = now or datetime.now(timezone.utc)
    expected = [item for item in calendar["sessions"] if item["session_type"] in {"REGULAR", "SPECIAL"}]
    dates = []
    for candle in candles:
        raw = candle.get("timestamp") or candle.get("ts")
        if not raw:
            continue
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        ist = timezone(timedelta(hours=5, minutes=30))
        dates.append(parsed.astimezone(ist).date().isoformat())
    actual_dates = set(dates)
    expected_dates = [item["session_date"] for item in expected]
    missing = sorted(set(expected_dates) - actual_dates)
    duplicate = sorted({item for item in dates if dates.count(item) > 1})
    last_final = False
    if expected:
        last = expected[-1]
        close = time.fromisoformat(str(last["closes_at"]))
        close_at = datetime.combine(date.fromisoformat(last["session_date"]), close, tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc)
        last_final = now >= close_at + timedelta(seconds=finality_delay_seconds)
    ingestion_settled = ingestion_status in {"completed", "up_to_date", "disabled"}
    complete = not missing and not duplicate and last_final and ingestion_settled
    reasons = []
    if missing: reasons.append("MISSING_SESSIONS")
    if duplicate: reasons.append("DUPLICATE_SESSIONS")
    if not last_final: reasons.append("LAST_CANDLE_NOT_FINAL")
    if not ingestion_settled: reasons.append("INGESTION_INCOMPLETE")
    return {"calendar_version": calendar["calendar_version"], "expected_sessions": len(expected_dates), "actual_sessions": len(actual_dates), "missing_sessions": missing, "duplicate_sessions": duplicate, "last_candle_final": last_final, "ingestion_status": ingestion_status, "complete": complete, "completeness_reasons": reasons}
