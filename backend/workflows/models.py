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
    params: dict = field(default_factory=dict)  # indicator params / expression tree
    source: Optional[str] = None  # indicator input field override (e.g. "volume")
    offset: Optional[int] = None  # indicator bar offset (0 = latest completed bar)


@dataclass(frozen=True)
class Condition:
    left: Operand
    op: str
    right: Operand


@dataclass(frozen=True)
class Stage:
    id: str
    type: str  # Phase 1: "signal"; Phase 2 adds "feature" and "filter"
    clock: Clock
    timeframe: Optional[str]  # required iff clock == "candle_close"
    conditions: tuple[Condition, ...]  # AND-combined
    input: Optional[str] = None  # optional upstream stage id (layered stages)
    any_conditions: tuple[Condition, ...] = ()  # OR-combined with unknown (3VL)
    not_conditions: tuple[Condition, ...] = ()  # negated with unknown (3VL)
    function: Optional[str] = None  # feature stages: indicator function
    stage_params: dict = field(default_factory=dict)  # feature stage params
    source_field: Optional[str] = None  # feature stage input field override


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
class UniverseRef:
    """One membership source inside a universe expression (Phase 2 F7).

    ``kind`` is one of: ``universe`` (a saved universe by name), ``index``
    (an index constituent source list), ``watchlist`` (alias of universe).
    """

    kind: Literal["universe", "index", "watchlist"]
    name: str

    def to_ref_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name}


@dataclass(frozen=True)
class UniverseSpec:
    """Document-level universe membership expression (Phase 2 F7).

    ``refs`` are combined with UNION, deduplicated exchange-qualified;
    ``exclude`` removes public keys or saved-universe members. An empty spec
    is invalid and fails validation.
    """

    refs: tuple[UniverseRef, ...]
    exclude: tuple[UniverseRef, ...] = ()
    intersect: tuple[UniverseRef, ...] = ()
    deduplicate: bool = True

    def to_document_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "union": [ref.to_ref_dict() for ref in self.refs],
            "deduplicate": self.deduplicate,
        }
        if self.exclude:
            payload["exclude"] = [ref.to_ref_dict() for ref in self.exclude]
        if self.intersect:
            payload["intersect"] = [ref.to_ref_dict() for ref in self.intersect]
        return payload


@dataclass(frozen=True)
class DataPolicy:
    missing: str = "exclude_and_report"
    insufficient_history: str = "wait"
    require_closed_candles: bool = True


@dataclass(frozen=True)
class ScheduleSpec:
    """Screener schedule (Phase 3 F9).

    Buckets are computed in IST on the named calendar. Only ``nse_equity``
    (calendar-backed) is supported: MCX/currency eligibility is feed-driven
    and provides no session calendar, so such schedules are rejected at
    validation instead of silently applying NSE hours.
    """

    every: str  # e.g. "1d", "60m", "15m"
    calendar: str = "nse_equity"
    at: Optional[str] = None  # "HH:MM" IST (required for 1d) or "session_close"

    def to_document_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"every": self.every, "calendar": self.calendar}
        if self.at is not None:
            payload["at"] = self.at
        return payload


@dataclass(frozen=True)
class RankSpec:
    """Deterministic ranking over qualifying members.

    Ties break by stable instrument identity (``EXCHANGE:SYMBOL`` ascending)
    regardless of direction; null scores never rank.
    """

    by: Operand
    direction: Literal["desc", "asc"] = "desc"

    def to_document_dict(self) -> dict[str, Any]:
        return {"by": _operand_dict(self.by), "direction": self.direction}


@dataclass(frozen=True)
class AttachmentSpec:
    """Alert behavior attached to screener results (Phase 3 F9).

    ``trigger``: entry | exit | top_n | rank_delta. Hysteresis ranks
    (``entry_rank``/``exit_rank``) buffer boundary oscillation for ``top_n``
    (E-17); they are distinct from ``rank_delta`` thresholds. The first
    complete run is a silent baseline unless ``initial_match`` is set.
    """

    id: str
    trigger: Literal["entry", "exit", "top_n", "rank_delta"]
    channels: tuple[str, ...] = ()
    top_n: Optional[int] = None
    rank_delta: Optional[int] = None
    entry_rank: Optional[int] = None
    exit_rank: Optional[int] = None
    exit_after: Optional[int] = None  # consecutive absent complete runs before exit (default 1)
    initial_match: bool = False
    message: Optional[str] = None

    def to_document_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "trigger": self.trigger,
            "channels": list(self.channels),
            "initial_match": self.initial_match,
        }
        for key in ("top_n", "rank_delta", "entry_rank", "exit_rank", "exit_after"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.message is not None:
            payload["message"] = self.message
        return payload


@dataclass(frozen=True)
class ScreenerSpec:
    """Scheduled ranked scan over a universe (Phase 3 F9).

    A document with a ``screener`` block is a screener workflow: lifecycle,
    revisions and authorization are the workflow's; execution is the
    scheduler's; results persist as screener runs.
    """

    schedule: ScheduleSpec
    rank: Optional[RankSpec] = None
    top_n: Optional[int] = None
    attachments: tuple[AttachmentSpec, ...] = ()
    freshness_limit_s: int = 3 * 24 * 3600  # downstream dynamic-universe TTL

    def to_document_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schedule": self.schedule.to_document_dict(),
            "freshness_limit_s": self.freshness_limit_s,
        }
        if self.rank is not None:
            payload["rank"] = self.rank.to_document_dict()
        if self.top_n is not None:
            payload["top_n"] = self.top_n
        if self.attachments:
            payload["attachments"] = [
                att.to_document_dict() for att in self.attachments
            ]
        return payload


@dataclass(frozen=True)
class WorkflowDocument:
    version: int
    name: str
    instruments: tuple[InstrumentRef, ...]
    stages: tuple[Stage, ...]
    alerts: tuple[AlertSpec, ...]
    data_policy: DataPolicy = DataPolicy()
    session: str = "nse_equity"
    universe: Optional[UniverseSpec] = None
    screener: Optional[ScreenerSpec] = None

    def to_document_dict(self) -> dict[str, Any]:
        """Full-fidelity plain-dict form.

        The output is accepted by ``parser.parse_workflow_dict`` and
        reproduces a document equal to this one, so hashing it via
        ``compiler.canonical_json`` is stable across the round trip.
        """
        payload = {
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
                    "conditions": _conditions_dict(stage.conditions),
                    "any_conditions": _conditions_dict(stage.any_conditions),
                    "not_conditions": _conditions_dict(stage.not_conditions),
                    "function": stage.function,
                    "stage_params": dict(stage.stage_params),
                    "source_field": stage.source_field,
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
        if self.universe is not None:
            payload["universe"] = self.universe.to_document_dict()
        if self.screener is not None:
            payload["screener"] = self.screener.to_document_dict()
        return payload


def _conditions_dict(conditions: tuple[Condition, ...]) -> list[dict[str, Any]]:
    return [
        {
            "left": _operand_dict(cond.left),
            "op": cond.op,
            "right": _operand_dict(cond.right),
        }
        for cond in conditions
    ]


def _operand_dict(operand: Operand) -> dict[str, Any]:
    return {
        "kind": operand.kind,
        "name": operand.name,
        "value": operand.value,
        "params": dict(operand.params),
        "source": operand.source,
        "offset": operand.offset,
    }
