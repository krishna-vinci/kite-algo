"""Shared feature engine: compute identical dependencies once per event (F8).

The engine keeps ONE bounded bar window per (instrument, timeframe) and one
declared-feature table per window. Every completed candle updates the window
(corrections replace the bar at the same timestamp) and each declared feature
is computed EXACTLY ONCE per event; dependent rules fan out from the cached
result. Rule-specific trigger state stays in the evaluation checkpoints —
the engine is stateless with respect to alerts.

Feature identity (must match backend.alerts.predicates.feature_operand_id):

    "<function>:<canonical params json>:<source field>[@<offset>]"

Values are None (= unknown) when history is insufficient or a denominator is
invalid — missing data never manufactures a signal (E-15/E-26).
"""

from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from backend.alerts import features as feature_functions
from backend.alerts.predicates import Observation

logger = logging.getLogger(__name__)

__all__ = ["FeatureSpec", "FeatureEngine", "DEFAULT_MAX_WINDOW"]

DEFAULT_MAX_WINDOW = 400  # bounded warmup/computation window per feature

# Multi-output functions and their default output key.
_MULTI_OUTPUT = {
    "macd": ("macd", "signal", "hist"),
    "bollinger": ("mid", "upper", "lower"),
    "supertrend": ("value", "direction"),
}


@dataclass(frozen=True)
class FeatureSpec:
    """One declared feature dependency."""

    function: str
    params: dict
    source: str = "close"
    offset: int = 0
    output: Optional[str] = None  # for multi-output functions

    def __hash__(self) -> int:  # params dict is canonicalized via the id
        return hash(self.feature_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FeatureSpec) and self.feature_id == other.feature_id

    @property
    def feature_id(self) -> str:
        base = feature_functions_feature_id(self.function, self.params, self.source)
        return f"{base}@{self.offset}" if self.offset else base


def feature_functions_feature_id(function: str, params: dict, source: str) -> str:
    import json

    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    return f"{function}:{canonical}:{source}"


def _bars_from_observations(observations: Sequence[Observation]) -> list[dict]:
    bars = []
    for obs in observations:
        bars.append(
            {
                "ts": obs.ts,
                "open": obs.open,
                "high": obs.high,
                "low": obs.low,
                "close": obs.close,
                "volume": obs.volume,
            }
        )
    return bars


class _Window:
    """Deduplicated bounded bar window (corrections replace by timestamp)."""

    def __init__(self, max_window: int) -> None:
        self._by_ts: "OrderedDict[Any, dict]" = OrderedDict()
        self._order: deque = deque()
        self._max = max_window

    def upsert(self, bar: dict) -> None:
        ts = bar["ts"]
        if ts in self._by_ts:
            self._by_ts[ts] = bar  # correction of a known bar
            return
        self._by_ts[ts] = bar
        self._order.append(ts)
        while len(self._order) > self._max:
            oldest = self._order.popleft()
            self._by_ts.pop(oldest, None)

    def bars(self) -> list[dict]:
        return [self._by_ts[ts] for ts in self._order]


class FeatureEngine:
    """Compute declared features once per market event, fan out to rules."""

    def __init__(self, *, max_window: int = DEFAULT_MAX_WINDOW) -> None:
        self._max_window = max(50, int(max_window))
        self._lock = threading.Lock()
        self._windows: Dict[Tuple[str, str], _Window] = {}
        self._specs: Dict[Tuple[str, str], "OrderedDict[str, FeatureSpec]"] = {}
        self._latest: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}

    # -- declaration -------------------------------------------------------

    def declare(self, instrument_key: str, timeframe: str, spec: FeatureSpec) -> None:
        """Register a feature dependency (idempotent per feature id)."""
        window_key = (instrument_key, timeframe)
        with self._lock:
            self._specs.setdefault(window_key, OrderedDict())
            self._specs[window_key][spec.feature_id] = spec

    def declared_timeframes(self, instrument_key: str) -> Tuple[str, ...]:
        with self._lock:
            return tuple(
                timeframe
                for (key, timeframe) in self._specs
                if key == instrument_key
            )

    def release(self, instrument_key: str, timeframe: Optional[str] = None) -> None:
        """Drop windows/specs/cached values for an instrument (or one of its
        timeframes). Called when a binding is retired/replaced so a removed
        instrument cannot leak memory or serve stale values."""
        with self._lock:
            doomed = [
                window_key
                for window_key in list(self._windows)
                if window_key[0] == instrument_key
                and (timeframe is None or window_key[1] == timeframe)
            ]
            for window_key in doomed:
                self._windows.pop(window_key, None)
                self._specs.pop(window_key, None)
                self._latest.pop(window_key, None)

    def has_declarations(self, instrument_key: str, timeframe: str) -> bool:
        with self._lock:
            return bool(self._specs.get((instrument_key, timeframe)))

    # -- warmup --------------------------------------------------------------

    def warm(self, instrument_key: str, timeframe: str, candle_history) -> int:
        """Preload the bounded window from durable completed candles.

        Silent by contract: warming never emits; it only gives the indicator
        windows enough completed history for the first live event.
        """
        window = self._windows.setdefault((instrument_key, timeframe), _Window(self._max_window))
        if len(window.bars()) >= self._max_window:
            return 0
        try:
            bars = candle_history.recent_bars(instrument_key, timeframe, self._max_window)
        except Exception:
            logger.warning(
                "feature warmup history read failed for %s/%s",
                instrument_key, timeframe, exc_info=True,
            )
            return 0
        for bar in bars:
            window.upsert(
                {
                    "ts": bar.ts,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            )
        count = len(bars)
        if count:
            logger.info(
                "feature window warmed: %s/%s loaded %d completed bars",
                instrument_key, timeframe, count,
            )
        # refresh cached snapshot from the warmed window
        self._recompute(instrument_key, timeframe)
        return count

    # -- live events ---------------------------------------------------------

    def on_bar(self, instrument_key: str, timeframe: str, obs: Observation) -> Dict[str, Optional[float]]:
        """Update the window with one completed bar and recompute once."""
        window = self._windows.setdefault((instrument_key, timeframe), _Window(self._max_window))
        window.upsert(
            {
                "ts": obs.ts,
                "open": obs.open,
                "high": obs.high,
                "low": obs.low,
                "close": obs.close,
                "volume": obs.volume,
            }
        )
        return self._recompute(instrument_key, timeframe)

    def snapshot(self, instrument_key: str, timeframe: str) -> Dict[str, Optional[float]]:
        """The most recent computed values (may be empty before warmup)."""
        with self._lock:
            return dict(self._latest.get((instrument_key, timeframe), {}))

    def field_snapshot(self, instrument_key: str, timeframe: str) -> Dict[str, Optional[float]]:
        """Latest COMPLETED bar fields as ``field:<name>`` keys.

        Used by layered filter stages on a different timeframe than the
        dispatch event: ``close > ema(200)`` on 1d resolves the daily close
        from the latest completed daily bar, never a forming candle (E-16).
        """
        with self._lock:
            window = self._windows.get((instrument_key, timeframe))
            if not window:
                return {}
            bars = window.bars()
        if not bars:
            return {}
        latest = bars[-1]
        out: Dict[str, Optional[float]] = {}
        for name in ("open", "high", "low", "close", "volume"):
            value = latest.get(name)
            out[f"field:{name}"] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
        return out

    # -- internals -------------------------------------------------------------

    def _recompute(self, instrument_key: str, timeframe: str) -> Dict[str, Optional[float]]:
        with self._lock:
            specs = self._specs.get((instrument_key, timeframe))
            window = self._windows.get((instrument_key, timeframe))
            if not specs or not window:
                return {}
            bars = window.bars()
            results: Dict[str, Optional[float]] = {}
            for feature_id, spec in specs.items():
                results[feature_id] = self._compute(spec, bars)
            self._latest[(instrument_key, timeframe)] = dict(results)
            return results

    @staticmethod
    def _compute(spec: FeatureSpec, bars: list[dict]) -> Optional[float]:
        """Compute one feature's scalar value at the requested offset."""
        closes = [bar.get("close") for bar in bars]
        volumes = [bar.get("volume") or 0.0 for bar in bars]
        fn: Optional[dict] = None
        params = dict(spec.params)
        output = spec.output
        try:
            if spec.function == "sma":
                series = feature_functions.sma(closes, int(params.get("period", 0)))
            elif spec.function == "ema":
                series = feature_functions.ema(closes, int(params.get("period", 0)))
            elif spec.function == "wma":
                series = feature_functions.wma(closes, int(params.get("period", 0)))
            elif spec.function == "rsi":
                series = feature_functions.rsi(closes, int(params.get("period", 14)))
            elif spec.function == "volume_sma":
                series = feature_functions.volume_sma(volumes, int(params.get("period", 0)))
            elif spec.function == "volume_ratio":
                series = feature_functions.volume_ratio(
                    volumes, int(params.get("period", 0)), int(params.get("offset", spec.offset))
                )
            elif spec.function in ("macd", "bollinger", "supertrend", "atr", "vwap_session"):
                fn = feature_functions.__dict__.get(spec.function)
            else:
                return None
            if fn is not None:
                if spec.function == "macd":
                    payload = feature_functions.macd(
                        closes,
                        fast=int(params.get("fast", 12)),
                        slow=int(params.get("slow", 26)),
                        signal=int(params.get("signal", 9)),
                    )
                    output = output or _MULTI_OUTPUT["macd"][0]
                elif spec.function == "bollinger":
                    payload = feature_functions.bollinger(
                        closes,
                        period=int(params.get("period", 20)),
                        num_std=float(params.get("num_std", 2.0)),
                    )
                    output = output or _MULTI_OUTPUT["bollinger"][0]
                elif spec.function == "supertrend":
                    payload = feature_functions.supertrend(
                        [
                            {
                                "high": bar.get("high"),
                                "low": bar.get("low"),
                                "close": bar.get("close"),
                            }
                            for bar in bars
                        ],
                        period=int(params.get("period", 10)),
                        multiplier=float(params.get("multiplier", 3.0)),
                    )
                    output = output or _MULTI_OUTPUT["supertrend"][0]
                elif spec.function == "atr":
                    payload = feature_functions.atr(
                        [
                            {
                                "high": bar.get("high"),
                                "low": bar.get("low"),
                                "close": bar.get("close"),
                            }
                            for bar in bars
                        ],
                        period=int(params.get("period", 14)),
                    )
                    output = None
                else:  # vwap_session
                    payload = feature_functions.vwap_session(bars, ["s"] * len(bars))
                    output = None
                series = payload.get(output) if isinstance(payload, dict) and output else payload
        except (TypeError, ValueError):
            return None

        if not isinstance(series, list):
            return None
        index = len(series) - 1 - max(0, int(spec.offset))
        if index < 0 or index >= len(series):
            return None
        value = series[index]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None
