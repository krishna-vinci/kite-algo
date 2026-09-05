"""Official NSE CM holiday synchronization.

Reads the official NSE holiday master, regenerates deterministic canonical
years, merges them with the active immutable calendar version, and imports the
complete merged CSV exactly once. The active version is never touched on
failure, and an unchanged official source never creates a new version.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

import requests

from backend.broker_api.market.exchange_calendar import (
    CSV_SCHEMA_VERSION,
    _require_exchange_calendar_refresh_state_schema,
    import_calendar_csv,
    require_exchange_calendar_schema,
    sha256_text,
)

EXCHANGE = "NSE"
SEGMENT = "CM"
NSE_CALENDAR_ACTOR = "system:calendar_refresh"

NSE_HOLIDAY_WARMUP_URL = "https://www.nseindia.com/resources/exchange-communication-holidays"
NSE_HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_TIMEOUT_SECONDS = 20

ACCEPTED_TRADING_DATE_FORMATS = ("%d-%b-%Y", "%d-%b-%y", "%d %B, %Y", "%Y-%m-%d")

REGULAR_OPEN = "09:15:00"
REGULAR_CLOSE = "15:30:00"


class NseCalendarSourceError(RuntimeError):
    """Raised when the official NSE source cannot be trusted (fail closed)."""


class NseCalendarSourceClient:
    """Fetches the official NSE holiday master behind its warm-up page."""

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self._session = session or requests.Session()
        self.timeout = NSE_TIMEOUT_SECONDS
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": NSE_HOLIDAY_WARMUP_URL,
        }

    def fetch(self) -> bytes:
        """Return the exact official response bytes; fail closed otherwise."""
        warm_up = self._session.get(NSE_HOLIDAY_WARMUP_URL, headers=self.headers, timeout=self.timeout)
        warm_up.raise_for_status()
        response = self._session.get(NSE_HOLIDAY_API_URL, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "")
        if "text/html" in content_type.lower():
            raise NseCalendarSourceError("official NSE holiday endpoint returned HTML instead of JSON")
        raw = response.content
        if not raw:
            raise NseCalendarSourceError("official NSE holiday endpoint returned an empty response")
        return raw


def _parse_trading_date(value: Any) -> date:
    text = str(value or "").strip()
    if not text:
        raise NseCalendarSourceError("official holiday row has an empty tradingDate")
    for fmt in ACCEPTED_TRADING_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise NseCalendarSourceError(f"unsupported official tradingDate format: {text!r}")


def parse_cm_holidays(payload: Any, year: int) -> List[date]:
    """Return the sorted official CM holiday dates for ``year``.

    Fails closed on missing CM data, non-JSON shapes, malformed dates, and
    duplicate dates. An empty result means the year has not been released yet.
    """
    year = int(year)
    if not isinstance(payload, Mapping):
        raise NseCalendarSourceError("official holiday payload is not a JSON object")
    cm = payload.get("CM")
    if not isinstance(cm, list) or not cm:
        raise NseCalendarSourceError("official holiday payload is missing CM data")
    holidays: Dict[date, None] = {}
    for item in cm:
        if not isinstance(item, Mapping):
            raise NseCalendarSourceError("official CM holiday row is not an object")
        parsed = _parse_trading_date(item.get("tradingDate"))
        if parsed.year != year:
            continue
        if parsed in holidays:
            raise NseCalendarSourceError(f"duplicate official CM holiday date: {parsed.isoformat()}")
        holidays[parsed] = None
    return sorted(holidays)


def build_canonical_year_rows(holidays: Iterable[date], year: int) -> List[Dict[str, Any]]:
    """Generate exactly 365 or 366 deterministic rows for ``year``.

    Weekdays default to verified REGULAR sessions; weekends and official CM
    holidays are HOLIDAY rows without session times. SPECIAL sessions are never
    invented here.
    """
    year = int(year)
    holiday_dates = {day if isinstance(day, date) and not isinstance(day, datetime) else _parse_trading_date(day) for day in holidays}
    rows: List[Dict[str, Any]] = []
    day = date(year, 1, 1)
    while day <= date(year, 12, 31):
        if day in holiday_dates or day.weekday() >= 5:
            rows.append({"session_date": day, "session_type": "HOLIDAY", "opens_at": None, "closes_at": None, "verified": True})
        else:
            rows.append({"session_date": day, "session_type": "REGULAR", "opens_at": REGULAR_OPEN, "closes_at": REGULAR_CLOSE, "verified": True})
        day += timedelta(days=1)
    if len(rows) not in (365, 366):
        raise NseCalendarSourceError(f"generated year {year} has {len(rows)} rows; expected 365 or 366")
    return rows


def _load_active_calendar_sessions(conn: Any) -> tuple[Optional[int], List[Dict[str, Any]]]:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(calendar_version) FROM public.exchange_calendar_source_documents WHERE exchange=%s AND segment=%s", (EXCHANGE, SEGMENT))
        version_row = cur.fetchone()
        version = int(version_row[0]) if version_row and version_row[0] is not None else None
        if version is None:
            return None, []
        cur.execute(
            """SELECT session_date, session_type, opens_at, closes_at, verified
                 FROM public.exchange_calendar_sessions
                WHERE exchange=%s AND segment=%s AND calendar_version=%s ORDER BY session_date""",
            (EXCHANGE, SEGMENT, version),
        )
        rows = [
            {
                "session_date": row[0],
                "session_type": str(row[1]),
                "opens_at": row[2].isoformat() if row[2] else None,
                "closes_at": row[3].isoformat() if row[3] else None,
                "verified": bool(row[4]),
            }
            for row in cur.fetchall()
        ]
    return version, rows


def merge_with_active_calendar(conn: Any, generated_rows: List[Dict[str, Any]], refreshed_years: Iterable[int]) -> List[Dict[str, Any]]:
    """Merge regenerated years with every session of the active version.

    Dates outside the refreshed years are preserved as-is; verified SPECIAL
    sessions inside refreshed years survive regeneration.
    """
    refreshed = {int(year) for year in refreshed_years}
    _version, active_rows = _load_active_calendar_sessions(conn)
    merged: Dict[date, Dict[str, Any]] = {}

    for row in active_rows:
        session_date = row["session_date"]
        if isinstance(session_date, str):
            session_date = date.fromisoformat(session_date)
            row = {**row, "session_date": session_date}
        if session_date.year in refreshed:
            continue
        merged[session_date] = row

    for row in generated_rows:
        merged[row["session_date"]] = row

    for row in active_rows:
        session_date = row["session_date"]
        if isinstance(session_date, str):
            session_date = date.fromisoformat(session_date)
        if session_date.year in refreshed and row["verified"] and row["session_type"] == "SPECIAL":
            merged[session_date] = {**row, "session_date": session_date}

    return sorted(merged.values(), key=lambda row: row["session_date"])


def render_canonical_csv(rows: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["session_date", "session_type", "opens_at", "closes_at", "verified"])
    for row in sorted(rows, key=lambda item: item["session_date"]):
        session_date = row["session_date"]
        writer.writerow([
            session_date.isoformat() if isinstance(session_date, date) else str(session_date),
            row["session_type"],
            row.get("opens_at") or "",
            row.get("closes_at") or "",
            "true" if row.get("verified") else "false",
        ])
    return buffer.getvalue()


def _next_daily_attempt(now: datetime) -> datetime:
    ist = ZoneInfo("Asia/Kolkata")
    local = now.astimezone(ist)
    next_run = local.replace(hour=5, minute=45, second=0, microsecond=0)
    if local >= next_run:
        next_run += timedelta(days=1)
    return next_run.astimezone(timezone.utc)


def _record_refresh_failure(conn: Any, now: datetime, message: str) -> None:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.exchange_calendar_refresh_state
                   (exchange, segment, last_attempt_at, last_failure_at, last_error, next_attempt_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (exchange, segment) DO UPDATE SET
                   last_attempt_at = EXCLUDED.last_attempt_at,
                   last_failure_at = EXCLUDED.last_failure_at,
                   last_error = EXCLUDED.last_error,
                   next_attempt_at = EXCLUDED.next_attempt_at,
                   updated_at = NOW()""",
            (EXCHANGE, SEGMENT, now, now, message[:2000], _next_daily_attempt(now)),
        )
    conn.commit()


def _record_refresh_success(
    conn: Any,
    now: datetime,
    *,
    observed_sha: str,
    active_version: Optional[int] = None,
    coverage_start: Optional[date] = None,
    coverage_end: Optional[date] = None,
    next_attempt: Optional[datetime] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.exchange_calendar_refresh_state
                   (exchange, segment, last_attempt_at, last_success_at, last_failure_at, last_error,
                    observed_source_sha256, active_calendar_version, coverage_start, coverage_end, next_attempt_at, updated_at)
               VALUES (%s, %s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (exchange, segment) DO UPDATE SET
                   last_attempt_at = EXCLUDED.last_attempt_at,
                   last_success_at = EXCLUDED.last_success_at,
                   last_failure_at = NULL,
                   last_error = NULL,
                   observed_source_sha256 = EXCLUDED.observed_source_sha256,
                   active_calendar_version = COALESCE(EXCLUDED.active_calendar_version, public.exchange_calendar_refresh_state.active_calendar_version),
                   coverage_start = COALESCE(EXCLUDED.coverage_start, public.exchange_calendar_refresh_state.coverage_start),
                   coverage_end = COALESCE(EXCLUDED.coverage_end, public.exchange_calendar_refresh_state.coverage_end),
                   next_attempt_at = EXCLUDED.next_attempt_at,
                   updated_at = NOW()""",
            (EXCHANGE, SEGMENT, now, now, observed_sha, active_version, coverage_start, coverage_end, next_attempt),
        )
    conn.commit()


def _read_previous_observed_sha(conn: Any) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT observed_source_sha256 FROM public.exchange_calendar_refresh_state WHERE exchange=%s AND segment=%s", (EXCHANGE, SEGMENT))
        row = cur.fetchone()
    return str(row[0]).strip().lower() if row and row[0] else None


def synchronize_official_calendar(conn: Any, years: Iterable[int], client: Optional[NseCalendarSourceClient] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Refresh the canonical calendar from the official NSE source.

    Returns a result dict with ``released`` and ``changed`` flags. The active
    immutable version is only replaced when every requested year has been
    released AND the official source hash changed; a partial or missing release
    yields a healthy awaiting_release result, and any failure retains the
    existing active version.
    """
    years = sorted({int(year) for year in years})
    if not years:
        raise ValueError("at least one calendar year is required")
    now = now or datetime.now(timezone.utc)
    client = client or NseCalendarSourceClient()
    require_exchange_calendar_schema(conn)
    _require_exchange_calendar_refresh_state_schema(conn)

    try:
        raw = client.fetch()
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        message = f"official NSE source fetch failed: {exc}"
        _record_refresh_failure(conn, now, message)
        return {"status": "failure", "released": False, "changed": False, "error": message}

    official_sha = hashlib.sha256(raw).hexdigest()
    try:
        released_rows: Dict[int, List[Dict[str, Any]]] = {}
        for year in years:
            holidays = parse_cm_holidays(payload, year)
            if holidays:
                released_rows[year] = build_canonical_year_rows(holidays, year)
    except Exception as exc:
        message = f"official NSE source parse failed: {exc}"
        _record_refresh_failure(conn, now, message)
        return {"status": "failure", "released": False, "changed": False, "official_source_sha256": official_sha, "error": message}

    awaiting_release_years = sorted(set(years) - set(released_rows))
    if awaiting_release_years:
        # Any requested year without official data — including a partial release
        # where only the current year is available — preserves the active
        # calendar and records a healthy awaiting state, never a failure.
        _record_refresh_success(conn, now, observed_sha=official_sha, next_attempt=_next_daily_attempt(now))
        return {
            "status": "awaiting_release",
            "released": False,
            "changed": False,
            "awaiting_release_years": awaiting_release_years,
            "official_source_sha256": official_sha,
        }

    refreshed_years = sorted(released_rows)
    generated_rows = [row for year in refreshed_years for row in released_rows[year]]
    merged_rows = merge_with_active_calendar(conn, generated_rows, refreshed_years)
    canonical_csv = render_canonical_csv(merged_rows)
    canonical_sha = sha256_text(canonical_csv)

    if _read_previous_observed_sha(conn) == official_sha:
        _record_refresh_success(conn, now, observed_sha=official_sha, next_attempt=_next_daily_attempt(now))
        return {
            "status": "unchanged",
            "released": True,
            "changed": False,
            "refreshed_years": refreshed_years,
            "official_source_sha256": official_sha,
            "canonical_csv_sha256": canonical_sha,
            "session_count": len(merged_rows),
        }

    result = import_calendar_csv(
        conn,
        canonical_csv,
        exchange=EXCHANGE,
        segment=SEGMENT,
        source_reference=NSE_HOLIDAY_API_URL,
        official_source_document_sha256=official_sha,
        parser_version=CSV_SCHEMA_VERSION,
        actor=NSE_CALENDAR_ACTOR,
        reason=f"official NSE CM holiday sync for {', '.join(str(y) for y in refreshed_years)}",
        apply=True,
    )
    _record_refresh_success(
        conn,
        now,
        observed_sha=official_sha,
        active_version=int(result["calendar_version"]),
        coverage_start=merged_rows[0]["session_date"],
        coverage_end=merged_rows[-1]["session_date"],
        next_attempt=_next_daily_attempt(now),
    )
    return {
        "status": "success",
        "released": True,
        "changed": True,
        "refreshed_years": refreshed_years,
        "calendar_version": int(result["calendar_version"]),
        "official_source_sha256": official_sha,
        "canonical_csv_sha256": canonical_sha,
        "session_count": len(merged_rows),
        "source_document_id": result.get("source_document_id"),
    }
