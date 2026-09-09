"""Pure condition evaluation for alert rules: state in -> state out.

Semantics (spec v2 §4 F3, §5; plan "Shared contract"):

- Level ops (``gt gte lt lte``): ``matched`` from current operand values,
  never ``fired``.
- ``crosses_above`` / ``crosses_below``: ``fired`` iff a previous value exists
  in the same epoch and ``prev < level and cur >= level`` (mirrored below).
  First observation of an epoch initializes without firing. After evaluation,
  ``state["prev"]`` holds the current value.
- ``within``: level between ``lo`` and ``hi`` inclusive; ``fired`` on the
  outside->inside transition. Range bounds come from the right operand:
  ``value`` is ``lo`` and ``params["hi"]`` is ``hi``.
- ``rises_pct`` / ``falls_pct``: compared against ``state["baseline"]``,
  captured on the first observation of an epoch (with ``baseline_ts``) and
  never re-derived while present. ``fired`` on the below->at-or-above
  threshold transition.
- ``breaks_prev_high`` / ``breaks_prev_low``: the right operand ``value``
  holds the previous-day level supplied by runtime context. Fires when the
  current value crosses strictly across it; the ``prev_day_*_broken`` guard
  prevents refiring while beyond the level, and resets only when the value
  returns to the other side.
- Unknown operand value (missing field / absent literal / unsupported
  ``indicator`` kind) => ``matched=None`` (unknown propagates), ``fired=False``
  and the state is left completely unchanged.
- An observation whose ``epoch_id`` differs from the one recorded in state
  re-initializes epoch-scoped keys and never fires on that observation. The
  runtime normally passes a fresh state per epoch; this is a second line of
  defense.

State values are JSON-serializable (floats, bools, ISO-8601 strings) so they
can be persisted in evaluation checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from backend.alerts.types import Condition, Operand, Stage

__all__ = ["Observation", "PredicateResult", "evaluate_condition", "evaluate_stage"]

_OBSERVATION_FIELDS = ("ltp", "open", "high", "low", "close", "volume")

# Epoch-scoped state keys: cleared when an observation opens a new epoch.
_EPOCH_KEYS = (
    "prev",
    "baseline",
    "baseline_ts",
    "within",
    "pct_matched",
    "prev_day_high_seen",
    "prev_day_high_broken",
    "prev_day_low_seen",
    "prev_day_low_broken",
)

_LEVEL_OPS = ("gt", "gte", "lt", "lte")
_CROSS_OPS = ("crosses_above", "crosses_below")
_PCT_OPS = ("rises_pct", "falls_pct")
_BREAK_OPS = ("breaks_prev_high", "breaks_prev_low")


@dataclass(frozen=True)
class Observation:
    ts: datetime            # event time, UTC
    epoch_id: str
    ltp: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    final: bool = False     # True only for completed candles


@dataclass(frozen=True)
class PredicateResult:
    matched: Optional[bool]      # None = unknown (missing operand data)
    fired: bool                  # a transition/crossing fired on this observation
    evidence: dict               # {"ltp": 3002.5, "level": 3000, "prev_ltp": 2999.1}
    state: dict                  # new persistent state (prev, baseline, ...)


def _resolve_operand(operand: Operand, obs: Observation) -> Optional[float]:
    """Resolve an operand to a float, or None when unknown."""
    if operand is None:
        return None
    if operand.kind == "value":
        return operand.value
    if operand.kind == "field" and operand.name in _OBSERVATION_FIELDS:
        return getattr(obs, operand.name)
    return None  # indicator operands are unsupported in Phase 1 -> unknown


def _left_key(operand: Operand) -> str:
    if operand.kind == "field" and operand.name:
        return operand.name
    return "value"


def _unknown(state: dict) -> PredicateResult:
    return PredicateResult(matched=None, fired=False, evidence={}, state=dict(state))


def _iso_ts(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _start_epoch(state: dict, obs: Observation) -> dict:
    """Copy state, clearing epoch-scoped keys if obs opens a new epoch."""
    working = dict(state)
    prev_epoch = working.get("epoch_id")
    if prev_epoch is not None and prev_epoch != obs.epoch_id:
        for key in _EPOCH_KEYS:
            working.pop(key, None)
    working["epoch_id"] = obs.epoch_id
    return working


def _compare(op: str, cur: float, level: float) -> bool:
    if op == "gt":
        return cur > level
    if op == "gte":
        return cur >= level
    if op == "lt":
        return cur < level
    return cur <= level  # lte


def _evaluate_level_op(cond: Condition, obs: Observation, working: dict) -> PredicateResult:
    level = _resolve_operand(cond.right, obs)
    if level is None:
        return _unknown(working)
    cur = _resolve_operand(cond.left, obs)
    matched = _compare(cond.op, cur, level)
    evidence = {_left_key(cond.left): cur, "level": level}
    return PredicateResult(matched, False, evidence, _start_epoch(working, obs))


def _evaluate_cross_op(cond: Condition, obs: Observation, working: dict) -> PredicateResult:
    level = _resolve_operand(cond.right, obs)
    if level is None:
        return _unknown(working)
    cur = _resolve_operand(cond.left, obs)
    is_above = cond.op == "crosses_above"
    state = _start_epoch(working, obs)
    prev = state.get("prev")
    matched = cur >= level if is_above else cur <= level
    if prev is None:
        # First observation of the epoch: initialize without firing.
        state["prev"] = cur
        evidence = {_left_key(cond.left): cur, "level": level, "prev_ltp": None}
        return PredicateResult(matched, False, evidence, state)
    fired = (prev < level and cur >= level) if is_above else (prev > level and cur <= level)
    state["prev"] = cur
    evidence = {_left_key(cond.left): cur, "level": level, "prev_ltp": prev}
    return PredicateResult(matched, fired, evidence, state)


def _evaluate_within(cond: Condition, obs: Observation, working: dict) -> PredicateResult:
    right = cond.right
    lo = right.value if right.value is not None else right.params.get("lo")
    hi = right.params.get("hi")
    if lo is None or hi is None:
        return _unknown(working)
    cur = _resolve_operand(cond.left, obs)
    state = _start_epoch(working, obs)
    matched = lo <= cur <= hi
    was_inside = state.get("within")
    fired = was_inside is False and matched
    state["within"] = matched
    evidence = {_left_key(cond.left): cur, "lo": lo, "hi": hi}
    return PredicateResult(matched, fired, evidence, state)


def _evaluate_pct_op(cond: Condition, obs: Observation, working: dict) -> PredicateResult:
    threshold = _resolve_operand(cond.right, obs)
    if threshold is None:
        return _unknown(working)
    cur = _resolve_operand(cond.left, obs)
    state = _start_epoch(working, obs)
    baseline = state.get("baseline")
    if baseline is None:
        if cur == 0:
            return _unknown(working)  # cannot anchor a percentage at zero
        state["baseline"] = cur
        state["baseline_ts"] = _iso_ts(obs.ts)
        baseline = cur
    pct = (cur - baseline) / baseline * 100.0
    is_rise = cond.op == "rises_pct"
    matched = pct >= threshold if is_rise else pct <= -threshold
    fired = state.get("pct_matched") is False and matched
    state["pct_matched"] = matched
    evidence = {
        _left_key(cond.left): cur,
        "baseline": baseline,
        "baseline_ts": state.get("baseline_ts"),
        "pct": pct,
        "threshold": threshold,
    }
    return PredicateResult(matched, fired, evidence, state)


def _evaluate_break_op(cond: Condition, obs: Observation, working: dict) -> PredicateResult:
    level = _resolve_operand(cond.right, obs)
    if level is None:
        return _unknown(working)
    cur = _resolve_operand(cond.left, obs)
    is_high = cond.op == "breaks_prev_high"
    state = _start_epoch(working, obs)
    seen_key = "prev_day_high_seen" if is_high else "prev_day_low_seen"
    broken_key = "prev_day_high_broken" if is_high else "prev_day_low_broken"
    seen = state.get(seen_key, False)
    broken = state.get(broken_key, False)
    matched = cur > level if is_high else cur < level
    fired = bool(seen) and matched and not broken
    if matched:
        state[broken_key] = True
    elif (cur < level) if is_high else (cur > level):
        state[broken_key] = False  # back to the other side: re-arm the break
    state[seen_key] = True
    evidence = {_left_key(cond.left): cur, "level": level}
    return PredicateResult(matched, fired, evidence, state)


def evaluate_condition(cond: Condition, obs: Observation, state: dict) -> PredicateResult:
    """Evaluate one condition against an observation with explicit state."""
    if _resolve_operand(cond.left, obs) is None:
        return _unknown(state)
    op = cond.op
    if op in _LEVEL_OPS:
        return _evaluate_level_op(cond, obs, state)
    if op in _CROSS_OPS:
        return _evaluate_cross_op(cond, obs, state)
    if op == "within":
        return _evaluate_within(cond, obs, state)
    if op in _PCT_OPS:
        return _evaluate_pct_op(cond, obs, state)
    if op in _BREAK_OPS:
        return _evaluate_break_op(cond, obs, state)
    return _unknown(state)  # unknown operator -> unknown (compiler rejects earlier)


def evaluate_stage(stage: Stage, obs: Observation, state: dict) -> PredicateResult:
    """AND-combine the stage's conditions; unknown propagates and blocks firing."""
    working = dict(state)
    evidence: dict = {}
    results = []
    for cond in stage.conditions:
        result = evaluate_condition(cond, obs, working)
        results.append(result)
        working = result.state
        evidence.update(result.evidence)
    if any(r.matched is None for r in results):
        return PredicateResult(matched=None, fired=False, evidence=evidence, state=working)
    matched = all(r.matched is True for r in results)
    fired = matched and any(r.fired for r in results)
    return PredicateResult(matched, fired, evidence, working)
