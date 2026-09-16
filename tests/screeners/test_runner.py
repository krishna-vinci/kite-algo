"""Phase 3 F9 acceptance: screener pipeline over deterministic stored data.

- ranking is deterministic: ties break by instrument identity, direction
  applies to scores, top-N boundary is exact;
- missing data is excluded with a typed reason and makes the run partial —
  never a failed match;
- the as-of cutoff never consumes candles after it;
- fundamentals context rides into member values with acquisition metadata;
- screener-only stored fields (change_pct, turnover) are computed from the
  latest completed daily candles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.screeners.runner import ScreenerPipeline, compute_screener_bucket
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict

T0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)  # intraday Wednesday


def _screener_doc(rank=True, top_n=None, direction="desc", stage_conditions=None):
    doc = {
        "version": 1,
        "name": "top-momentum",
        "session": "nse_equity",
        "universe": {"union": [{"universe": "my-list"}]},
        "stages": [
            {
                "id": "scan",
                "type": "filter",
                "clock": "candle_close",
                "timeframe": "1d",
                "conditions": stage_conditions
                or {"all": [{"left": {"field": "close"}, "op": "gt", "right": {"value": 90}}]},
            }
        ],
        "alerts": [],
        "screener": {
            "schedule": {"every": "1d", "at": "session_close"},
            **({"rank": {"by": {"field": "change_pct"}, "direction": direction}} if rank else {}),
            **({"top_n": top_n} if top_n else {}),
            "attachments": [],
        },
    }
    return doc


class _Bar:
    def __init__(self, ts, close, volume):
        self.ts = ts
        self.epoch_id = "screener"
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = volume
        self.final = True


class _Bars:
    def __init__(self, closes_by_key, *, base=T0, prev_by_key=None, volumes=None):
        self.closes = closes_by_key
        self.prev = prev_by_key or {}
        self.volumes = volumes or {}
        self._base = base

    def recent_bars(self, key, timeframe, limit):
        if key not in self.closes:
            return []
        close = self.closes[key]
        prev_close = self.prev.get(key, close)
        volume = self.volumes.get(key, 1000.0)
        bars = [
            _Bar(self._base + timedelta(days=i), prev_close, volume)
            for i in range(9)
        ]
        bars.append(_Bar(self._base + timedelta(days=9), close, volume))
        return bars


def _evaluate(doc_dict, history, members, *, as_of=T0 + timedelta(days=10), context=None):
    pipeline = ScreenerPipeline(candle_history=history, window_bars=120)
    compiled = compile_document(parse_workflow_dict(doc_dict))
    return pipeline.evaluate(
        compiled.document,
        members,
        as_of=as_of,
        context_loader=(lambda key: (context or {}).get(key)) if context is not None else None,
    )


def test_rank_desc_ties_break_by_instrument_identity():
    history = _Bars({
        "NSE:AAA": 110.0,
        "NSE:BBB": 110.0,
        "NSE:CCC": 105.0,
    }, prev_by_key={"NSE:AAA": 100.0, "NSE:BBB": 100.0, "NSE:CCC": 100.0})
    outcome = _evaluate(_screener_doc(), history, ["NSE:CCC", "NSE:BBB", "NSE:AAA"])
    ranked = sorted((m for m in outcome["members"] if m.passed), key=lambda m: m.rank)
    assert [m.instrument_key for m in ranked] == ["NSE:AAA", "NSE:BBB", "NSE:CCC"]
    assert [m.rank for m in ranked] == [1, 2, 3]
    assert outcome["status"] == "complete"
    assert outcome["coverage"]["qualifying"] == 3


def test_rank_asc_reverses_scores_but_not_ties():
    history = _Bars({
        "NSE:AAA": 105.0,
        "NSE:BBB": 105.0,
        "NSE:CCC": 110.0,
    }, prev_by_key={"NSE:AAA": 100.0, "NSE:BBB": 100.0, "NSE:CCC": 100.0})
    outcome = _evaluate(_screener_doc(direction="asc"), history, ["NSE:AAA", "NSE:BBB", "NSE:CCC"])
    ranked = sorted((m for m in outcome["members"] if m.passed), key=lambda m: m.rank)
    assert [m.instrument_key for m in ranked] == ["NSE:AAA", "NSE:BBB", "NSE:CCC"]
    assert [m.rank for m in ranked] == [1, 2, 3]


def test_top_n_boundary_is_exact_and_deterministic():
    history = _Bars({
        "NSE:A1": 120.0,
        "NSE:A2": 119.0,
        "NSE:A3": 118.0,
        "NSE:A4": 117.0,
    }, prev_by_key={k: 100.0 for k in ["NSE:A1", "NSE:A2", "NSE:A3", "NSE:A4"]})
    outcome = _evaluate(_screener_doc(top_n=2), history, ["NSE:A4", "NSE:A3", "NSE:A2", "NSE:A1"])
    passing = sorted(m.instrument_key for m in outcome["members"] if m.passed)
    assert passing == ["NSE:A1", "NSE:A2"]
    excluded = {m.instrument_key: m for m in outcome["members"] if not m.passed}
    assert excluded["NSE:A3"].exclusion_reason == "beyond_top_n"
    assert excluded["NSE:A3"].rank == 3  # rank recorded even beyond the cut
    assert outcome["status"] == "complete"  # truncation is not missing data


def test_missing_data_is_unavailable_not_a_failed_match():
    history = _Bars({"NSE:HAS": 110.0}, prev_by_key={"NSE:HAS": 100.0})
    outcome = _evaluate(_screener_doc(), history, ["NSE:HAS", "NSE:NODATA"])
    by_key = {m.instrument_key: m for m in outcome["members"]}
    assert by_key["NSE:NODATA"].exclusion_reason == "no_data"
    assert by_key["NSE:NODATA"].matched is None
    assert outcome["coverage"]["unavailable"] == 1
    assert outcome["status"] == "partial"


def test_condition_failure_is_a_valid_negative_not_unavailable():
    # close 90 is NOT > 90 -> failed condition: evaluated, excluded, complete
    history = _Bars({"NSE:FLAT": 90.0}, prev_by_key={"NSE:FLAT": 100.0})
    outcome = _evaluate(_screener_doc(), history, ["NSE:FLAT"])
    member = outcome["members"][0]
    assert member.matched is False
    assert member.exclusion_reason == "condition_filter"
    assert outcome["status"] == "complete"
    assert outcome["coverage"]["qualifying"] == 0


def test_as_of_cutoff_never_consumes_future_candles():
    base = T0
    history = _Bars({"NSE:AAA": 110.0}, prev_by_key={"NSE:AAA": 100.0}, base=base)
    compiled = compile_document(parse_workflow_dict(_screener_doc()))
    pipeline = ScreenerPipeline(candle_history=history, window_bars=120)
    outcome = pipeline.evaluate(
        compiled.document, ["NSE:AAA"], as_of=base + timedelta(days=4)
    )
    candle_ts = datetime.fromisoformat(outcome["members"][0].values["candle_ts"])
    assert candle_ts <= base + timedelta(days=4)


def test_stored_fields_change_pct_and_turnover_are_computed():
    history = _Bars(
        {"NSE:AAA": 110.0},
        prev_by_key={"NSE:AAA": 100.0},
        volumes={"NSE:AAA": 2000.0},
    )
    outcome = _evaluate(_screener_doc(), history, ["NSE:AAA"])
    values = outcome["members"][0].values
    assert values["change_pct"] == pytest.approx(10.0)
    assert values["turnover"] == pytest.approx(220000.0)


def test_fundamentals_metadata_copies_into_values():
    history = _Bars({"NSE:AAA": 110.0}, prev_by_key={"NSE:AAA": 100.0})
    doc = _screener_doc(
        stage_conditions={
            "all": [{"field": "fundamentals.pe_ratio", "op": "lt", "value": 40}]
        }
    )
    context = {
        "NSE:AAA": {
            "fundamentals.pe_ratio": 21.5,
            "fundamentals.acquired_at": "2026-09-01T18:30:00+00:00",
        }
    }
    outcome = _evaluate(doc, history, ["NSE:AAA"], context=context)
    assert outcome["members"][0].passed is True
    assert outcome["members"][0].values["fundamentals.acquired_at"] == "2026-09-01T18:30:00+00:00"
    assert outcome["status"] == "complete"


def test_missing_fundamentals_are_unknown_not_false():
    history = _Bars({"NSE:AAA": 110.0}, prev_by_key={"NSE:AAA": 100.0})
    doc = _screener_doc(
        stage_conditions={
            "all": [{"field": "fundamentals.pe_ratio", "op": "lt", "value": 40}]
        }
    )
    outcome = _evaluate(doc, history, ["NSE:AAA"], context={})
    member = outcome["members"][0]
    assert member.matched is None  # unknown propagates; never a false match
    assert member.passed is False
    assert member.exclusion_reason == "fundamentals_unknown"
    assert outcome["status"] == "partial"


def test_zero_denominator_change_pct_is_unknown():
    history = _Bars(
        {"NSE:AAA": 110.0},
        prev_by_key={"NSE:AAA": 0.0},  # zero previous close
    )
    outcome = _evaluate(_screener_doc(), history, ["NSE:AAA"])
    values = outcome["members"][0].values
    assert values["change_pct"] is None  # E-26: unknown, not exception/0


# ---------------------------------------------------------------------------
# schedule buckets
# ---------------------------------------------------------------------------


def test_bucket_intraday_aligns_to_epoch_multiples():
    now = datetime(2026, 9, 9, 10, 7, tzinfo=timezone.utc)
    bucket = compute_screener_bucket(900, None, now)
    assert bucket.timestamp() <= now.timestamp()
    assert int(bucket.timestamp()) % 900 == 0


def test_bucket_daily_uses_ist_wall_clock_session_close():
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)  # 17:30 IST
    bucket = compute_screener_bucket(86400, "session_close", now)
    assert bucket.hour == 10 and bucket.minute == 0  # 15:30 IST == 10:00 UTC
    assert bucket.date() == now.date()


def test_bucket_walks_back_over_inactive_sessions():
    def gate(at):
        return (at.weekday() < 5, "session")

    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)  # Saturday
    bucket = compute_screener_bucket(86400, "session_close", now, session_gate=gate)
    assert bucket.weekday() < 5
    assert bucket.date().isoformat() == "2026-09-11"


def test_bucket_gate_never_active_returns_none():
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    bucket = compute_screener_bucket(
        86400, "session_close", now, session_gate=lambda at: (False, "x")
    )
    assert bucket is None
