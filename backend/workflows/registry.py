"""Phase 1 capability registry plus units/warmup metadata.

The compiler is the primary consumer: it validates documents against these
sets so that definitions needing unavailable capabilities fail validation
instead of being partially interpreted. Keep this module dependency-free.
"""

from __future__ import annotations

import json
from typing import Mapping

# Phase 1 operators. "kind" describes evaluation semantics:
#   level    — matched from current values only
#   crossing — needs two valid observations in one continuous epoch
#   range    — entry into a band around a level
#   pct      — percentage move vs a captured baseline
#   break_   — break of a prior structural level
OPERATORS: Mapping[str, dict] = {
    "gt": {"kind": "level"},
    "gte": {"kind": "level"},
    "lt": {"kind": "level"},
    "lte": {"kind": "level"},
    "crosses_above": {"kind": "crossing"},
    "crosses_below": {"kind": "crossing"},
    "within": {"kind": "range", "params": ["tolerance"]},
    "rises_pct": {"kind": "pct"},
    "falls_pct": {"kind": "pct"},
    "breaks_prev_high": {"kind": "break_"},
    "breaks_prev_low": {"kind": "break_"},
}

# Phase 1 fields. "unit" documents expected units; "warmup_bars" is the
# completed-candle history the field needs (0 = none).
FIELDS: Mapping[str, dict] = {
    "ltp": {"unit": "price", "warmup_bars": 0},
    "open": {"unit": "price", "warmup_bars": 1},
    "high": {"unit": "price", "warmup_bars": 1},
    "low": {"unit": "price", "warmup_bars": 1},
    "close": {"unit": "price", "warmup_bars": 1},
    "volume": {"unit": "quantity", "warmup_bars": 1},
    # Context-resolved at evaluation time from the runtime-supplied session
    # context (previous trading day's levels), not from the observation bar.
    "prev_day_high": {"unit": "price", "warmup_bars": 0, "context_resolved": True},
    "prev_day_low": {"unit": "price", "warmup_bars": 0, "context_resolved": True},
    # Screener-only stored-data fields (Phase 3): computed by the screener
    # pipeline from the latest completed daily candles — % change vs the
    # previous close and traded turnover (close x volume). Rejected for
    # regular alert documents, which have no stored-scan data path.
    "change_pct": {"unit": "percent", "warmup_bars": 2, "screener_only": True},
    "turnover": {"unit": "currency_volume", "warmup_bars": 1, "screener_only": True},
}

CLOCKS: Mapping[str, dict] = {
    "ltp": {"latency": "seconds"},
    "candle_close": {"latency": "one_bar"},
}

# Authoring-time aliases: fundamentals_refresh normalizes to candle_close.
# There is no dedicated fundamentals stream; fundamentals conditions ride the
# declared candle clock and read the latest stored snapshot.
CLOCK_ALIASES: Mapping[str, str] = {
    "fundamentals_refresh": "candle_close",
}

CLOCK_METADATA: Mapping[str, dict] = {
    "ltp": {
        "latency": "seconds",
        "source": "market:ticks pub/sub (live LTP)",
    },
    "candle_close": {
        "latency": "one_bar",
        "source": "completed candles (redis completions + postgres continuity)",
        "note": (
            "fundamentals.* conditions evaluate on this clock too, reading "
            "the latest stored fundamentals snapshot (fundamentals_refresh "
            "is an alias, not a separate stream)"
        ),
    },
}

# registry field name -> public.fundamentals_features column
FUNDAMENTALS_COLUMN_MAP: Mapping[str, str] = {
    "fundamentals.quarterly_revenue_yoy_pct": "quarterly_revenue_yoy_pct",
    "fundamentals.latest_roce_pct": "latest_roce_pct",
    "fundamentals.pe_ratio": "stock_pe",
    "fundamentals.market_cap": "market_cap_cr",
}


def operand_uses_fundamentals(operand: object, depth: int = 0) -> bool:
    """True when an operand (descending arithmetic trees) references a
    ``fundamentals.*`` field."""
    if operand is None or depth > 6:
        return False
    if getattr(operand, "kind", None) == "field":
        name = getattr(operand, "name", None)
        return isinstance(name, str) and name.startswith("fundamentals.")
    if getattr(operand, "kind", None) == "indicator":
        from backend.workflows.compiler import _coerce_expression_arg

        for value in (getattr(operand, "params", None) or {}).values():
            if not isinstance(value, list):
                continue
            for arg in value:
                if operand_uses_fundamentals(_coerce_expression_arg(arg), depth + 1):
                    return True
    return False


def stage_uses_fundamentals(stage: object) -> bool:
    """True when any condition of the stage can reference fundamentals."""
    for group in (
        getattr(stage, "conditions", ()),
        getattr(stage, "any_conditions", ()),
        getattr(stage, "not_conditions", ()),
    ):
        for cond in group:
            if operand_uses_fundamentals(getattr(cond, "left", None)) or (
                operand_uses_fundamentals(getattr(cond, "right", None))
            ):
                return True
    return False

TRIGGERS: Mapping[str, dict] = {
    "once": {"max_fires": 1},
    "on_transition": {"max_fires": None},
    "once_per_session": {"max_fires": 1, "window": "session"},
    "reminder": {"max_fires": None, "requires": ["reminder_interval_s"]},
}

# Supported candle timeframes (Kite interval names).
TIMEFRAMES: frozenset[str] = frozenset(
    {"minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"}
)

# Calendar/session implementations wired by the Phase 1 runtime. New names
# must be added with a real provider; the compiler rejects unknown settings.
SESSION_EXCHANGES: Mapping[str, frozenset[str]] = {
    "nse_equity": frozenset({"NSE"}),
    "mcx_commodity": frozenset({"MCX"}),
    "currency": frozenset({"CDS", "BCD"}),
}
SESSIONS: frozenset[str] = frozenset(SESSION_EXCHANGES)

# ---------------------------------------------------------------------------
# Phase 2: shared feature functions (F8). "params" names required/allowed
# parameters with bounds; "inputs" names the bar fields the function needs.
# Numerics live in backend/alerts/features.py and match SDK fixtures.
# ---------------------------------------------------------------------------
FEATURE_FUNCTIONS: Mapping[str, dict] = {
    "sma": {"params": {"period": (2, 500)}, "inputs": ("close",)},
    "ema": {"params": {"period": (2, 500)}, "inputs": ("close",)},
    "wma": {"params": {"period": (2, 500)}, "inputs": ("close",)},
    "rsi": {"params": {"period": (2, 500)}, "inputs": ("close",)},
    "macd": {
        "params": {"fast": (2, 200), "slow": (3, 400), "signal": (2, 200)},
        "defaults": {"fast": 12, "slow": 26, "signal": 9},
        "inputs": ("close",),
        "outputs": ("macd", "signal", "hist"),
    },
    "atr": {"params": {"period": (2, 500)}, "inputs": ("high", "low", "close")},
    "bollinger": {
        "params": {"period": (2, 500), "num_std": (0.1, 10.0)},
        "defaults": {"num_std": 2.0},
        "inputs": ("close",),
        "outputs": ("mid", "upper", "lower"),
    },
    "supertrend": {
        "params": {"period": (2, 500), "multiplier": (0.5, 20.0)},
        "defaults": {"multiplier": 3.0},
        "inputs": ("high", "low", "close"),
        "outputs": ("value", "direction"),
    },
    "vwap_session": {"params": {}, "inputs": ("high", "low", "close", "volume")},
    "volume_sma": {"params": {"period": (2, 500)}, "inputs": ("volume",)},
    "volume_ratio": {"params": {"period": (2, 500), "offset": (0, 100)}, "defaults": {"offset": 0}, "inputs": ("volume",)},
}

# Bounded arithmetic operators for layered operands (spec F8): bounded depth
# and width; division by zero/unknown denominators is unknown, never an error.
ARITHMETIC_OPS: Mapping[str, dict] = {
    "add": {"arity": 2},
    "subtract": {"arity": 2},
    "multiply": {"arity": 2},
    "divide": {"arity": 2},
}
MAX_ARITHMETIC_DEPTH = 3
MAX_CONDITIONS_PER_GROUP = 32
MAX_FEATURE_STAGES = 8
MAX_INPUT_CHAIN_DEPTH = 8

# ---------------------------------------------------------------------------
# Phase 4 F10: advanced conditions. Stage types, bounded advanced-condition
# limits, cross-instrument pair computations and their freshness rules. The
# compiler validates against these maps and /capabilities is derived from the
# same source, so validation and discovery can never disagree.
# ---------------------------------------------------------------------------
STAGE_TYPES: Mapping[str, dict] = {
    "signal": {"requires_conditions": True},
    "filter": {"requires_conditions": True},
    "feature": {"requires_conditions": False},
    "breadth": {"requires_conditions": False, "requires_breadth": True},
}

# Seconds per timeframe — used for pair freshness bounds and (documented)
# bar/time equivalence. Values are the Kite interval names' nominal length.
TIMEFRAME_SECONDS: Mapping[str, int] = {
    "minute": 60,
    "3minute": 180,
    "5minute": 300,
    "10minute": 600,
    "15minute": 900,
    "30minute": 1800,
    "60minute": 3600,
    "day": 86400,
}

# Cross-instrument operand kinds (F10 relative strength / pair ratios).
# ``fields`` lists the bar fields each computation reads; both legs must read
# the SAME store on the same timeframe, so basis compatibility is structural.
PAIR_COMPUTATIONS: Mapping[str, dict] = {
    "pair_ratio": {
        "fields": ("close",),
        "requires": ("instrument", "reference"),
        "optional": ("field",),
        "formula": "close_A(bar) / close_B(bar) on the same completed bar",
        "unknown_reasons": (
            "pair_misaligned", "pair_stale", "pair_missing",
            "pair_zero_denominator",
        ),
    },
    "relative_strength": {
        "fields": ("close",),
        "requires": ("instrument", "reference", "lookback"),
        "optional": ("max_skew_bars",),
        "formula": (
            "((close_A(bar)/close_A(anchor)) - (close_B(bar)/close_B(anchor))) * 100 "
            "where anchor = bar - lookback bars on the stage timeframe; both legs "
            "must have a completed bar at BOTH endpoints"
        ),
        "unknown_reasons": (
            "pair_misaligned", "pair_lookback_misaligned", "pair_stale",
            "pair_missing", "pair_insufficient_history", "pair_zero_denominator",
        ),
    },
}

PAIR_LOOKBACK_BOUNDS = (1, 500)
PAIR_MAX_SKEW_BARS = 2  # 0 = exact head alignment (default)

# Advanced-condition bounds.
MAX_CONSECUTIVE_BARS = 50
MIN_CONSECUTIVE_BARS = 1
MAX_SEQUENCE_WITHIN_BARS = 500
MIN_SEQUENCE_WITHIN_BARS = 1
MAX_SEQUENCE_WITHIN_S = 30 * 24 * 3600
MAX_BREADTH_INSTRUMENTS = 1000
MIN_BREADTH_INSTRUMENTS = 2
MAX_BREADTH_WINDOW_S = 24 * 3600
MIN_BREADTH_WINDOW_S = 60

# Breadth modes. ``simultaneous`` is reserved in the schema but NOT
# implemented: "K symbols on the same bar" has its own alignment and
# partial-bar semantics and is deferred rather than half-specified.
BREADTH_MODES: Mapping[str, dict] = {
    "triggers_within": {"implemented": True},
    "simultaneous": {"implemented": False},
}

# Per-session cap bounds and the only supported reset boundary. Any value
# implying exchange market hours is rejected rather than silently applying
# NSE hours to feed-driven segments (MCX/currency have no session calendar).
MAX_PER_SESSION = 1000
MIN_PER_SESSION = 1
SESSION_CAP_RESETS: frozenset[str] = frozenset({"session"})

# Operators that may carry explicit hysteresis (level predicates on a
# CONSTANT threshold only — dynamic release operands are not implemented).
HYSTERESIS_OPS: frozenset[str] = frozenset({"gt", "gte", "lt", "lte"})

# Fundamentals observation fields (latest snapshot with acquisition metadata).
# Values are carried as context; replay never treats them as historical truth.
FUNDAMENTALS_FIELDS: frozenset[str] = frozenset(
    {
        "fundamentals.quarterly_revenue_yoy_pct",
        "fundamentals.latest_roce_pct",
        "fundamentals.pe_ratio",
        "fundamentals.market_cap",
    }
)

CAPABILITIES: Mapping[str, object] = {
    "operators": OPERATORS,
    "fields": FIELDS,
    "clocks": CLOCKS,
    "triggers": TRIGGERS,
    "timeframes": TIMEFRAMES,
    "sessions": SESSIONS,
    "features": FEATURE_FUNCTIONS,
    "arithmetic": ARITHMETIC_OPS,
    # Phase 4 F10
    "stage_types": STAGE_TYPES,
    "pairs": PAIR_COMPUTATIONS,
    "breadth_modes": BREADTH_MODES,
    "limits": {
        "max_consecutive_bars": MAX_CONSECUTIVE_BARS,
        "max_sequence_within_bars": MAX_SEQUENCE_WITHIN_BARS,
        "max_breadth_instruments": MAX_BREADTH_INSTRUMENTS,
        "max_breadth_window_s": MAX_BREADTH_WINDOW_S,
        "max_per_session": MAX_PER_SESSION,
        "max_arithmetic_depth": MAX_ARITHMETIC_DEPTH,
    },
}


def is_known_stage_type(name: object) -> bool:
    return isinstance(name, str) and name in STAGE_TYPES


def is_known_breadth_mode(name: object) -> bool:
    return isinstance(name, str) and name in BREADTH_MODES


def is_implemented_breadth_mode(name: object) -> bool:
    return bool(BREADTH_MODES.get(str(name), {}).get("implemented"))


def is_known_pair_computation(name: object) -> bool:
    return isinstance(name, str) and name in PAIR_COMPUTATIONS


def timeframe_seconds(name: object) -> int:
    """Nominal length of a supported timeframe in seconds (0 when unknown)."""
    return int(TIMEFRAME_SECONDS.get(str(name), 0))


def is_known_feature_function(name: object) -> bool:
    return isinstance(name, str) and name in FEATURE_FUNCTIONS


def is_known_arithmetic_op(name: object) -> bool:
    return isinstance(name, str) and name in ARITHMETIC_OPS


def feature_feature_id(function: str, params: Mapping[str, object], source_field: str) -> str:
    """Canonical feature identity (function + sorted params + source field).

    The calc version lives in backend.alerts.features.CALC_VERSION and the
    timeframe/instrument are engine-side scope, so they are not repeated in
    the per-document id.
    """
    canonical = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), default=str)
    return f"{function}:{canonical}:{source_field}"


def is_known_operator(name: object) -> bool:
    return isinstance(name, str) and name in OPERATORS


def is_known_field(name: object) -> bool:
    return isinstance(name, str) and name in FIELDS


def is_context_field(name: object) -> bool:
    return is_known_field(name) and bool(FIELDS[name].get("context_resolved"))


def is_known_clock(name: object) -> bool:
    return isinstance(name, str) and name in CLOCKS


def is_known_trigger(name: object) -> bool:
    return isinstance(name, str) and name in TRIGGERS


def is_supported_timeframe(name: object) -> bool:
    return isinstance(name, str) and name in TIMEFRAMES


def is_supported_session(name: object) -> bool:
    return isinstance(name, str) and name in SESSIONS


def session_accepts_exchange(session: object, exchange: object) -> bool:
    """Return whether a workflow session is valid for an instrument venue."""
    if not isinstance(session, str) or not isinstance(exchange, str):
        return False
    return exchange.upper() in SESSION_EXCHANGES.get(session, frozenset())


# External producer references (Phase 4 F10): ``external.<producer>.<field>``.
# The producer is registered at runtime through the signals API, so the
# compiler validates the SHAPE (a resolvable producer/field pair) rather than
# the existence of a row it cannot see; an absent, expired, late or revoked
# producer resolves to unknown at evaluation, never to a signal.
EXTERNAL_REFERENCE_PATTERN = r"^external\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_]+$"
EXTERNAL_NAMESPACE = "external"


def is_external_field(name: object) -> bool:
    """True for a well-formed ``external.<producer>.<field>`` reference."""
    import re

    return isinstance(name, str) and re.match(EXTERNAL_REFERENCE_PATTERN, name) is not None


def is_namespaced_field(name: object) -> bool:
    """True for dotted names that belong to a capability domain
    (e.g. ``fundamentals.latest_roce_pct``, ``external.myproducer.score``).
    Unknown domains are reported as ``unknown_capability`` rather than
    ``unknown_field``."""
    return isinstance(name, str) and "." in name
