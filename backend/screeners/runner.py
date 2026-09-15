"""Screener pipeline (Phase 3 F9): stored data -> filters -> deterministic rank.

A screener document reuses the canonical workflow schema (stages, operands,
three-valued conditions, feature functions) but is evaluated as a SCHEDULED
SCAN over stored completed candles — never live ticks:

    stored daily candles + latest fundamentals snapshot
      -> per-member filter/signal stage chain (3VL, layered)
      -> deterministic ranking (ties break by instrument identity)
      -> persisted run with coverage, freshness and per-member evidence.

Honest data semantics:

- coherent cutoff: only completed daily candles with ``ts <= as_of`` are
  consumed — a run never sees future or incomplete candles;
- missing data is NOT a failed match: a member without data, with unknown
  conditions (insufficient history, missing fundamentals) or without a rank
  value is EXCLUDED with a typed reason and reported in run coverage;
- fundamentals are the latest stored snapshot (§5.8) and their acquisition
  metadata is copied into member values.

Run status is precise (E-18):

- ``complete`` — every expected member had data, every condition resolved,
  every passing member had a rank value (when ranking is configured);
- ``partial``  — results exist but at least one member was excluded for a
  data reason; visible, never drives attachments or downstream universes;
- ``failed``   — the pipeline itself errored (recorded, retried by claim).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from backend.alerts.predicates import Observation, evaluate_stage
from backend.screeners.candle_warming import (
    applies_daily_finality,
    bar_session_date,
    daily_session_is_final,
)
from backend.alerts import predicates as _predicates
from backend.workflows.feature_engine import FeatureEngine
from backend.workflows.feature_planner import build_subscription_plan, stage_chain
from backend.workflows.models import Operand, WorkflowDocument

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
__all__ = [
    "ScreenerPipeline",
    "MemberResult",
    "compute_screener_bucket",
]

DEFAULT_WINDOW_BARS = 120
_DEFAULT_WINDOW_BARS = DEFAULT_WINDOW_BARS
_SESSION_CLOSE = "15:30"


@dataclass
class MemberResult:
    instrument_key: str
    matched: Optional[bool]  # None = unknown (3VL)
    exclusion_reason: Optional[str]
    values: dict = field(default_factory=dict)
    score: Optional[float] = None
    rank: Optional[int] = None
    passed: bool = False


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def compute_screener_bucket(
    every_s: int,
    at: Optional[str],
    now: datetime,
    *,
    session_gate=None,
    max_walkback: int = 40,
) -> Optional[datetime]:
    """The latest DUE schedule bucket at or before ``now`` (IST anchored).

    Buckets align to UTC epoch multiples of ``every_s`` (intraday) or to the
    configured IST wall-clock time (daily). With ``session_gate`` (a callable
    returning (session_active, session_id) for the nse_equity calendar), a
    bucket on a non-session day (holiday/weekend) is not due: walk back to
    the most recent bucket that falls inside an active session, bounded by
    ``max_walkback`` steps (coalescing — E-19: only the LATEST due bucket
    is ever returned, so a backlog never replays).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_epoch = now.timestamp()
    if at is None:
        candidate_epoch = int(now_epoch // every_s) * every_s
    else:
        clock = at if at != "session_close" else _SESSION_CLOSE
        hours, minutes = (int(part) for part in clock.split(":"))
        ist_now = now.astimezone(IST)
        candidate_day = ist_now.date()
        while True:
            candidate = datetime(
                candidate_day.year, candidate_day.month, candidate_day.day,
                hours, minutes, tzinfo=IST,
            )
            if candidate.timestamp() <= now_epoch:
                candidate_epoch = int(candidate.timestamp())
                break
            candidate_day = candidate_day - timedelta(days=1)
    steps = 0
    while steps <= max_walkback:
        candidate = datetime.fromtimestamp(candidate_epoch, tz=timezone.utc)
        if session_gate is None:
            return candidate
        active, _session_id = session_gate(candidate)
        if active:
            return candidate
        candidate_epoch -= every_s
        steps += 1
    return None


class ScreenerPipeline:
    """Evaluate screener documents over stored candles (no live state).

    ``warmer`` is optional: when present, the members that need daily history
    acquire it (bounded) before evaluation, and the outcome records what was
    warmed, skipped or unavailable — so a run that evaluates nothing because the
    data was never fetched is distinguishable from a run where nothing matched.
    """

    def __init__(
        self,
        *,
        candle_history,
        window_bars: int = _DEFAULT_WINDOW_BARS,
        warmer: Any = None,
    ) -> None:
        self.candle_history = candle_history
        self.window_bars = max(30, int(window_bars))
        self.warmer = warmer

    # ------------------------------------------------------------------
    def evaluate(
        self,
        document: WorkflowDocument,
        members: Sequence[str],
        *,
        as_of: datetime,
        context_loader=None,
        member_limit: Optional[int] = None,
    ) -> dict:
        """Run the pipeline; returns members, coverage and freshness.

        ``context_loader(instrument_key) -> Optional[dict]`` supplies the
        fundamentals context (production: FundamentalsLoader). The result is
        pure — persistence and attachments live with the caller.
        """
        terminal = self._terminal_stage(document)
        session = str(getattr(document, "session", "") or "")
        universe_members = [str(m) for m in members]
        if member_limit is not None:
            universe_members = sorted(universe_members)[: int(member_limit)]
        expected = len(universe_members)

        warming = None
        if self.warmer is not None and universe_members:
            try:
                warming = self.warmer.ensure_members(
                    universe_members, session=session, as_of=as_of
                )
            except Exception:  # warming must never fail the run
                logger.exception("screener candle warming failed")
                warming = None

        results: List[MemberResult] = []
        candle_max_ts: Optional[datetime] = None
        forming_excluded = 0

        for key in universe_members:
            bars, dropped_forming = self._bars(key, as_of, session=session)
            forming_excluded += dropped_forming
            if bars:
                latest_ts = bars[-1].ts
                if candle_max_ts is None or latest_ts > candle_max_ts:
                    candle_max_ts = latest_ts
            if not bars:
                results.append(
                    MemberResult(
                        instrument_key=key,
                        matched=None,
                        exclusion_reason="no_data",
                        values={"expected_bars": self.window_bars},
                    )
                )
                continue
            context = dict(context_loader(key) or {}) if context_loader is not None else {}
            result = self._evaluate_member(document, terminal, key, bars, context, as_of)
            results.append(result)

        if terminal is not None and document.screener is not None and document.screener.rank is not None:
            self._rank(results, document.screener.rank, document.screener.top_n)

        evaluated = sum(1 for r in results if r.exclusion_reason != "no_data")
        unavailable = expected - evaluated
        unknown_conditions = sum(1 for r in results if r.exclusion_reason in ("insufficient_history", "condition_unknown", "fundamentals_unknown"))
        rank_missing = sum(1 for r in results if r.exclusion_reason == "rank_value_missing")
        passed = [r for r in results if r.passed]
        complete = unavailable == 0 and unknown_conditions == 0 and rank_missing == 0
        coverage = {
            "expected": expected,
            "evaluated": evaluated,
            "unavailable": unavailable,
            "unknown_conditions": unknown_conditions,
            "rank_value_missing": rank_missing,
            "qualifying": len(passed),
            "complete": complete,
        }
        if warming is not None:
            coverage["candle_warming"] = warming.to_coverage()
        freshness = {
            "candle_max_ts": candle_max_ts.isoformat() if candle_max_ts else None,
            "as_of": as_of.isoformat(),
        }
        if applies_daily_finality(session):
            freshness["candle_finality_session"] = session
            freshness["forming_candles_excluded"] = forming_excluded
        return {
            "members": results,
            "coverage": coverage,
            "data_freshness": freshness,
            "status": "complete" if complete else ("partial" if evaluated > 0 else "failed"),
        }

    # ------------------------------------------------------------------
    def _bars(
        self, instrument_key: str, as_of: datetime, *, session: str = ""
    ) -> tuple[List[Observation], int]:
        """Bars a run may consume, and how many were dropped as still forming.

        Coherent cutoff: no candle at/after the as-of instant, so a run never
        consumes future data whatever its bucket time is. For feed-driven
        sessions (MCX, currency) the schedule is anchored to the NSE calendar,
        so a bucket can fire mid-session; that session's daily bar is dropped
        until the exchange has closed and the provider row has settled
        (``applies_daily_finality``). NSE equity buckets fire at its own close,
        where the newest bar already is the session's close.
        """
        recent = self.candle_history.recent_bars(instrument_key, "day", self.window_bars)
        cutoff = applies_daily_finality(session)
        now = datetime.now(timezone.utc)
        consumed: List[Observation] = []
        forming = 0
        for bar in recent:
            ts = self._ts(bar)
            if ts is None or ts > as_of:
                continue
            if cutoff and not daily_session_is_final(session, bar_session_date(ts), now):
                forming += 1
                continue
            consumed.append(bar)
        return consumed, forming

    @staticmethod
    def _ts(bar: Any) -> Optional[datetime]:
        ts = getattr(bar, "ts", None)
        if isinstance(ts, datetime):
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        return None

    def _terminal_stage(self, document: WorkflowDocument):
        candidates = [s for s in document.stages if s.type in ("signal", "filter")]
        return candidates[-1] if candidates else None

    # ------------------------------------------------------------------
    def _evaluate_member(
        self,
        document: WorkflowDocument,
        terminal,
        instrument_key: str,
        bars: Sequence[Observation],
        context: dict,
        as_of: datetime,
    ) -> MemberResult:
        latest = bars[-1]
        engine = FeatureEngine()
        features: Dict[str, Any] = {}
        layers: List = []
        if terminal is not None:
            plan = build_subscription_plan(document, terminal.id)
            for timeframe, spec in plan.specs:
                engine.declare(instrument_key, timeframe, spec)
            for bar in bars:
                engine.on_bar(instrument_key, "day", bar)
            for timeframe in plan.feature_timeframes:
                snapshot = engine.snapshot(instrument_key, timeframe)
                for feature_id, value in snapshot.items():
                    features.setdefault(feature_id, value)
                for name, value in engine.field_snapshot(instrument_key, timeframe).items():
                    features.setdefault(name, value)
            for ancestor, _tf in plan.layers:
                layers.append((ancestor, None))

        values = {
            "close": _finite(getattr(latest, "close", None)),
            "open": _finite(getattr(latest, "open", None)),
            "high": _finite(getattr(latest, "high", None)),
            "low": _finite(getattr(latest, "low", None)),
            "volume": _finite(getattr(latest, "volume", None)),
            "candle_ts": self._ts(latest).isoformat() if self._ts(latest) else None,
        }
        prev = bars[-2] if len(bars) >= 2 else None
        close_now = values["close"]
        close_prev = _finite(getattr(prev, "close", None)) if prev is not None else None
        change_pct = None
        if close_now is not None and close_prev not in (None, 0):
            change_pct = (close_now - close_prev) / close_prev * 100.0
        turnover = None
        if close_now is not None and values["volume"] is not None:
            turnover = close_now * values["volume"]
        if change_pct is not None:
            features.setdefault("field:change_pct", change_pct)
        if turnover is not None:
            features.setdefault("field:turnover", turnover)
        values["change_pct"] = change_pct
        values["turnover"] = turnover
        for meta_key in ("fundamentals.acquired_at", "fundamentals.as_of_date"):
            if context.get(meta_key) is not None:
                values[meta_key] = context[meta_key]

        if terminal is None:
            return MemberResult(instrument_key, None, "no_pipeline_stage", values)

        terminal_result = evaluate_stage(terminal, latest, {}, context, features)
        matched = terminal_result.matched
        for ancestor, _ancestor_features in layers:
            ancestor_result = evaluate_stage(ancestor, latest, {}, context, features)
            if matched is None or ancestor_result.matched is None:
                matched = None  # unknown propagates through the layer AND
            else:
                matched = bool(matched and ancestor_result.matched)
        if matched is None:
            if _uses_fundamentals(document, terminal) and not any(
                str(k).startswith("fundamentals.") for k in context
            ):
                reason = "fundamentals_unknown"
            elif any(
                value is None
                for feature_id, value in features.items()
                if not str(feature_id).startswith("field:")
            ):
                reason = "insufficient_history"
            else:
                reason = "condition_unknown"
            return MemberResult(instrument_key, None, reason, values)

        values["matched"] = bool(matched)
        if not matched:
            return MemberResult(instrument_key, False, "condition_filter", values)

        screener = document.screener
        if screener is not None and screener.rank is not None:
            score = self._score(screener.rank.by, latest, context, features)
            if score is None:
                return MemberResult(instrument_key, True, "rank_value_missing", values)
            values["score"] = score
            return MemberResult(instrument_key, True, None, values, score=score, passed=True)
        return MemberResult(instrument_key, True, None, values, passed=True)

    @staticmethod
    def _score(operand: Operand, obs: Observation, context: dict, features: dict) -> Optional[float]:
        value = _predicates._resolve_operand(operand, obs, context, features)
        return _finite(value)

    # ------------------------------------------------------------------
    def _rank(self, results: List[MemberResult], rank_spec, top_n: Optional[int]) -> None:
        """Deterministic ranking: null scores never rank; ties break by
        stable instrument identity ascending regardless of direction."""
        candidates = [r for r in results if r.passed and r.score is not None]
        reverse = rank_spec.direction == "desc"
        candidates.sort(key=lambda r: (-(r.score) if reverse else r.score, r.instrument_key))
        limit = top_n if top_n is not None else len(candidates)
        for position, member in enumerate(candidates, start=1):
            member.rank = position
            if position <= limit:
                member.passed = True
            else:
                member.passed = False
                member.exclusion_reason = "beyond_top_n"


def _uses_fundamentals(document: WorkflowDocument, terminal) -> bool:
    from backend.workflows.registry import stage_uses_fundamentals

    if terminal is None:
        return False
    chain = [terminal, *stage_chain(document, terminal.id)]
    return any(stage_uses_fundamentals(stage) for stage in chain)
