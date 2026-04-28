from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from ..marketdata import candles_to_df
from .hybrid import adx, aroon, sar
from .momentum import cci, macd, rsi, stochastic, williamsr
from .oscillators import dpo, ppo
from .statistics import linreg
from .trend import ema, sma, supertrend, vwma, wma
from .utils import crossover, crossunder, falling, highest, lowest, rising
from .volatility import atr, bbands, keltner
from .volume import mfi, obv, vwap


IndicatorConfig = tuple[str, Mapping[str, Any]]


_INDICATOR_REGISTRY: dict[str, Callable[..., Any]] = {
    "adx": adx,
    "aroon": aroon,
    "atr": atr,
    "bbands": bbands,
    "cci": cci,
    "crossover": crossover,
    "crossunder": crossunder,
    "dpo": dpo,
    "ema": ema,
    "falling": falling,
    "highest": highest,
    "keltner": keltner,
    "linreg": linreg,
    "lowest": lowest,
    "macd": macd,
    "mfi": mfi,
    "obv": obv,
    "ppo": ppo,
    "rising": rising,
    "rsi": rsi,
    "sar": sar,
    "sma": sma,
    "stochastic": stochastic,
    "supertrend": supertrend,
    "vwap": vwap,
    "vwma": vwma,
    "williamsr": williamsr,
    "williams_r": williamsr,
    "wma": wma,
}


@dataclass(frozen=True)
class IndicatorValue:
    value: Any
    confirmed: bool
    ts: str
    ready: bool


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (float, np.floating)):
        return bool(np.isnan(value))
    return False


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and np.isnan(value):
        return None
    return value


def _extract_latest_value(result: Any) -> tuple[Any, bool]:
    if isinstance(result, pd.DataFrame):
        if result.empty:
            return {}, False
        row = result.iloc[-1]
        payload = {str(key): _normalize_scalar(value) for key, value in row.to_dict().items()}
        ready = bool(payload) and all(not _is_missing(value) for value in payload.values())
        return payload, ready

    if isinstance(result, pd.Series):
        if result.empty:
            return None, False
        value = _normalize_scalar(result.iloc[-1])
        return value, not _is_missing(value)

    array = np.asarray(result)
    if array.ndim == 0:
        value = _normalize_scalar(array.item())
        return value, not _is_missing(value)
    if array.size == 0:
        return None, False
    if array.ndim == 1:
        value = _normalize_scalar(array[-1])
        return value, not _is_missing(value)
    last_row = array[-1]
    payload = [_normalize_scalar(item) for item in np.asarray(last_row).tolist()]
    ready = bool(payload) and all(not _is_missing(value) for value in payload)
    return payload, ready


def _coerce_indicator_configs(indicators: Sequence[IndicatorConfig | tuple[str, dict[str, Any]]]) -> list[IndicatorConfig]:
    configs: list[IndicatorConfig] = []
    for item in indicators:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("indicators must be (name, params) tuples")
        name, params = item
        if not isinstance(name, str):
            raise TypeError("indicator names must be strings")
        if not isinstance(params, Mapping):
            raise TypeError("indicator params must be mappings")
        configs.append((name, dict(params)))
    return configs


def _normalize_ts(ts: Any) -> str:
    if ts is None:
        raise ValueError("candle payload requires ts")
    return str(ts)


def _coerce_is_complete(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "f", "no", "n"}:
            return False
        if normalized in {"1", "true", "t", "yes", "y"}:
            return True
    return bool(value)


def _frame_from_source(source: Any) -> tuple[pd.DataFrame, Optional[dict[str, Any]]]:
    frame = candles_to_df(source)
    if frame.empty:
        return frame.copy(), None

    provisional_payload: Optional[dict[str, Any]] = None
    if not bool(frame.iloc[-1]["is_complete"]):
        row = frame.iloc[-1]
        provisional_payload = {
            "ts": _normalize_ts(frame.index[-1].isoformat()),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "oi": row.get("oi"),
            "is_complete": False,
        }
        frame = frame.iloc[:-1]

    if not frame.empty:
        frame = frame.copy()
        frame["is_complete"] = True
    return frame, provisional_payload


class LiveIndicatorEngine:
    def __init__(self, indicators: Sequence[IndicatorConfig | tuple[str, dict[str, Any]]]):
        self.indicators: list[IndicatorConfig] = _coerce_indicator_configs(indicators)
        self._confirmed_frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi", "is_complete"])
        self.confirmed_values: dict[str, IndicatorValue] = {}
        self.provisional_values: dict[str, IndicatorValue] = {}
        self.last_confirmed_ts: Optional[str] = None
        self.last_ts: Optional[str] = None

    @classmethod
    def from_history(cls, df: Any, indicators: Sequence[IndicatorConfig | tuple[str, dict[str, Any]]]):
        engine = cls(indicators)
        engine.rebuild(df)
        return engine

    @property
    def values(self) -> dict[str, IndicatorValue]:
        return self.provisional_values or self.confirmed_values

    @property
    def ready(self) -> bool:
        return bool(self.values) and all(value.ready for value in self.values.values())

    @property
    def confirmed(self) -> bool:
        return not bool(self.provisional_values)

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "confirmed": self.confirmed,
            "last_confirmed_ts": self.last_confirmed_ts,
            "last_ts": self.last_ts,
        }

    def update_provisional(self, candle: Any) -> dict[str, IndicatorValue]:
        payload = self._normalize_candle(candle, is_complete=False)
        frame = self._with_candle(self._confirmed_frame, payload)
        self.provisional_values = self._evaluate(frame, confirmed=False, ts=payload["ts"])
        self.last_ts = payload["ts"]
        return self.provisional_values

    def finalize_candle(self, candle: Any) -> dict[str, IndicatorValue]:
        payload = self._normalize_candle(candle, is_complete=True)
        self._confirmed_frame = self._with_candle(self._confirmed_frame, payload)
        self._confirmed_frame["is_complete"] = True
        evaluation_frame = self._confirmed_frame.loc[:payload["ts"]]
        self.confirmed_values = self._evaluate(evaluation_frame, confirmed=True, ts=payload["ts"])
        self.provisional_values = {}
        self.last_confirmed_ts = payload["ts"]
        self.last_ts = payload["ts"]
        return self.confirmed_values

    def rebuild(self, history_df: Any, last_stream_candle: Any | None = None) -> dict[str, IndicatorValue]:
        confirmed_frame, provisional_payload = _frame_from_source(history_df)
        self._confirmed_frame = confirmed_frame
        self.last_confirmed_ts = self._frame_ts(confirmed_frame)
        if self.last_confirmed_ts is not None:
            self.confirmed_values = self._evaluate(self._confirmed_frame, confirmed=True, ts=self.last_confirmed_ts)
        else:
            self.confirmed_values = self._empty_values(confirmed=True, ts="")

        self.provisional_values = {}
        self.last_ts = self.last_confirmed_ts

        active_candle = last_stream_candle if last_stream_candle is not None else provisional_payload
        if active_candle is not None:
            if isinstance(active_candle, Mapping):
                is_complete = _coerce_is_complete(active_candle.get("is_complete", False))
            else:
                is_complete = _coerce_is_complete(getattr(active_candle, "is_complete", False))
            payload = self._normalize_candle(active_candle, is_complete=is_complete)
            if payload["is_complete"]:
                return self.finalize_candle(payload)
            return self.update_provisional(payload)

        return self.values

    def _evaluate(self, frame: pd.DataFrame, *, confirmed: bool, ts: str) -> dict[str, IndicatorValue]:
        values: dict[str, IndicatorValue] = {}
        for name, params in self.indicators:
            indicator_name = str(name)
            func = _INDICATOR_REGISTRY.get(indicator_name)
            if func is None:
                raise KeyError(f"unsupported live indicator '{indicator_name}'")

            result = self._call_indicator(func, frame, params)
            latest_value, ready = _extract_latest_value(result)
            values[indicator_name] = IndicatorValue(value=latest_value, confirmed=confirmed, ts=ts, ready=ready)
        return values

    def _call_indicator(self, func: Callable[..., Any], frame: pd.DataFrame, params: Mapping[str, Any]) -> Any:
        kwargs = dict(params)
        source = kwargs.pop("source", None)
        if source is None:
            return func(frame, **kwargs)

        if isinstance(source, str):
            if source not in frame.columns:
                raise KeyError(f"source column '{source}' not found")
            return func(frame[source], **kwargs)

        if isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
            series_args = []
            for column in source:
                if column not in frame.columns:
                    raise KeyError(f"source column '{column}' not found")
                series_args.append(frame[str(column)])
            return func(*series_args, **kwargs)

        raise TypeError("source must be a column name or iterable of column names")

    def _empty_values(self, *, confirmed: bool, ts: str) -> dict[str, IndicatorValue]:
        return {name: IndicatorValue(value=None, confirmed=confirmed, ts=ts, ready=False) for name, _ in self.indicators}

    def _normalize_candle(self, candle: Any, *, is_complete: bool) -> dict[str, Any]:
        frame = candles_to_df([candle])
        if frame.empty:
            raise ValueError("candle payload could not be normalized")
        row = frame.iloc[-1]
        return {
            "ts": _normalize_ts(frame.index[-1].isoformat()),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "oi": row.get("oi"),
            "is_complete": bool(is_complete),
        }

    def _with_candle(self, frame: pd.DataFrame, candle: Mapping[str, Any]) -> pd.DataFrame:
        candle_frame = candles_to_df([candle])
        if frame.empty:
            return candle_frame.copy()
        combined = pd.concat([frame, candle_frame])
        combined = combined[~combined.index.duplicated(keep="last")]
        return combined.sort_index()

    def _frame_ts(self, frame: pd.DataFrame) -> Optional[str]:
        if frame.empty:
            return None
        return _normalize_ts(frame.index[-1].isoformat())


__all__ = ["IndicatorValue", "LiveIndicatorEngine"]
