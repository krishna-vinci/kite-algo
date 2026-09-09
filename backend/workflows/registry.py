"""Phase 1 capability registry plus units/warmup metadata.

The compiler is the primary consumer: it validates documents against these
sets so that definitions needing unavailable capabilities fail validation
instead of being partially interpreted. Keep this module dependency-free.
"""

from __future__ import annotations

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
}

CLOCKS: Mapping[str, dict] = {
    "ltp": {"latency": "seconds"},
    "candle_close": {"latency": "one_bar"},
}

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

CAPABILITIES: Mapping[str, object] = {
    "operators": OPERATORS,
    "fields": FIELDS,
    "clocks": CLOCKS,
    "triggers": TRIGGERS,
    "timeframes": TIMEFRAMES,
}


def is_known_operator(name: object) -> bool:
    return isinstance(name, str) and name in OPERATORS


def is_known_field(name: object) -> bool:
    return isinstance(name, str) and name in FIELDS


def is_known_clock(name: object) -> bool:
    return isinstance(name, str) and name in CLOCKS


def is_known_trigger(name: object) -> bool:
    return isinstance(name, str) and name in TRIGGERS


def is_supported_timeframe(name: object) -> bool:
    return isinstance(name, str) and name in TIMEFRAMES


def is_namespaced_field(name: object) -> bool:
    """True for dotted names that belong to a future capability domain
    (e.g. ``fundamentals.latest_roce_pct``). These are reported as
    ``unknown_capability`` rather than ``unknown_field``."""
    return isinstance(name, str) and "." in name
