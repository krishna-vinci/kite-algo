from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from .models import WorkerCandle, WorkerHistoricalCandles


OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "oi", "is_complete"]

__all__ = ["OHLCV_COLUMNS", "OhlcvArrays", "candles_to_df", "ohlcv_arrays"]


@dataclass(frozen=True)
class OhlcvArrays:
    index: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    oi: Optional[np.ndarray]
    is_complete: np.ndarray


def _is_candle_mapping(value: Any) -> bool:
    return isinstance(value, Mapping) and any(key in value for key in ("ts", "timestamp", "time"))


def _coerce_is_complete(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "f", "no", "n"}:
            return False
        if normalized in {"1", "true", "t", "yes", "y"}:
            return True
    return bool(value)


def _coerce_rows(source: Any) -> list[dict[str, Any]]:
    if source is None:
        return []

    if isinstance(source, pd.DataFrame):
        frame = source.copy()
        if "ts" not in frame.columns:
            frame = frame.reset_index()
            if "ts" not in frame.columns:
                first_column = frame.columns[0]
                frame = frame.rename(columns={first_column: "ts"})
        return [_normalize_candle_row(item) for item in frame.to_dict(orient="records")]

    if isinstance(source, WorkerHistoricalCandles):
        rows = []
        rows.extend(source.candles)
        if source.current is not None:
            rows.append(source.current)
        return [_normalize_candle_row(item) for item in rows]

    if isinstance(source, WorkerCandle):
        return [_normalize_candle_row(source)]

    if isinstance(source, Mapping):
        if "candles" in source or "current" in source:
            rows: list[Any] = []
            rows.extend(list(source.get("candles") or []))
            current = source.get("current")
            if current is not None:
                rows.append(current)
            return [_normalize_candle_row(item) for item in rows]
        if _is_candle_mapping(source):
            return [_normalize_candle_row(source)]

    if isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
        items = list(source)
        if not items:
            return []
        if all(_is_candle_mapping(item) or isinstance(item, WorkerCandle) for item in items):
            return [_normalize_candle_row(item) for item in items]
        return [_normalize_candle_row(item) for item in items]

    return [_normalize_candle_row(source)]


def _normalize_candle_row(value: Any) -> dict[str, Any]:
    if isinstance(value, WorkerCandle):
        payload = value.model_dump(exclude_none=False)
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = dict(getattr(value, "__dict__", {}) or {})

    ts = payload.get("ts", payload.get("timestamp", payload.get("time")))
    if ts is None:
        raise ValueError("candle payload requires a timestamp field")

    return {
        "ts": ts,
        "open": payload.get("open"),
        "high": payload.get("high"),
        "low": payload.get("low"),
        "close": payload.get("close"),
        "volume": payload.get("volume"),
        "oi": payload.get("oi"),
        "is_complete": _coerce_is_complete(payload.get("is_complete")),
    }


def candles_to_df(source: Any) -> pd.DataFrame:
    rows = _coerce_rows(source)
    if not rows:
        frame = pd.DataFrame(columns=OHLCV_COLUMNS)
        frame.index = pd.DatetimeIndex([], name="ts")
        return frame

    frame = pd.DataFrame(rows)
    for column in OHLCV_COLUMNS:
        if column not in frame.columns:
            frame[column] = np.nan if column != "is_complete" else True

    frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
    frame = frame.dropna(subset=["ts"])
    if frame.empty:
        empty = pd.DataFrame(columns=OHLCV_COLUMNS)
        empty.index = pd.DatetimeIndex([], name="ts")
        return empty

    frame = frame.sort_values("ts", kind="mergesort")
    frame = frame.drop_duplicates(subset=["ts"], keep="last")
    frame = frame.set_index("ts")
    frame = frame.reindex(columns=OHLCV_COLUMNS)
    frame = frame.sort_index()
    return frame


def ohlcv_arrays(source: Any) -> OhlcvArrays:
    frame = candles_to_df(source)

    index = frame.index.to_numpy(copy=False)
    open_ = frame["open"].to_numpy(copy=False)
    high = frame["high"].to_numpy(copy=False)
    low = frame["low"].to_numpy(copy=False)
    close = frame["close"].to_numpy(copy=False)
    volume = frame["volume"].to_numpy(copy=False)
    is_complete = frame["is_complete"].fillna(True).astype(bool).to_numpy(copy=False)

    oi: Optional[np.ndarray]
    if "oi" in frame.columns and not frame["oi"].isna().all():
        oi = frame["oi"].to_numpy(copy=False)
    else:
        oi = None

    return OhlcvArrays(
        index=index,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        oi=oi,
        is_complete=is_complete,
    )
