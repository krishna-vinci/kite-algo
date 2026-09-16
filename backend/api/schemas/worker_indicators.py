"""Request schemas for the worker indicator compute route.

Mirrors the MCP adapter's ``IndicatorRequest`` contract exactly so the
adapter can forward tool calls without reshaping.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_INDICATOR_BARS = 1_000

IndicatorName = Literal[
    "sma", "ema", "wma", "vwma", "supertrend", "rsi", "macd", "ppo", "dpo", "stochastic",
    "cci", "williams_r", "linreg", "atr", "bbands", "keltner", "adx", "aroon", "sar", "obv",
    "vwap", "mfi", "crossover", "crossunder", "highest", "lowest", "rising", "falling",
]


class WorkerIndicatorBar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: Optional[str] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    is_complete: bool = True


class WorkerIndicatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: IndicatorName
    bars: list[WorkerIndicatorBar] = Field(min_length=1, max_length=MAX_INDICATOR_BARS)
    period: int = Field(default=14, ge=1, le=500)
    fast_period: int = Field(default=12, ge=1, le=500)
    slow_period: int = Field(default=26, ge=1, le=500)
    signal_period: int = Field(default=9, ge=1, le=500)
    multiplier: float = Field(default=2.0, gt=0, le=10)
    include_forming: bool = False

    @model_validator(mode="after")
    def _periods(self) -> "WorkerIndicatorRequest":
        if self.fast_period >= self.slow_period and self.name in {"macd", "ppo"}:
            raise ValueError("fast_period must be less than slow_period")
        if not self.include_forming:
            self.bars = [bar for bar in self.bars if bar.is_complete]
        if not self.bars:
            raise ValueError("bars must contain at least one completed candle")
        return self
