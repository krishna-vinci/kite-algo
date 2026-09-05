"""Typed investment data models for index constituents, market calendar,
and coherent account portfolio snapshots.

These models follow the 0.7.5 SDK conventions: frozen dataclasses built on
:class:`~kite_algo_worker.models.RawModelMixin`, so unknown additive server
fields are preserved in ``raw`` and round-trip through ``model_dump()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import RawModelMixin, _coerce_bool, _coerce_int, _coerce_optional_float, _coerce_optional_int


def _required_str(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


@dataclass(frozen=True)
class WorkerSourceEnvelope(RawModelMixin):
    """Base envelope for versioned worker investment documents."""

    schema_version: int
    source: str
    retrieved_at: str
    source_as_of: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _coerce_int(self.schema_version))
        if self.schema_version <= 0:
            raise ValueError("schema_version must be a positive integer")
        object.__setattr__(self, "source", _required_str(self.source, field_name="source"))
        object.__setattr__(self, "retrieved_at", _required_str(self.retrieved_at, field_name="retrieved_at"))
        object.__setattr__(self, "source_as_of", _optional_str(self.source_as_of))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerIndexMember(RawModelMixin):
    exchange: str
    tradingsymbol: str
    instrument_token: int
    company_name: Optional[str] = None
    series: Optional[str] = None
    isin_code: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _required_str(self.exchange, field_name="exchange"))
        object.__setattr__(self, "tradingsymbol", _required_str(self.tradingsymbol, field_name="tradingsymbol"))
        object.__setattr__(self, "instrument_token", _coerce_int(self.instrument_token))
        if self.instrument_token <= 0:
            raise ValueError("instrument_token must be a positive integer")
        object.__setattr__(self, "company_name", _optional_str(self.company_name))
        object.__setattr__(self, "series", _optional_str(self.series))
        object.__setattr__(self, "isin_code", _optional_str(self.isin_code))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerIndexConstituentsSnapshot(WorkerSourceEnvelope):
    """Official index constituent snapshot with preserved exchange identity."""

    source_list: str = ""
    complete: bool = False
    member_count: int = 0
    checksum: Optional[str] = None
    effective_date: Optional[str] = None
    source_url: Optional[str] = None
    members: List[WorkerIndexMember] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "source_list", _required_str(self.source_list, field_name="source_list"))
        object.__setattr__(self, "complete", _coerce_bool(self.complete))
        object.__setattr__(self, "member_count", _coerce_int(self.member_count))
        object.__setattr__(self, "checksum", _optional_str(self.checksum))
        object.__setattr__(self, "effective_date", _optional_str(self.effective_date))
        object.__setattr__(self, "source_url", _optional_str(self.source_url))
        members = [member if isinstance(member, WorkerIndexMember) else WorkerIndexMember.model_validate(member) for member in list(self.members or [])]
        object.__setattr__(self, "members", members)
        if not self.member_count:
            object.__setattr__(self, "member_count", len(members))


@dataclass(frozen=True)
class WorkerIndexConstituentStatus(WorkerSourceEnvelope):
    """Per-source-list refresh status; success for one list implies nothing
    about any other list."""

    source_list: str = ""
    complete: bool = False
    expected_member_count: Optional[int] = None
    actual_member_count: Optional[int] = None
    checksum: Optional[str] = None
    effective_date: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_failure: Optional[Dict[str, Any]] = None
    next_attempt_at: Optional[str] = None
    scheduler_state: Optional[str] = None
    retry_eligible: Optional[bool] = None
    fresh: Optional[bool] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "source_list", _required_str(self.source_list, field_name="source_list"))
        object.__setattr__(self, "complete", _coerce_bool(self.complete))
        object.__setattr__(self, "expected_member_count", _coerce_optional_int(self.expected_member_count))
        object.__setattr__(self, "actual_member_count", _coerce_optional_int(self.actual_member_count))
        object.__setattr__(self, "checksum", _optional_str(self.checksum))
        object.__setattr__(self, "effective_date", _optional_str(self.effective_date))
        object.__setattr__(self, "last_attempt_at", _optional_str(self.last_attempt_at))
        object.__setattr__(self, "last_success_at", _optional_str(self.last_success_at))
        object.__setattr__(self, "last_failure_at", _optional_str(self.last_failure_at))
        last_failure = self.last_failure
        if last_failure is not None and not isinstance(last_failure, dict):
            last_failure = {"reason": str(last_failure)}
        object.__setattr__(self, "last_failure", dict(last_failure) if last_failure is not None else None)
        object.__setattr__(self, "next_attempt_at", _optional_str(self.next_attempt_at))
        object.__setattr__(self, "scheduler_state", _optional_str(self.scheduler_state))
        object.__setattr__(self, "retry_eligible", None if self.retry_eligible is None else _coerce_bool(self.retry_eligible))
        object.__setattr__(self, "fresh", None if self.fresh is None else _coerce_bool(self.fresh))


@dataclass(frozen=True)
class WorkerCalendarSession(RawModelMixin):
    session_date: str
    session_type: str
    opens_at: Optional[str] = None
    closes_at: Optional[str] = None
    verified: bool = False
    source_reference: Optional[str] = None
    label: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_date", _required_str(self.session_date, field_name="session_date"))
        object.__setattr__(self, "session_type", _required_str(self.session_type, field_name="session_type"))
        object.__setattr__(self, "opens_at", _optional_str(self.opens_at))
        object.__setattr__(self, "closes_at", _optional_str(self.closes_at))
        object.__setattr__(self, "verified", _coerce_bool(self.verified))
        object.__setattr__(self, "source_reference", _optional_str(self.source_reference))
        object.__setattr__(self, "label", _optional_str(self.label))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerMarketCalendarSnapshot(WorkerSourceEnvelope):
    """Exchange trading-calendar sessions backed by an immutable calendar
    version. Missing coverage must fail closed upstream; it is never inferred
    from weekdays."""

    exchange: str = ""
    segment: str = ""
    timezone: str = "Asia/Kolkata"
    calendar_version: Optional[int] = None
    official_source_document_sha256: Optional[str] = None
    canonical_csv_sha256: Optional[str] = None
    complete: Optional[bool] = None
    amendment: Optional[Dict[str, Any]] = None
    sessions: List[WorkerCalendarSession] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "exchange", _required_str(self.exchange, field_name="exchange"))
        object.__setattr__(self, "segment", _required_str(self.segment, field_name="segment"))
        object.__setattr__(self, "timezone", _optional_str(self.timezone) or "Asia/Kolkata")
        object.__setattr__(self, "calendar_version", _coerce_optional_int(self.calendar_version))
        object.__setattr__(self, "official_source_document_sha256", _optional_str(self.official_source_document_sha256))
        object.__setattr__(self, "canonical_csv_sha256", _optional_str(self.canonical_csv_sha256))
        object.__setattr__(self, "complete", None if self.complete is None else _coerce_bool(self.complete))
        amendment = self.amendment
        object.__setattr__(self, "amendment", dict(amendment) if amendment is not None else None)
        sessions = [session if isinstance(session, WorkerCalendarSession) else WorkerCalendarSession.model_validate(session) for session in list(self.sessions or [])]
        object.__setattr__(self, "sessions", sessions)


@dataclass(frozen=True)
class WorkerMarketCalendarStatus(WorkerSourceEnvelope):
    """Durable calendar import/coverage health for one exchange segment."""

    exchange: str = ""
    segment: str = ""
    active_calendar_version: Optional[int] = None
    coverage_start: Optional[str] = None
    coverage_end: Optional[str] = None
    complete: bool = False
    expiry_warning: bool = False
    official_source: Optional[str] = None
    last_import_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_error: Optional[str] = None
    next_attempt_at: Optional[str] = None
    pending_import: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "exchange", _required_str(self.exchange, field_name="exchange"))
        object.__setattr__(self, "segment", _required_str(self.segment, field_name="segment"))
        object.__setattr__(self, "active_calendar_version", _coerce_optional_int(self.active_calendar_version))
        object.__setattr__(self, "coverage_start", _optional_str(self.coverage_start))
        object.__setattr__(self, "coverage_end", _optional_str(self.coverage_end))
        object.__setattr__(self, "complete", _coerce_bool(self.complete))
        object.__setattr__(self, "expiry_warning", _coerce_bool(self.expiry_warning))
        object.__setattr__(self, "official_source", _optional_str(self.official_source))
        object.__setattr__(self, "last_import_at", _optional_str(self.last_import_at))
        object.__setattr__(self, "last_attempt_at", _optional_str(self.last_attempt_at))
        object.__setattr__(self, "last_success_at", _optional_str(self.last_success_at))
        object.__setattr__(self, "last_failure_at", _optional_str(self.last_failure_at))
        object.__setattr__(self, "last_error", _optional_str(self.last_error))
        object.__setattr__(self, "next_attempt_at", _optional_str(self.next_attempt_at))
        pending_import = self.pending_import
        object.__setattr__(self, "pending_import", dict(pending_import) if pending_import is not None else None)


@dataclass(frozen=True)
class WorkerPortfolioHolding(RawModelMixin):
    exchange: str
    tradingsymbol: str
    instrument_token: int
    product: Optional[str] = None
    quantity: int = 0
    average_price: Optional[float] = None
    last_price: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _required_str(self.exchange, field_name="exchange"))
        object.__setattr__(self, "tradingsymbol", _required_str(self.tradingsymbol, field_name="tradingsymbol"))
        object.__setattr__(self, "instrument_token", _coerce_int(self.instrument_token))
        object.__setattr__(self, "product", _optional_str(self.product))
        object.__setattr__(self, "quantity", _coerce_int(self.quantity))
        object.__setattr__(self, "average_price", _coerce_optional_float(self.average_price))
        object.__setattr__(self, "last_price", _coerce_optional_float(self.last_price))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerPortfolioPosition(RawModelMixin):
    exchange: str
    tradingsymbol: str
    instrument_token: int
    product: Optional[str] = None
    quantity: int = 0
    average_price: Optional[float] = None
    last_price: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _required_str(self.exchange, field_name="exchange"))
        object.__setattr__(self, "tradingsymbol", _required_str(self.tradingsymbol, field_name="tradingsymbol"))
        object.__setattr__(self, "instrument_token", _coerce_int(self.instrument_token))
        object.__setattr__(self, "product", _optional_str(self.product))
        object.__setattr__(self, "quantity", _coerce_int(self.quantity))
        object.__setattr__(self, "average_price", _coerce_optional_float(self.average_price))
        object.__setattr__(self, "last_price", _coerce_optional_float(self.last_price))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class WorkerAccountPortfolioSnapshot(WorkerSourceEnvelope):
    """Coherent broker-account portfolio observation. This is evidence, not
    strategy ownership or durable P&L history."""

    account_scope: str = ""
    coherent: bool = False
    coherence_skew_ms: int = 0
    component_times: Dict[str, str] = field(default_factory=dict)
    funds: Dict[str, Any] = field(default_factory=dict)
    holdings: List[WorkerPortfolioHolding] = field(default_factory=list)
    net_positions: List[WorkerPortfolioPosition] = field(default_factory=list)
    day_positions: List[WorkerPortfolioPosition] = field(default_factory=list)
    profile_capabilities: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "account_scope", _required_str(self.account_scope, field_name="account_scope"))
        object.__setattr__(self, "coherent", _coerce_bool(self.coherent))
        object.__setattr__(self, "coherence_skew_ms", _coerce_int(self.coherence_skew_ms))
        object.__setattr__(self, "component_times", {str(key): str(value) for key, value in dict(self.component_times or {}).items()})
        object.__setattr__(self, "funds", dict(self.funds or {}))
        object.__setattr__(self, "holdings", [item if isinstance(item, WorkerPortfolioHolding) else WorkerPortfolioHolding.model_validate(item) for item in list(self.holdings or [])])
        object.__setattr__(self, "net_positions", [item if isinstance(item, WorkerPortfolioPosition) else WorkerPortfolioPosition.model_validate(item) for item in list(self.net_positions or [])])
        object.__setattr__(self, "day_positions", [item if isinstance(item, WorkerPortfolioPosition) else WorkerPortfolioPosition.model_validate(item) for item in list(self.day_positions or [])])
        object.__setattr__(self, "profile_capabilities", dict(self.profile_capabilities or {}))


__all__ = [
    "WorkerAccountPortfolioSnapshot",
    "WorkerCalendarSession",
    "WorkerIndexConstituentStatus",
    "WorkerIndexConstituentsSnapshot",
    "WorkerIndexMember",
    "WorkerMarketCalendarSnapshot",
    "WorkerMarketCalendarStatus",
    "WorkerPortfolioHolding",
    "WorkerPortfolioPosition",
    "WorkerSourceEnvelope",
]
