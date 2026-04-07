from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from algo_runtime.models import NoopAction, NotifyAction, Snapshot, StatePatchAction


class EmaMonitorConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    token: int
    timeframe: str
    fast_length: int
    slow_length: int
    signal_on_cross: bool = True
    notify_level: Literal["info", "warning"] = "info"

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        cleaned = str(value or "").strip().lower()
        if not cleaned:
            raise ValueError("timeframe is required")
        return cleaned

    @model_validator(mode="after")
    def validate_lengths(self) -> "EmaMonitorConfig":
        if self.fast_length < 1 or self.slow_length < 1:
            raise ValueError("ema lengths must be >= 1")
        if self.fast_length >= self.slow_length:
            raise ValueError("fast_length must be smaller than slow_length")
        return self


class ModularEmaMonitorAlgo:
    ALGO_TYPE = "ema_monitor"

    def __init__(self, instance, **kwargs):
        self.instance = instance
        self.config = EmaMonitorConfig.model_validate(instance.config)

    async def initialize(self, context: Any) -> None:
        return None

    async def evaluate(self, snapshot: Snapshot, state: Dict[str, Any]) -> List[Any]:
        fast_indicator = snapshot.indicators.get(self._indicator_key(self.config.fast_length), {})
        slow_indicator = snapshot.indicators.get(self._indicator_key(self.config.slow_length), {})
        if not fast_indicator.get("ready") or not slow_indicator.get("ready"):
            return [NoopAction(reason="indicators_not_ready")]

        fast_value = fast_indicator.get("value")
        slow_value = slow_indicator.get("value")
        if fast_value is None or slow_value is None:
            return [NoopAction(reason="missing_indicator_values")]

        regime = self._regime(float(fast_value), float(slow_value))
        latest_closed = self._latest_closed_candle(snapshot)
        close_price = latest_closed.get("close")
        previous_regime = state.get("regime")

        patch = {
            "regime": regime,
            "last_fast_ema": float(fast_value),
            "last_slow_ema": float(slow_value),
            "last_close": float(close_price) if close_price is not None else None,
            "last_evaluated_at": self._utcnow(),
        }
        if previous_regime == regime:
            return [StatePatchAction(patch=patch)]

        patch["last_signal"] = regime
        patch["last_signal_at"] = self._utcnow()
        if not self.config.signal_on_cross or previous_regime is None:
            return [StatePatchAction(patch=patch)]

        signal = "bullish_ema_cross" if regime == "bullish" else "bearish_ema_cross"
        return [
            NotifyAction(
                message=(
                    f"EMA monitor {self.instance.instance_id} detected {signal} on {self.config.token} "
                    f"{self.config.timeframe}: fast={float(fast_value):.2f}, slow={float(slow_value):.2f}"
                ),
                level=self.config.notify_level,
                metadata={
                    "instance_id": self.instance.instance_id,
                    "signal": signal,
                    "token": self.config.token,
                    "timeframe": self.config.timeframe,
                    "fast_length": self.config.fast_length,
                    "slow_length": self.config.slow_length,
                    "fast_value": float(fast_value),
                    "slow_value": float(slow_value),
                    "close_price": float(close_price) if close_price is not None else None,
                },
            ),
            StatePatchAction(patch=patch),
        ]

    def _indicator_key(self, length: int) -> str:
        return f"ema:{self.config.token}:{self.config.timeframe}:length={length}"

    def _regime(self, fast_value: float, slow_value: float) -> str:
        if fast_value > slow_value:
            return "bullish"
        if fast_value < slow_value:
            return "bearish"
        return "neutral"

    def _latest_closed_candle(self, snapshot: Snapshot) -> Dict[str, Any]:
        matching = []
        prefix = f"{self.config.token}:{self.config.timeframe}:"
        for key, payload in snapshot.candles.items():
            if not key.startswith(prefix) or not isinstance(payload, dict):
                continue
            try:
                lookback = int(key.split(":")[2])
            except (IndexError, ValueError):
                lookback = 0
            matching.append((lookback, payload))
        if not matching:
            return {}
        matching.sort(key=lambda item: item[0], reverse=True)
        return matching[0][1].get("latest_closed") or {}

    def _utcnow(self) -> str:
        return datetime.now(timezone.utc).isoformat()
