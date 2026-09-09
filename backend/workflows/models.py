"""Workflow document model — the only definition of the document types.

This is the shared contract from the Alerts Platform Phase 1 plan
(docs/superpowers/plans/2026-09-08-alerts-platform-phase1-plan.md).
Do not rename or reshape these dataclasses: parser, compiler, alerts and
API layers all code against them. Keep this module dependency-free
(stdlib only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Clock = Literal["ltp", "candle_close"]
Trigger = Literal["once", "on_transition", "once_per_session", "reminder"]


@dataclass(frozen=True)
class InstrumentRef:
    symbol: str
    exchange: str

    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"


@dataclass(frozen=True)
class Operand:
    kind: Literal["field", "value", "indicator"]
    name: Optional[str] = None  # field name or indicator function
    value: Optional[float] = None
    params: dict = field(default_factory=dict)  # indicator params


@dataclass(frozen=True)
class Condition:
    left: Operand
    op: str
    right: Operand


@dataclass(frozen=True)
class Stage:
    id: str
    type: str  # Phase 1: "signal"
    clock: Clock
    timeframe: Optional[str]  # required iff clock == "candle_close"
    conditions: tuple[Condition, ...]  # AND-combined
    input: Optional[str] = None  # optional upstream stage id (reserved)


@dataclass(frozen=True)
class AlertSpec:
    id: str
    source: str  # stage id
    trigger: Trigger = "on_transition"
    reminder_interval_s: Optional[int] = None
    cooldown_s: Optional[int] = None
    rearm_level: Optional[float] = None  # from rearm_above/rearm_below
    rearm_direction: Optional[Literal["above", "below"]] = None
    notify_if_already_true: bool = False
    expires_at: Optional[str] = None  # ISO-8601 UTC
    channels: tuple[str, ...] = ()
    message: Optional[str] = None


@dataclass(frozen=True)
class DataPolicy:
    missing: str = "exclude_and_report"
    insufficient_history: str = "wait"
    require_closed_candles: bool = True


@dataclass(frozen=True)
class WorkflowDocument:
    version: int
    name: str
    instruments: tuple[InstrumentRef, ...]
    stages: tuple[Stage, ...]
    alerts: tuple[AlertSpec, ...]
    data_policy: DataPolicy = DataPolicy()
    session: str = "nse_equity"

    def to_document_dict(self) -> dict[str, Any]:
        """Full-fidelity plain-dict form.

        The output is accepted by ``parser.parse_workflow_dict`` and
        reproduces a document equal to this one, so hashing it via
        ``compiler.canonical_json`` is stable across the round trip.
        """
        return {
            "version": self.version,
            "name": self.name,
            "session": self.session,
            "instruments": [
                {"symbol": inst.symbol, "exchange": inst.exchange}
                for inst in self.instruments
            ],
            "stages": [
                {
                    "id": stage.id,
                    "type": stage.type,
                    "clock": stage.clock,
                    "timeframe": stage.timeframe,
                    "input": stage.input,
                    "conditions": [
                        {
                            "left": _operand_dict(cond.left),
                            "op": cond.op,
                            "right": _operand_dict(cond.right),
                        }
                        for cond in stage.conditions
                    ],
                }
                for stage in self.stages
            ],
            "alerts": [
                {
                    "id": alert.id,
                    "source": alert.source,
                    "trigger": alert.trigger,
                    "reminder_interval_s": alert.reminder_interval_s,
                    "cooldown_s": alert.cooldown_s,
                    "rearm_level": alert.rearm_level,
                    "rearm_direction": alert.rearm_direction,
                    "notify_if_already_true": alert.notify_if_already_true,
                    "expires_at": alert.expires_at,
                    "channels": list(alert.channels),
                    "message": alert.message,
                }
                for alert in self.alerts
            ],
            "data_policy": {
                "missing": self.data_policy.missing,
                "insufficient_history": self.data_policy.insufficient_history,
                "require_closed_candles": self.data_policy.require_closed_candles,
            },
        }


def _operand_dict(operand: Operand) -> dict[str, Any]:
    return {
        "kind": operand.kind,
        "name": operand.name,
        "value": operand.value,
        "params": dict(operand.params),
    }
