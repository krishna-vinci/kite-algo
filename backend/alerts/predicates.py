"""Pure condition evaluation for alert rules: state in -> state out.

Semantics (spec v2 §4 F3, §5; plan "Shared contract"):

- Level ops (``gt gte lt lte``): ``matched`` from current operand values,
  never ``fired``.
- ``crosses_above`` / ``crosses_below``: ``fired`` iff a previous value exists
  in the same epoch and ``prev < level and cur >= level`` (mirrored below).
  First observation of an epoch initializes without firing. After evaluation,
  the condition's ``prev`` holds the current value.
- ``within``: level between ``lo`` and ``hi`` inclusive; ``fired`` on the
  outside->inside transition. Range bounds come from the right operand:
  ``value`` is ``lo`` and ``params["hi"]`` is ``hi``.
- ``rises_pct`` / ``falls_pct``: compared against the condition's stored
  ``baseline``, captured on the first observation of an epoch (with
  ``baseline_ts``) and never re-derived while present. ``fired`` on the
  below->at-or-above threshold transition.
- ``breaks_prev_high`` / ``breaks_prev_low``: the level comes from the right
  operand — either a ``value`` literal or a context-resolved previous-day
  field (``{kind: "field", name: "prev_day_high"|"prev_day_low"}``, resolved
  numerically from the ``context`` mapping). Fires when the current value
  crosses strictly across it; the ``prev_day_*_broken`` guard prevents
  refiring while beyond the level, and resets only when the value returns to
  the other side.
- Unknown operand value (missing field / absent literal / unsupported
  ``indicator`` kind / missing ``context`` entry) => ``matched=None``
  (unknown propagates), ``fired=False`` and the state is left completely
  unchanged.
- An observation whose ``epoch_id`` differs from the one recorded in state
  re-initializes epoch-scoped keys and never fires on that observation. The
  runtime normally passes a fresh state per epoch; this is a second line of
  defense.

State shape (pinned):

- Top level carries ``epoch_id`` plus a ``conds`` mapping keyed by the
  canonical condition key ``f"{op}:{left_key}:{right_key}"`` where
  ``left_key``/``right_key`` are ``field:<name>``, ``value:<num>`` or
  ``indicator:<name>:<params-json>``. Each condition reads and writes ONLY
  its own ``state["conds"][cond_key]`` sub-dict, so a multi-condition stage
  can never clobber another condition's ``prev``/``baseline``/guard state.
- Each sub-dict also carries the ``epoch_id`` it was last evaluated with:
  epoch-scoped keys are cleared per condition when that condition sees a
  new epoch, independent of evaluation order within a stage.
- ``evidence`` is partitioned the same way: ``evidence[cond_key]`` holds the
  values observed for that condition alone.

State values are JSON-serializable (floats, bools, ISO-8601 strings) so they
can be persisted in evaluation checkpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from backend.alerts.types import Condition, Operand, Stage
from backend.workflows import registry

__all__ = ["Observation", "PredicateResult", "evaluate_condition", "evaluate_stage"]

# Feature-stage ids referenced by operand kind "indicator" with name set to
# a stage id ("stage:" prefix disambiguates from inline indicator functions).
_STAGE_REF_PREFIX = "stage:"

_OBSERVATION_FIELDS = ("ltp", "open", "high", "low", "close", "volume")

# Previous-day levels resolved from the caller-supplied context mapping.
_CONTEXT_FIELDS = ("prev_day_high", "prev_day_low")

# Epoch-scoped state keys: cleared when an observation opens a new epoch.
# These live inside each condition's own sub-dict.
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
    evidence: dict               # {cond_key: {"ltp": 3002.5, "level": 3000, ...}}
    state: dict                  # new persistent state ({conds: {cond_key: {...}}})


def feature_operand_id(operand: Operand) -> Optional[str]:
    """Canonical feature key for an inline indicator operand.

    Matches the identity used by the feature engine: ``function:params_json:
    source`` plus an optional ``@offset`` suffix. Stage references (name
    starting with ``stage:``) resolve by that stage id directly.
    """
    if operand is None or operand.kind != "indicator":
        return None
    if operand.name and operand.name.startswith(_STAGE_REF_PREFIX):
        return operand.name
    if not operand.name:
        return None  # expression operands are evaluated structurally
    source = operand.source or "close"
    base = registry.feature_feature_id(operand.name, operand.params or {}, source)
    return f"{base}@{operand.offset}" if operand.offset else base


def _resolve_arithmetic(operand: Operand, resolve) -> Optional[float]:
    """Evaluate a bounded arithmetic operand with unknown propagation."""
    op_name = next(
        (k for k in (operand.params or {}) if k in registry.ARITHMETIC_OPS), None
    )
    if op_name is None:
        return None
    args = operand.params[op_name]
    if not isinstance(args, list) or len(args) != registry.ARITHMETIC_OPS[op_name]["arity"]:
        return None
    from backend.workflows.compiler import _coerce_expression_arg

    values = []
    for arg in args:
        child = _coerce_expression_arg(arg)
        value = resolve(child)
        if value is None:
            return None  # unknown propagates through arithmetic
        values.append(value)
    a, b = values
    if op_name == "add":
        return a + b
    if op_name == "subtract":
        return a - b
    if op_name == "multiply":
        return a * b
    # divide: zero/invalid denominator is unknown, never an error (E-26)
    if abs(b) < 1e-12:
        return None
    return a / b


def _operand_key(operand: Operand) -> str:
    """Canonical key fragment for one operand."""
    if operand is None:
        return "value:None"
    if operand.kind == "field":
        return f"field:{operand.name}"
    if operand.kind == "value":
        return f"value:{operand.value}"
    params_json = json.dumps(operand.params or {}, sort_keys=True, separators=(",", ":"))
    return f"indicator:{operand.name}:{params_json}"


def cond_key(cond: Condition) -> str:
    """Canonical identity of a condition: ``op:left_key:right_key``."""
    return f"{cond.op}:{_operand_key(cond.left)}:{_operand_key(cond.right)}"


def _resolve_operand(
    operand: Operand,
    obs: Observation,
    context: Optional[dict] = None,
    features: Optional[dict] = None,
) -> Optional[float]:
    """Resolve an operand to a float, or None when unknown."""
    if operand is None:
        return None
    if operand.kind == "value":
        return operand.value
    if operand.kind == "field":
        if operand.name in _OBSERVATION_FIELDS:
            value = getattr(obs, operand.name)
            if value is not None:
                return value
            # Layered snapshot fallback: a filter stage on an upstream
            # timeframe resolves its bar fields from the feature engine's
            # latest COMPLETED bar of that timeframe (never a forming candle).
            if features:
                snapshot_value = features.get(f"field:{operand.name}")
                if isinstance(snapshot_value, (int, float)) and not isinstance(snapshot_value, bool):
                    return float(snapshot_value)
            return None
        if operand.name in _CONTEXT_FIELDS and context:
            value = context.get(operand.name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        if isinstance(operand.name, str) and operand.name.startswith("fundamentals."):
            # Latest fundamentals snapshot, resolved from context; absent
            # data is unknown, never false.
            if context:
                value = context.get(operand.name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
            return None
        if features:
            # Computed-field fallback (screener stored-data fields such as
            # change_pct/turnover are delivered through the features map).
            value = features.get(f"field:{operand.name}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None  # unknown field / missing context entry
    # indicator operand: stage reference or inline feature / expression
    if operand.name and operand.name.startswith(_STAGE_REF_PREFIX):
        if features and operand.name in features:
            value = features[operand.name]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None
    if operand.name:
        feature_id = feature_operand_id(operand)
        if feature_id and features and feature_id in features:
            value = features[feature_id]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None  # feature value not available for this event
    return _resolve_arithmetic(
        operand,
        lambda child: _resolve_operand(child, obs, context, features),
    )


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


def _begin_observation(state: dict, key: str, obs: Observation) -> dict:
    """Copy the condition's sub-state, clearing epoch-scoped keys on epoch change.

    The epoch marker lives INSIDE the condition's own sub-dict, so the epoch
    change is detected per condition even when a stage chains one condition's
    output state into the next (which re-stamps the top-level epoch id).
    """
    conds = state.get("conds")
    raw = conds.get(key) if isinstance(conds, dict) else None
    sub = dict(raw) if isinstance(raw, dict) else {}
    prev_epoch = sub.get("epoch_id")
    if prev_epoch is not None and prev_epoch != obs.epoch_id:
        for epoch_key in _EPOCH_KEYS:
            sub.pop(epoch_key, None)
    return sub


def _commit_substate(state: dict, key: str, sub: dict, obs: Observation) -> dict:
    """Return a new top-level state with this condition's sub-dict written."""
    sub["epoch_id"] = obs.epoch_id
    out = dict(state)
    conds = dict(out.get("conds") or {})
    conds[key] = sub
    out["conds"] = conds
    out["epoch_id"] = obs.epoch_id
    return out


def _compare(op: str, cur: float, level: float) -> bool:
    if op == "gt":
        return cur > level
    if op == "gte":
        return cur >= level
    if op == "lt":
        return cur < level
    return cur <= level  # lte


def _evaluate_level_op(cond, obs, sub, context, features):
    level = _resolve_operand(cond.right, obs, context, features)
    if level is None:
        return None
    cur = _resolve_operand(cond.left, obs, context, features)
    matched = _compare(cond.op, cur, level)
    evidence = {_left_key(cond.left): cur, "level": level}
    return matched, False, evidence, sub


def _evaluate_cross_op(cond, obs, sub, context, features):
    level = _resolve_operand(cond.right, obs, context, features)
    if level is None:
        return None
    cur = _resolve_operand(cond.left, obs, context, features)
    is_above = cond.op == "crosses_above"
    prev = sub.get("prev")
    matched = cur >= level if is_above else cur <= level
    if prev is None:
        # First observation of the epoch: initialize without firing.
        sub["prev"] = cur
        evidence = {_left_key(cond.left): cur, "level": level, "prev_ltp": None}
        return matched, False, evidence, sub
    fired = (prev < level and cur >= level) if is_above else (prev > level and cur <= level)
    sub["prev"] = cur
    evidence = {_left_key(cond.left): cur, "level": level, "prev_ltp": prev}
    return matched, fired, evidence, sub


def _evaluate_within(cond, obs, sub, context, features):
    right = cond.right
    lo = right.value if right.value is not None else right.params.get("lo")
    hi = right.params.get("hi")
    if lo is None or hi is None:
        return None
    cur = _resolve_operand(cond.left, obs, context, features)
    matched = lo <= cur <= hi
    was_inside = sub.get("within")
    fired = was_inside is False and matched
    sub["within"] = matched
    evidence = {_left_key(cond.left): cur, "lo": lo, "hi": hi}
    return matched, fired, evidence, sub


def _evaluate_pct_op(cond, obs, sub, context, features):
    threshold = _resolve_operand(cond.right, obs, context, features)
    if threshold is None:
        return None
    cur = _resolve_operand(cond.left, obs, context, features)
    baseline = sub.get("baseline")
    if baseline is None:
        if cur == 0:
            return None  # cannot anchor a percentage at zero
        sub["baseline"] = cur
        sub["baseline_ts"] = _iso_ts(obs.ts)
        baseline = cur
    pct = (cur - baseline) / baseline * 100.0
    is_rise = cond.op == "rises_pct"
    matched = pct >= threshold if is_rise else pct <= -threshold
    fired = sub.get("pct_matched") is False and matched
    sub["pct_matched"] = matched
    evidence = {
        _left_key(cond.left): cur,
        "baseline": baseline,
        "baseline_ts": sub.get("baseline_ts"),
        "pct": pct,
        "threshold": threshold,
    }
    return matched, fired, evidence, sub


def _evaluate_break_op(cond, obs, sub, context, features):
    level = _resolve_operand(cond.right, obs, context, features)
    if level is None:
        return None
    cur = _resolve_operand(cond.left, obs, context, features)
    is_high = cond.op == "breaks_prev_high"
    seen_key = "prev_day_high_seen" if is_high else "prev_day_low_seen"
    broken_key = "prev_day_high_broken" if is_high else "prev_day_low_broken"
    seen = sub.get(seen_key, False)
    broken = sub.get(broken_key, False)
    matched = cur > level if is_high else cur < level
    fired = bool(seen) and matched and not broken
    if matched:
        sub[broken_key] = True
    elif (cur < level) if is_high else (cur > level):
        sub[broken_key] = False  # back to the other side: re-arm the break
    sub[seen_key] = True
    evidence = {_left_key(cond.left): cur, "level": level}
    return matched, fired, evidence, sub


_OP_EVALUATORS = {
    "level": _evaluate_level_op,
    "cross": _evaluate_cross_op,
    "within": _evaluate_within,
    "pct": _evaluate_pct_op,
    "break": _evaluate_break_op,
}


def _op_family(op: str) -> Optional[str]:
    if op in _LEVEL_OPS:
        return "level"
    if op in _CROSS_OPS:
        return "cross"
    if op == "within":
        return "within"
    if op in _PCT_OPS:
        return "pct"
    if op in _BREAK_OPS:
        return "break"
    return None


def evaluate_condition(
    cond: Condition,
    obs: Observation,
    state: dict,
    context: Optional[dict] = None,
    features: Optional[dict] = None,
) -> PredicateResult:
    """Evaluate one condition against an observation with explicit state.

    The condition reads and writes ONLY ``state["conds"][cond_key]``; the
    rest of the state is passed through untouched.
    """
    if _resolve_operand(cond.left, obs, context, features) is None:
        return _unknown(state)
    key = cond_key(cond)
    family = _op_family(cond.op)
    evaluator = _OP_EVALUATORS.get(family) if family else None
    if evaluator is None:
        return _unknown(state)  # unknown operator -> unknown (compiler rejects earlier)
    sub = _begin_observation(state, key, obs)
    outcome = evaluator(cond, obs, sub, context, features)
    if outcome is None:
        return _unknown(state)  # unknown operand: state completely unchanged
    matched, fired, cond_evidence, new_sub = outcome
    return PredicateResult(
        matched,
        fired,
        {key: cond_evidence},
        _commit_substate(state, key, new_sub, obs),
    )


def _combine_all(matched_list):
    """Three-valued AND: False dominates, then unknown, else True."""
    if any(m is False for m in matched_list):
        return False
    if any(m is None for m in matched_list):
        return None
    return all(m is True for m in matched_list)


def _combine_any(matched_list):
    """Three-valued OR: True dominates, then unknown, else False."""
    if any(m is True for m in matched_list):
        return True
    if any(m is None for m in matched_list):
        return None
    return False


def _combine_not(matched_list):
    """Three-valued NOT over a single-condition group."""
    if not matched_list:
        return True
    m = matched_list[0]
    if m is None:
        return None
    return not m


def evaluate_stage(
    stage: Stage,
    obs: Observation,
    state: dict,
    context: Optional[dict] = None,
    features: Optional[dict] = None,
) -> PredicateResult:
    """Combine the stage's condition groups with three-valued logic.

    ``all`` groups AND, ``any`` groups OR, ``not`` groups negate; unknown
    propagates (unknown AND true = unknown, NOT unknown = unknown) and a rule
    with any unknown group result never fires. Every condition is partitioned
    by its canonical key, so evaluating one condition can never leak its
    updated state into another condition's ``prev``/``baseline``/guard keys.
    """
    working = dict(state)
    evidence: dict = {}
    group_matched = []
    any_fired = False

    def _run(group):
        nonlocal working, any_fired
        results = []
        for cond in group:
            result = evaluate_condition(cond, obs, working, context, features)
            results.append(result)
            working = result.state
            evidence.update(result.evidence)  # keys are cond keys: no clobbering
        matched_list = [r.matched for r in results]
        fired_here = any(r.fired for r in results if r.matched is not None)
        any_fired = any_fired or fired_here
        return matched_list

    group_matched.append(_combine_all(_run(stage.conditions)))
    if stage.any_conditions:
        group_matched.append(_combine_any(_run(stage.any_conditions)))
    if stage.not_conditions:
        group_matched.append(_combine_not(_run(stage.not_conditions)))

    matched = _combine_all(group_matched)
    if matched is None:
        return PredicateResult(matched=None, fired=False, evidence=evidence, state=working)
    fired = bool(matched) and any_fired
    return PredicateResult(matched, fired, evidence, working)
