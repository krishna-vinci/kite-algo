"""Post-close finalization of daily candles used by investment strategies.

The websocket candle aggregator is useful during the session, but its daily
candle is still forming until the exchange closes.  This module refreshes the
latest completed sessions from Kite historical data after the official NSE
close, overwriting any partial rows in ``historical_candles``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from backend.app.database import SessionLocal, get_db_connection
from backend.app.monitor import heartbeat, set_component_status, set_meta
from backend.broker_api.instruments.index_ingestion import (
    SUPPORTED_INDEXES,
    get_worker_index_snapshot,
    normalize_source_list,
)
from backend.broker_api.market.candle_ingestion import CandleIngestion
from backend.broker_api.market.exchange_calendar import get_calendar_sessions
from backend.broker_api.session.kite_session import build_kite_client, get_system_access_token


logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_RUN_AT = time(16, 0)
DEFAULT_FINALITY_DELAY = timedelta(minutes=15)
DEFAULT_RETRY_DELAYS = (2.0, 10.0)


@dataclass(frozen=True)
class FinalizationWindow:
    action: str
    session_date: date | None = None
    final_at: datetime | None = None
    reason: str | None = None


def _parse_time(value: str | time | None, default: time) -> time:
    if isinstance(value, time):
        return value
    text = str(value or "").strip()
    return time.fromisoformat(text) if text else default


def _session_final_at(
    session: Mapping[str, Any],
    *,
    finality_delay: timedelta = DEFAULT_FINALITY_DELAY,
) -> datetime:
    session_date = date.fromisoformat(str(session["session_date"]))
    close = _parse_time(session.get("closes_at"), time(15, 30))
    return datetime.combine(session_date, close, tzinfo=IST) + finality_delay


def decide_finalization_window(
    sessions: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    require_current_session: bool,
    finality_delay: timedelta = DEFAULT_FINALITY_DELAY,
) -> FinalizationWindow:
    """Select the exchange session that is safe to finalize.

    The scheduler requires today's verified session and therefore skips
    holidays.  Manual recovery selects the latest already-final session, which
    lets an operator repair Friday data during a weekend without inventing a
    trading day.
    """
    now_ist = now.astimezone(IST)
    tradable = [
        item
        for item in sessions
        if bool(item.get("verified"))
        and str(item.get("session_type") or "").upper() in {"REGULAR", "SPECIAL"}
    ]
    if require_current_session:
        today = now_ist.date().isoformat()
        matches = [item for item in tradable if str(item.get("session_date")) == today]
        if not matches:
            return FinalizationWindow(action="skip", reason="NO_TRADING_SESSION_TODAY")
        selected = matches[-1]
        final_at = _session_final_at(selected, finality_delay=finality_delay)
        if now_ist < final_at:
            return FinalizationWindow(
                action="wait",
                session_date=date.fromisoformat(today),
                final_at=final_at,
                reason="SESSION_NOT_FINAL",
            )
        return FinalizationWindow(
            action="run",
            session_date=date.fromisoformat(today),
            final_at=final_at,
        )

    finalized = [
        item
        for item in tradable
        if _session_final_at(item, finality_delay=finality_delay) <= now_ist
    ]
    if not finalized:
        return FinalizationWindow(action="skip", reason="NO_FINALIZED_SESSION")
    selected = max(finalized, key=lambda item: str(item["session_date"]))
    return FinalizationWindow(
        action="run",
        session_date=date.fromisoformat(str(selected["session_date"])),
        final_at=_session_final_at(selected, finality_delay=finality_delay),
    )


def _configured_source_lists() -> list[str]:
    raw = os.getenv("CANDLE_FINALIZATION_SOURCE_LISTS", "Nifty500")
    return [normalize_source_list(item) for item in raw.split(",") if item.strip()]


def _load_calendar(*, today: date, lookback_days: int = 14) -> list[dict[str, Any]]:
    conn = get_db_connection()
    try:
        result = get_calendar_sessions(
            conn,
            exchange="NSE",
            segment="CM",
            from_date=today - timedelta(days=lookback_days),
            to_date=today,
        )
        return list(result["sessions"])
    finally:
        conn.close()


def _load_tracker_token(source_list: str) -> dict[str, Any] | None:
    tracker_name = SUPPORTED_INDEXES[source_list].tracker_name
    if not tracker_name:
        return None
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT instrument_token, tradingsymbol, exchange
                FROM public.kite_indices
                WHERE UPPER(tradingsymbol) = UPPER(%s)
                  AND exchange = 'NSE'
                ORDER BY last_updated DESC NULLS LAST
                LIMIT 1
                """,
                (tracker_name,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError(f"INDEX_TRACKER_NOT_FOUND:{tracker_name}")
    return {"instrument_token": int(row[0]), "tradingsymbol": str(row[1]), "exchange": str(row[2])}


def load_finalization_instruments(source_lists: Iterable[str]) -> list[dict[str, Any]]:
    """Load current official constituents and their index tracker tokens."""
    instruments: dict[int, dict[str, Any]] = {}
    for value in source_lists:
        source_list = normalize_source_list(value)
        snapshot = get_worker_index_snapshot(source_list)
        for member in snapshot["members"]:
            if str(member.get("exchange") or "").upper() != "NSE":
                raise RuntimeError(f"NON_NSE_INDEX_MEMBER:{source_list}:{member.get('tradingsymbol')}")
            token = int(member["instrument_token"])
            instruments[token] = {
                "instrument_token": token,
                "tradingsymbol": str(member["tradingsymbol"]),
                "exchange": "NSE",
                "source_list": source_list,
            }
        tracker = _load_tracker_token(source_list)
        if tracker:
            instruments[int(tracker["instrument_token"])] = {**tracker, "source_list": source_list}
    return sorted(instruments.values(), key=lambda item: int(item["instrument_token"]))


def _load_kite_client():
    session = SessionLocal()
    try:
        access_token = get_system_access_token(session)
    finally:
        session.close()
    if not access_token:
        raise RuntimeError("SYSTEM_KITE_TOKEN_UNAVAILABLE")
    kite = build_kite_client(access_token, session_id="system")
    # Validate once before walking a large universe.  This prevents an expired
    # token from being retried independently for every constituent.
    kite.profile()
    return kite


async def _ingest_with_retries(
    ingestion: CandleIngestion,
    instrument: Mapping[str, Any],
    *,
    from_date: datetime,
    to_date: datetime,
    retry_delays: Sequence[float],
    sleep_fn: Callable[[float], Awaitable[Any]],
) -> dict[str, Any]:
    attempts = len(retry_delays) + 1
    last: dict[str, Any] = {}
    for attempt in range(attempts):
        try:
            last = await ingestion.ingest_historical_data(
                int(instrument["instrument_token"]),
                "day",
                from_date=from_date,
                to_date=to_date,
                force_refresh=True,
            )
        except Exception as exc:  # defensive for injected/custom ingestion implementations
            last = {"status": "error", "message": str(exc), "error": str(exc)}
        if str(last.get("status")) in {"success", "up_to_date"}:
            return {**last, "attempts": attempt + 1}
        if attempt < len(retry_delays):
            await sleep_fn(float(retry_delays[attempt]))
    return {**last, "attempts": attempts}


async def finalize_daily_candles(
    instruments: Sequence[Mapping[str, Any]],
    *,
    session_date: date,
    tail_sessions: Sequence[date],
    ingestion: CandleIngestion,
    min_request_interval: float = 0.36,
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    """Force-refresh finalized daily candles sequentially across instruments."""
    if not tail_sessions:
        raise ValueError("tail_sessions cannot be empty")
    from_date = datetime.combine(min(tail_sessions), time.min, tzinfo=IST).astimezone(timezone.utc)
    to_date = datetime.combine(session_date + timedelta(days=1), time.min, tzinfo=IST).astimezone(timezone.utc)
    results: list[dict[str, Any]] = []
    for index, instrument in enumerate(instruments):
        if index:
            await sleep_fn(min_request_interval)
        result = await _ingest_with_retries(
            ingestion,
            instrument,
            from_date=from_date,
            to_date=to_date,
            retry_delays=retry_delays,
            sleep_fn=sleep_fn,
        )
        results.append({
            "instrument_token": int(instrument["instrument_token"]),
            "tradingsymbol": instrument.get("tradingsymbol"),
            "status": result.get("status"),
            "attempts": result.get("attempts"),
            "fetched": int(result.get("fetched") or 0),
            "inserted": int(result.get("inserted") or 0),
            "updated": int(result.get("updated") or 0),
            "error": result.get("error") or (result.get("message") if result.get("status") not in {"success", "up_to_date"} else None),
        })
    failures = [item for item in results if item["status"] not in {"success", "up_to_date"}]
    return {
        "status": "success" if not failures else ("partial_success" if len(failures) < len(results) else "error"),
        "session_date": session_date.isoformat(),
        "from_session": min(tail_sessions).isoformat(),
        "instrument_count": len(instruments),
        "success_count": len(results) - len(failures),
        "failure_count": len(failures),
        "failures": failures,
        "fetched": sum(item["fetched"] for item in results),
        "inserted": sum(item["inserted"] for item in results),
        "updated": sum(item["updated"] for item in results),
    }


async def run_daily_candle_finalization(
    *,
    source_lists: Sequence[str] | None = None,
    tail_session_count: int = 3,
    require_current_session: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve the calendar/universe and run one finalization pass."""
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    source_lists = list(source_lists or _configured_source_lists())
    sessions = _load_calendar(today=now_ist.date())
    decision = decide_finalization_window(
        sessions,
        now=now_ist,
        require_current_session=require_current_session,
    )
    if decision.action != "run" or not decision.session_date:
        result = {
            "status": decision.action,
            "reason": decision.reason,
            "session_date": decision.session_date.isoformat() if decision.session_date else None,
            "final_at": decision.final_at.isoformat() if decision.final_at else None,
        }
        set_meta("daily_candle_finalization", result)
        return result

    eligible_dates = sorted(
        date.fromisoformat(str(item["session_date"]))
        for item in sessions
        if bool(item.get("verified"))
        and str(item.get("session_type") or "").upper() in {"REGULAR", "SPECIAL"}
        and date.fromisoformat(str(item["session_date"])) <= decision.session_date
    )
    tail_sessions = eligible_dates[-max(1, int(tail_session_count)):]
    instruments = await asyncio.to_thread(load_finalization_instruments, source_lists)
    kite = await asyncio.to_thread(_load_kite_client)
    set_component_status(
        "daily_candle_finalization",
        "running",
        detail=f"Finalizing {decision.session_date} daily candles",
        meta={"source_lists": source_lists, "instrument_count": len(instruments)},
    )
    result = await finalize_daily_candles(
        instruments,
        session_date=decision.session_date,
        tail_sessions=tail_sessions,
        ingestion=CandleIngestion(kite),
    )
    result.update({"source_lists": source_lists, "completed_at": datetime.now(timezone.utc).isoformat()})
    component_status = "healthy" if result["status"] == "success" else "degraded"
    set_component_status(
        "daily_candle_finalization",
        component_status,
        detail=f"Daily candle finalization {result['status']} for {decision.session_date}",
        meta=result,
    )
    set_meta("daily_candle_finalization", result)
    return result


async def schedule_daily_candle_finalization(
    *,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    run_fn: Callable[..., Awaitable[dict[str, Any]]] = run_daily_candle_finalization,
) -> None:
    """Run once per day at 16:00 IST; special sessions wait until final.

    Starting the service after 16:00 still runs that day's pass.  A transient
    setup failure is retried after one minute instead of silently deferring the
    repair until the following day.
    """
    now_fn = now_fn or (lambda: datetime.now(IST))
    processed_date: date | None = None
    set_component_status("daily_candle_finalization", "healthy", detail="Daily candle finalization scheduler started")
    while True:
        try:
            now = now_fn().astimezone(IST)
            run_at = datetime.combine(now.date(), DEFAULT_RUN_AT, tzinfo=IST)
            if now < run_at:
                heartbeat("daily_candle_finalization", detail="Waiting for post-close finalization", meta={"next_run": run_at.isoformat()})
                await sleep_fn(max(1.0, (run_at - now).total_seconds()))
            elif processed_date == now.date():
                next_run = datetime.combine(now.date() + timedelta(days=1), DEFAULT_RUN_AT, tzinfo=IST)
                heartbeat("daily_candle_finalization", detail="Waiting for post-close finalization", meta={"next_run": next_run.isoformat()})
                await sleep_fn(max(1.0, (next_run - now).total_seconds()))

            decision = await run_fn(require_current_session=True, now=now_fn())
            if decision.get("status") == "wait" and decision.get("final_at"):
                final_at = datetime.fromisoformat(str(decision["final_at"]))
                await sleep_fn(max(1.0, (final_at - now_fn().astimezone(IST)).total_seconds()))
                await run_fn(require_current_session=True, now=now_fn())
            processed_date = now_fn().astimezone(IST).date()
        except asyncio.CancelledError:
            set_component_status("daily_candle_finalization", "stopped", detail="Daily candle finalization scheduler cancelled")
            raise
        except Exception as exc:
            logger.error("Daily candle finalization scheduler failed: %s", exc, exc_info=True)
            set_component_status("daily_candle_finalization", "degraded", detail=str(exc))
            await sleep_fn(60.0)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finalize broker daily candles after NSE close")
    parser.add_argument("--source-list", action="append", dest="source_lists")
    parser.add_argument("--tail-sessions", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = asyncio.run(
        run_daily_candle_finalization(
            source_lists=args.source_lists,
            tail_session_count=args.tail_sessions,
            require_current_session=False,
        )
    )
    print(result)
    return 0 if result.get("status") in {"success", "skip"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
