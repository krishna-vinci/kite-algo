from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from .models import IndicatorSpec


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_candle_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ordered_unique_closes(candles: Dict[str, Any] | None) -> List[float]:
    if not candles:
        return []

    ordered_rows: List[tuple[datetime, str, float]] = []
    for row in candles.get("history") or []:
        if not isinstance(row, dict):
            continue
        close_value = _safe_float(row.get("close"))
        raw_ts = row.get("ts") or row.get("timestamp")
        parsed_ts = _parse_candle_timestamp(raw_ts)
        if close_value is None or parsed_ts is None:
            continue
        ordered_rows.append((parsed_ts, parsed_ts.isoformat(), close_value))

    latest_closed = candles.get("latest_closed")
    if isinstance(latest_closed, dict):
        close_value = _safe_float(latest_closed.get("close"))
        raw_ts = latest_closed.get("ts") or latest_closed.get("timestamp")
        parsed_ts = _parse_candle_timestamp(raw_ts)
        if close_value is not None and parsed_ts is not None:
            ordered_rows.append((parsed_ts, parsed_ts.isoformat(), close_value))

    ordered_rows.sort(key=lambda item: item[0])
    deduped: Dict[str, float] = {}
    for _, ts, close_value in ordered_rows:
        deduped[ts] = close_value
    return list(deduped.values())


def compute_ema_series(values: Sequence[float], length: int) -> List[float]:
    if length < 1:
        raise ValueError("ema length must be >= 1")
    if not values:
        return []

    multiplier = 2.0 / (float(length) + 1.0)
    ema_values: List[float] = [float(values[0])]
    for price in values[1:]:
        previous = ema_values[-1]
        ema_values.append((float(price) - previous) * multiplier + previous)
    return ema_values


class BuiltInIndicatorReader:
    async def get_indicator(self, spec: IndicatorSpec, candles: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return self.compute_indicator(spec, candles)

    def compute_indicator(self, spec: IndicatorSpec, candles: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if spec.kind == "ema":
            return self._compute_ema(spec, candles)
        raise ValueError(f"unsupported indicator kind '{spec.kind}'")

    def _compute_ema(self, spec: IndicatorSpec, candles: Dict[str, Any] | None) -> Dict[str, Any]:
        raw_length = spec.params.get("length", 0)
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("ema length must be an integer") from exc
        if length < 1:
            raise ValueError("ema length must be >= 1")

        closes = _ordered_unique_closes(candles)
        ema_values = compute_ema_series(closes, length)
        latest = ema_values[-1] if ema_values else None
        previous = ema_values[-2] if len(ema_values) > 1 else None
        return {
            "kind": spec.kind,
            "params": {"length": length},
            "value": latest,
            "previous": previous,
            "ready": len(closes) >= length,
            "history_len": len(closes),
            "source": {
                "token": spec.token,
                "timeframe": spec.timeframe,
                "points": len(closes),
            },
        }


__all__ = ["BuiltInIndicatorReader", "compute_ema_series"]
