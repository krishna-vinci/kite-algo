"""Bounded candle warming for screener universes: behaviour, not snapshots.

Every test here drives the real warmer with a fake history reader and a fake
ingestion adapter, so what is asserted is the contract the screener depends on:
bounded work, idempotent repeats, explicit unavailability, and final-session
handling for feed-driven exchanges.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.screeners.candle_warming import (
    FINALITY_DELAY,
    ScreenerCandleWarmer,
    applies_daily_finality,
    bar_session_date,
    build_screener_warmer,
    daily_session_is_final,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: date):
    return SimpleNamespace(ts=datetime(day.year, day.month, day.day, tzinfo=IST))


class FakeHistory:
    """Stand-in for PgCandleHistory: per-key daily bars, newest last."""

    def __init__(self, bars: dict[str, list[date]] | None = None) -> None:
        self.bars = {key: sorted(value) for key, value in (bars or {}).items()}
        self.reads: list[str] = []

    def recent_bars(self, instrument_key: str, interval: str, limit: int):
        self.reads.append(instrument_key)
        days = self.bars.get(instrument_key, [])
        return [_bar(day) for day in days[-limit:]]


class FakeCatalog:
    def __init__(self, descriptors: dict[str, SimpleNamespace]) -> None:
        self.descriptors = descriptors
        self.resolved: list[str] = []

    def resolve_public_key(self, public_key: str):
        self.resolved.append(public_key)
        if public_key not in self.descriptors:
            raise KeyError(public_key)
        return self.descriptors[public_key]


class FakeIngestion:
    """Records the calls; optionally persists concrete session dates."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, datetime, datetime]] = []

    async def ingest_historical_data(self, token, interval, from_dt, to_dt, force_refresh=False):
        self.calls.append((token, interval, from_dt, to_dt))
        return {"status": "success", "inserted": 5, "updated": 0}


def _descriptor(token: int, generation: str = "gen-1", lifecycle: str = "active"):
    return SimpleNamespace(
        broker_token=token, catalog_generation=generation, lifecycle_status=lifecycle
    )


def _trading_days(count: int, *, through: date) -> list[date]:
    days: list[date] = []
    cursor = through
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)  # 17:30 IST, MCX still open


# ---------------------------------------------------------------------------
# finality
# ---------------------------------------------------------------------------


def test_feed_driven_sessions_are_the_only_ones_filtered():
    assert applies_daily_finality("mcx_commodity") is True
    assert applies_daily_finality("currency") is True
    # NSE buckets fire at the NSE close, so its bar already is the close.
    assert applies_daily_finality("nse_equity") is False
    assert applies_daily_finality("") is False


def test_mcx_session_bar_is_not_final_until_after_its_close():
    session_date = date(2026, 9, 15)
    assert daily_session_is_final("mcx_commodity", session_date, NOW) is False
    just_after_close = datetime(2026, 9, 15, 23, 30, tzinfo=IST) + FINALITY_DELAY
    assert daily_session_is_final("mcx_commodity", session_date, just_after_close) is True
    # a previous session is final regardless of the current time
    assert daily_session_is_final("mcx_commodity", date(2026, 9, 14), NOW) is True


def test_unknown_session_never_filters_data():
    assert daily_session_is_final("something_new", date(2026, 9, 15), NOW) is True


def test_bar_session_date_uses_the_ist_session_day():
    # 2026-09-14T18:30Z is 2026-09-15T00:00 IST: the 15th's session.
    assert bar_session_date(datetime(2026, 9, 14, 18, 30, tzinfo=timezone.utc)) == date(2026, 9, 15)


# ---------------------------------------------------------------------------
# warming
# ---------------------------------------------------------------------------


def _warmer(history, catalog, ingestion, **kwargs):
    return ScreenerCandleWarmer(
        history,
        catalog=catalog,
        ingestion_factory=lambda: ingestion,
        required_bars=kwargs.pop("required_bars", 5),
        clock=lambda: NOW,
        **kwargs,
    )


def test_members_with_enough_history_are_left_alone():
    days = _trading_days(6, through=date(2026, 9, 14))
    history = FakeHistory({"MCX:A": days})
    catalog = FakeCatalog({"MCX:A": _descriptor(101)})
    ingestion = FakeIngestion()
    outcome = _warmer(history, catalog, ingestion).ensure_members(
        ["MCX:A"], session="mcx_commodity", as_of=NOW
    )
    assert outcome.status == "complete"
    assert outcome.fresh == 1 and outcome.warmed == 0
    assert ingestion.calls == []  # nothing fetched: idempotent repeat


def test_missing_history_is_fetched_and_reported_as_warmed():
    history = FakeHistory({})
    catalog = FakeCatalog({"MCX:A": _descriptor(101)})
    ingestion = FakeIngestion()

    class PersistingIngestion(FakeIngestion):
        async def ingest_historical_data(self, token, interval, from_dt, to_dt, force_refresh=False):
            history.bars["MCX:A"] = _trading_days(6, through=date(2026, 9, 14))
            return await super().ingest_historical_data(
                token, interval, from_dt, to_dt, force_refresh=force_refresh
            )

    outcome = _warmer(history, catalog, PersistingIngestion()).ensure_members(
        ["MCX:A"], session="mcx_commodity", as_of=NOW
    )
    assert outcome.status == "complete"
    assert outcome.warmed == 1
    assert outcome.members[0].broker_token == 101
    assert outcome.catalog_generation == "gen-1"


def test_the_forming_session_is_not_counted_as_history():
    """Today's MCX bar exists but is not final, so it cannot satisfy warming."""
    days = _trading_days(6, through=NOW.astimezone(IST).date())
    history = FakeHistory({"MCX:A": days})
    catalog = FakeCatalog({"MCX:A": _descriptor(101)})
    outcome = _warmer(history, catalog, FakeIngestion()).ensure_members(
        ["MCX:A"], session="mcx_commodity", as_of=NOW
    )
    # 6 stored sessions, but the current one is excluded -> 5 final bars
    assert outcome.members[0].bars == 5
    assert outcome.fresh == 1


def test_insufficient_history_after_fetch_is_unavailable_not_a_match():
    history = FakeHistory({"MCX:A": _trading_days(2, through=date(2026, 9, 14))})
    catalog = FakeCatalog({"MCX:A": _descriptor(101)})
    outcome = _warmer(history, catalog, FakeIngestion()).ensure_members(
        ["MCX:A"], session="mcx_commodity", as_of=NOW
    )
    assert outcome.status == "unavailable"
    assert outcome.unavailable == 1
    assert outcome.members[0].status == "unavailable"
    assert "2/5 final bars" in (outcome.members[0].detail or "")


def test_expired_contract_is_reported_not_silently_bound():
    history = FakeHistory({})
    catalog = FakeCatalog({"MCX:A": _descriptor(101, lifecycle="expired")})
    ingestion = FakeIngestion()
    outcome = _warmer(history, catalog, ingestion).ensure_members(
        ["MCX:A"], session="mcx_commodity", as_of=NOW
    )
    assert outcome.expired == 1
    assert ingestion.calls == []  # never fetches a dead token
    assert "expired" in (outcome.members[0].detail or "")


def test_member_cap_is_reported_and_retryable():
    keys = [f"MCX:{index}" for index in range(5)]
    history = FakeHistory({})
    catalog = FakeCatalog({key: _descriptor(100 + index) for index, key in enumerate(keys)})
    outcome = _warmer(history, catalog, FakeIngestion(), max_members=2).ensure_members(
        keys, session="mcx_commodity", as_of=NOW
    )
    assert outcome.requested == 5
    assert outcome.skipped == 3
    assert outcome.budget_exhausted is False
    assert {m.instrument_key for m in outcome.members if m.status == "skipped"} == set(keys[2:])


def test_wall_clock_budget_stops_work_and_reports_skipped():
    keys = ["MCX:A", "MCX:B", "MCX:C"]
    history = FakeHistory({})
    catalog = FakeCatalog({key: _descriptor(100) for key in keys})

    # monotonic(): start of the call, then the first member's check, then past
    # the deadline — so exactly one member is attempted before the budget stops
    # the rest, and the remainder stays retryable.
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else 999.0

    warmer = ScreenerCandleWarmer(
        history,
        catalog=catalog,
        ingestion_factory=lambda: FakeIngestion(),
        required_bars=5,
        deadline_s=10.0,
        clock=lambda: NOW,
    )
    import backend.screeners.candle_warming as module

    original = module.time.monotonic
    module.time.monotonic = fake_monotonic
    try:
        outcome = warmer.ensure_members(keys, session="mcx_commodity", as_of=NOW)
    finally:
        module.time.monotonic = original
    assert outcome.budget_exhausted is True
    assert outcome.status == "skipped"
    assert outcome.skipped == 2
    assert {m.instrument_key for m in outcome.members if m.status == "skipped"} == {"MCX:B", "MCX:C"}


def test_catalog_failure_is_isolated_to_that_member():
    history = FakeHistory({})
    catalog = FakeCatalog({"MCX:B": _descriptor(102)})
    outcome = _warmer(history, catalog, FakeIngestion()).ensure_members(
        ["MCX:A", "MCX:B"], session="mcx_commodity", as_of=NOW
    )
    by_key = {m.instrument_key: m for m in outcome.members}
    assert by_key["MCX:A"].status == "unavailable"
    assert "catalog resolution failed" in (by_key["MCX:A"].detail or "")
    assert by_key["MCX:B"].status in {"unavailable", "warmed"}


def test_storage_read_failure_is_unknown_not_empty():
    class ExplodingHistory(FakeHistory):
        def recent_bars(self, instrument_key, interval, limit):
            raise RuntimeError("storage unavailable")

    outcome = _warmer(ExplodingHistory({}), FakeCatalog({"MCX:A": _descriptor(101)}), FakeIngestion()).ensure_members(
        ["MCX:A"], session="mcx_commodity", as_of=NOW
    )
    assert outcome.unavailable == 1  # never reported as "no data" silently


def test_data_status_is_read_only():
    days = _trading_days(6, through=date(2026, 9, 14))
    history = FakeHistory({"MCX:A": days, "MCX:B": days[:2]})
    catalog = FakeCatalog({"MCX:A": _descriptor(101), "MCX:B": _descriptor(102)})
    ingestion = FakeIngestion()
    rows = _warmer(history, catalog, ingestion).data_status(
        ["MCX:A", "MCX:B"], session="mcx_commodity", as_of=NOW
    )
    assert [row["sufficient"] for row in rows] == [True, False]
    assert rows[1]["warming"] is True
    assert ingestion.calls == []


def test_coverage_payload_is_compact_and_serialisable():
    import json

    days = _trading_days(6, through=date(2026, 9, 14))
    history = FakeHistory({"MCX:A": days})
    outcome = _warmer(history, FakeCatalog({"MCX:A": _descriptor(101)}), FakeIngestion()).ensure_members(
        ["MCX:A"], session="mcx_commodity", as_of=NOW
    )
    payload = outcome.to_coverage()
    json.dumps(payload)  # must be storable in a JSON coverage column
    assert payload["status"] == "complete"
    assert payload["required_bars"] == 5
    assert payload["members"][0]["instrument_key"] == "MCX:A"


def test_warmer_bounds_come_from_the_environment(monkeypatch):
    warmer = build_screener_warmer(
        FakeHistory({}),
        required_bars=30,
        env={
            "ALERTS_SCREENER_WARM_MAX_MEMBERS": "7",
            "ALERTS_SCREENER_WARM_LOOKBACK_DAYS": "90",
            "ALERTS_SCREENER_WARM_DEADLINE_S": "12.5",
        },
    )
    assert (warmer.max_members, warmer.lookback_days, warmer.deadline_s) == (7, 90, 12.5)


# ---------------------------------------------------------------------------
# pipeline integration
# ---------------------------------------------------------------------------


def _screener_doc(session: str = "mcx_commodity"):
    return {
        "version": 1,
        "name": "mcx-scan",
        "session": session,
        "universe": {"union": [{"universe": "my-list"}]},
        "stages": [
            {
                "id": "scan",
                "type": "filter",
                "clock": "candle_close",
                "timeframe": "1d",
                "conditions": {"all": [{"left": {"field": "close"}, "op": "gt", "right": {"value": 1}}]},
            }
        ],
        "alerts": [],
        "screener": {
            "schedule": {"every": "1d", "at": "session_close"},
            "rank": {"by": {"field": "change_pct"}, "direction": "desc"},
            "attachments": [],
        },
    }


class _PipelineHistory:
    def __init__(self, bars_by_key):
        self.bars = bars_by_key

    def recent_bars(self, key, timeframe, limit):
        return self.bars.get(key, [])[-limit:]


class _RecordingWarmer:
    def __init__(self, outcome_payload):
        self.payload = outcome_payload
        self.calls = []

    def ensure_members(self, members, *, session, as_of):
        self.calls.append((sorted(members), session))
        return SimpleNamespace(to_coverage=lambda: self.payload)


def _bars_for(days: list[date], close: float = 100.0):
    return [SimpleNamespace(ts=datetime(d.year, d.month, d.day, tzinfo=IST), open=close,
                            high=close, low=close, close=close, volume=1.0, epoch_id="x")
            for d in days]


def test_pipeline_records_the_warming_outcome_in_coverage():
    from backend.screeners.runner import ScreenerPipeline
    from backend.workflows.compiler import compile_document
    from backend.workflows.parser import parse_workflow_dict

    days = _trading_days(35, through=date(2026, 9, 14))
    history = _PipelineHistory({"MCX:A": _bars_for(days)})
    warmer = _RecordingWarmer({"status": "complete", "warmed": 1, "members": []})
    pipeline = ScreenerPipeline(candle_history=history, window_bars=30, warmer=warmer)
    document = compile_document(parse_workflow_dict(_screener_doc())).document

    outcome = pipeline.evaluate(document, ["MCX:A"], as_of=NOW)

    assert warmer.calls == [(["MCX:A"], "mcx_commodity")]
    assert outcome["coverage"]["candle_warming"] == {"status": "complete", "warmed": 1, "members": []}
    # 35 stored sessions includes today's forming MCX bar -> 34 final bars
    assert outcome["coverage"]["expected"] == 1
    assert outcome["coverage"]["evaluated"] == 1


def test_pipeline_excludes_the_forming_mcx_session_from_the_ranking():
    from backend.screeners.runner import ScreenerPipeline
    from backend.workflows.compiler import compile_document
    from backend.workflows.parser import parse_workflow_dict

    today = NOW.astimezone(IST).date()
    days = _trading_days(34, through=date(2026, 9, 14)) + [today]
    bars = _bars_for(days[:-1], close=100.0) + [
        SimpleNamespace(ts=datetime(today.year, today.month, today.day, tzinfo=IST),
                        open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0, epoch_id="x")
    ]
    pipeline = ScreenerPipeline(candle_history=_PipelineHistory({"MCX:A": bars}), window_bars=30)
    document = compile_document(parse_workflow_dict(_screener_doc())).document

    outcome = pipeline.evaluate(document, ["MCX:A"], as_of=NOW)

    assert outcome["data_freshness"]["forming_candles_excluded"] == 1
    # the still-forming bar (close=1.0) must not become the ranked close
    assert outcome["members"][0].values["close"] == 100.0


def test_pipeline_keeps_the_session_close_bar_for_nse_equity():
    from backend.screeners.runner import ScreenerPipeline
    from backend.workflows.compiler import compile_document
    from backend.workflows.parser import parse_workflow_dict

    today = NOW.astimezone(IST).date()
    days = _trading_days(33, through=date(2026, 9, 14)) + [today]
    bars = _bars_for(days[:-1], close=100.0) + [
        SimpleNamespace(ts=datetime(today.year, today.month, today.day, tzinfo=IST),
                        open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0, epoch_id="x")
    ]
    pipeline = ScreenerPipeline(candle_history=_PipelineHistory({"NSE:A": bars}), window_bars=30)
    document = compile_document(parse_workflow_dict(_screener_doc("nse_equity"))).document

    outcome = pipeline.evaluate(document, ["NSE:A"], as_of=NOW)

    assert "forming_candles_excluded" not in outcome["data_freshness"]
    # NSE buckets fire at the NSE close, so the newest bar is the ranked close
    assert outcome["members"][0].values["close"] == 1.0


def test_pipeline_warming_failure_never_fails_the_run():
    from backend.screeners.runner import ScreenerPipeline
    from backend.workflows.compiler import compile_document
    from backend.workflows.parser import parse_workflow_dict

    class ExplodingWarmer:
        def ensure_members(self, members, *, session, as_of):
            raise RuntimeError("broker down")

    days = _trading_days(35, through=date(2026, 9, 14))
    pipeline = ScreenerPipeline(
        candle_history=_PipelineHistory({"MCX:A": _bars_for(days)}),
        window_bars=30,
        warmer=ExplodingWarmer(),
    )
    document = compile_document(parse_workflow_dict(_screener_doc())).document

    outcome = pipeline.evaluate(document, ["MCX:A"], as_of=NOW)

    assert outcome["coverage"]["evaluated"] == 1
    assert "candle_warming" not in outcome["coverage"]
