"""Audited canonical exchange calendar import; it never downloads NSE data."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List

CSV_SCHEMA_VERSION = "nse_cm_sessions_v1"
REQUIRED_COLUMNS = {"session_date", "session_type", "opens_at", "closes_at", "verified"}


class CalendarUnavailable(RuntimeError):
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


def dry_run_import(text: str, *, source_reference: str, sha256: str, parser_version: str, actor: str, reason: str) -> Dict[str, Any]:
    if not all(str(value).strip() for value in (source_reference, sha256, parser_version, actor, reason)):
        raise ValueError("source_reference, sha256, parser_version, actor and reason are required")
    actual = sha256_text(text)
    if actual != sha256.lower():
        raise ValueError("provided SHA-256 does not match CSV content")
    sessions = parse_canonical_csv(text, schema_version=parser_version)
    return {"dry_run": True, "source_reference": source_reference, "sha256": actual, "parser_version": parser_version, "actor": actor, "reason": reason, "session_count": len(sessions), "from": sessions[0]["session_date"].isoformat(), "to": sessions[-1]["session_date"].isoformat(), "sessions": sessions}


def ensure_exchange_calendar_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS public.exchange_calendar_source_documents (
            source_document_id BIGSERIAL PRIMARY KEY, exchange TEXT NOT NULL, segment TEXT NOT NULL,
            official_source_reference TEXT NOT NULL, content_sha256 CHAR(64) NOT NULL, parser_version TEXT NOT NULL,
            calendar_version BIGINT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            supersedes_calendar_version BIGINT, UNIQUE(exchange, segment, calendar_version), UNIQUE(exchange, segment, content_sha256))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS public.exchange_calendar_sessions (
            exchange TEXT NOT NULL, segment TEXT NOT NULL, session_date DATE NOT NULL, calendar_version BIGINT NOT NULL,
            session_type TEXT NOT NULL CHECK (session_type IN ('REGULAR','HOLIDAY','SPECIAL')), opens_at TIME, closes_at TIME,
            verified BOOLEAN NOT NULL, source_document_id BIGINT NOT NULL REFERENCES public.exchange_calendar_source_documents(source_document_id),
            PRIMARY KEY(exchange, segment, session_date, calendar_version))""")
    conn.commit()


def import_calendar_csv(conn: Any, text: str, *, exchange: str, segment: str, source_reference: str, sha256: str, parser_version: str, actor: str, reason: str, apply: bool) -> Dict[str, Any]:
    preview = dry_run_import(text, source_reference=source_reference, sha256=sha256, parser_version=parser_version, actor=actor, reason=reason)
    if not apply:
        return preview
    ensure_exchange_calendar_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(calendar_version),0) FROM public.exchange_calendar_source_documents WHERE exchange=%s AND segment=%s", (exchange, segment))
        previous = int(cur.fetchone()[0])
        version = previous + 1
        cur.execute("""INSERT INTO public.exchange_calendar_source_documents (exchange,segment,official_source_reference,content_sha256,parser_version,calendar_version,actor,reason,supersedes_calendar_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING source_document_id""", (exchange, segment, source_reference, preview["sha256"], parser_version, version, actor, reason, previous or None))
        document_id = int(cur.fetchone()[0])
        for item in preview["sessions"]:
            cur.execute("""INSERT INTO public.exchange_calendar_sessions (exchange,segment,session_date,calendar_version,session_type,opens_at,closes_at,verified,source_document_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)""", (exchange, segment, item["session_date"], version, item["session_type"], item["opens_at"], item["closes_at"], document_id))
    conn.commit()
    return {**preview, "dry_run": False, "calendar_version": version, "source_document_id": document_id}


def get_calendar_sessions(conn: Any, *, exchange: str, segment: str, from_date: date, to_date: date) -> Dict[str, Any]:
    ensure_exchange_calendar_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(calendar_version) FROM public.exchange_calendar_source_documents WHERE exchange=%s AND segment=%s", (exchange, segment))
        version = (cur.fetchone() or [None])[0]
        if version is None:
            raise CalendarUnavailable("CALENDAR_UNAVAILABLE")
        cur.execute("""SELECT s.session_date,s.session_type,s.opens_at,s.closes_at,s.verified,d.official_source_reference,d.imported_at
            FROM public.exchange_calendar_sessions s JOIN public.exchange_calendar_source_documents d ON d.source_document_id=s.source_document_id
            WHERE s.exchange=%s AND s.segment=%s AND s.calendar_version=%s AND s.session_date BETWEEN %s AND %s ORDER BY s.session_date""", (exchange, segment, version, from_date, to_date))
        rows = cur.fetchall()
    if len(rows) != (to_date - from_date).days + 1 or not all(row[4] for row in rows):
        raise CalendarUnavailable("CALENDAR_RANGE_UNCOVERED")
    return {"schema_version": 1, "source": "operator_imported_official_nse_document", "source_as_of": rows[-1][6].astimezone(timezone.utc).isoformat(), "retrieved_at": datetime.now(timezone.utc).isoformat(), "exchange": exchange, "segment": segment, "calendar_version": int(version), "sessions": [{"session_date": row[0].isoformat(), "session_type": row[1], "opens_at": row[2].isoformat() if row[2] else None, "closes_at": row[3].isoformat() if row[3] else None, "verified": row[4], "source_reference": row[5]} for row in rows]}
