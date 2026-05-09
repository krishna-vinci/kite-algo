from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class OptionProtectionMetric:
    key: str
    value: float


@dataclass(frozen=True)
class OptionProtectionRuleSpec:
    key: str
    metric: str
    role: str
    operator: Literal["lte", "gte"]
    threshold: float


SUPPORTED_PROTECTION_METRIC_KEYS = {
    "index_ltp",
    "combined_premium",
    "combined_premium_change_pct",
    "strategy_mtm",
    "open_quantity",
}


class OptionProtectionRuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str | None = None
    metric: str
    role: str | None = None
    operator: str
    threshold: float
    action: Literal["exit"] = "exit"


class OptionProtectionConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[OptionProtectionRuleModel] = Field(default_factory=list)
    precedence: list[str] = Field(default_factory=list)


class OptionProtectionConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[OptionProtectionRuleModel] = Field(default_factory=list)
    precedence: list[str] = Field(default_factory=list)


class OptionProtectionMetricSnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index_ltp: float | None = None
    combined_premium: float | None = None
    combined_premium_change_pct: float | None = None
    strategy_mtm: float | None = None
    open_quantity: float | int | None = None


class OptionProtectionReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_snapshots: list[OptionProtectionMetricSnapshotModel] = Field(default_factory=list)
    protection: OptionProtectionConfigModel | None = None


class OptionProtectionStateResponse(BaseModel):
    strategy_run_id: str
    triggered: bool
    matched_rule: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    recommended_exit_orders: list[dict[str, Any]] = Field(default_factory=list)
