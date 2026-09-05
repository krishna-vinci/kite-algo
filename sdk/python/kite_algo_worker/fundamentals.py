"""Typed fundamentals snapshot, status, statements, and refresh contracts.

Follows the 0.7.5/0.7.6 SDK conventions: frozen dataclasses built on
:class:`~kite_algo_worker.models.RawModelMixin`, so unknown additive server
fields are preserved in ``raw`` and round-trip through ``model_dump()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import RawModelMixin, _coerce_int, _coerce_optional_float


def _required_str(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


@dataclass(frozen=True)
class FundamentalsEnvelope(RawModelMixin):
    """Base envelope for versioned fundamentals read documents."""

    schema_version: int
    source: str
    retrieved_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _coerce_int(self.schema_version))
        if self.schema_version <= 0:
            raise ValueError("schema_version must be a positive integer")
        object.__setattr__(self, "source", _required_str(self.source, field_name="source"))
        object.__setattr__(self, "retrieved_at", _optional_str(self.retrieved_at))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class FundamentalFeatureRow(RawModelMixin):
    """One stored derived-feature row for a single (symbol, statement_scope)."""

    symbol: str
    statement_scope: str = "consolidated"
    company_name: Optional[str] = None
    market_cap_cr: Optional[float] = None
    current_price: Optional[float] = None
    stock_pe: Optional[float] = None
    book_value: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    latest_quarter_revenue: Optional[float] = None
    latest_quarter_net_profit: Optional[float] = None
    latest_quarter_eps: Optional[float] = None
    ttm_revenue: Optional[float] = None
    ttm_net_profit: Optional[float] = None
    quarterly_revenue_yoy_pct: Optional[float] = None
    quarterly_profit_yoy_pct: Optional[float] = None
    latest_roce_pct: Optional[float] = None
    latest_roe_pct: Optional[float] = None
    promoter_holding_pct: Optional[float] = None
    fii_holding_pct: Optional[float] = None
    dii_holding_pct: Optional[float] = None
    promoter_holding_change_1y_pct: Optional[float] = None
    fii_holding_change_1y_pct: Optional[float] = None
    dii_holding_change_1y_pct: Optional[float] = None
    as_of_date: Optional[str] = None
    scraped_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _required_str(self.symbol, field_name="symbol"))
        object.__setattr__(self, "statement_scope", _optional_str(self.statement_scope) or "consolidated")
        object.__setattr__(self, "company_name", _optional_str(self.company_name))
        for name in (
            "market_cap_cr", "current_price", "stock_pe", "book_value", "dividend_yield_pct",
            "latest_quarter_revenue", "latest_quarter_net_profit", "latest_quarter_eps",
            "ttm_revenue", "ttm_net_profit", "quarterly_revenue_yoy_pct", "quarterly_profit_yoy_pct",
            "latest_roce_pct", "latest_roe_pct", "promoter_holding_pct", "fii_holding_pct",
            "dii_holding_pct", "promoter_holding_change_1y_pct", "fii_holding_change_1y_pct",
            "dii_holding_change_1y_pct",
        ):
            object.__setattr__(self, name, _coerce_optional_float(getattr(self, name)))
        object.__setattr__(self, "as_of_date", _optional_str(self.as_of_date))
        object.__setattr__(self, "scraped_at", _optional_str(self.scraped_at))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class FundamentalFeatures(FundamentalsEnvelope):
    """Typed fundamentals feature snapshot for a symbol list or index universe."""

    features: List[FundamentalFeatureRow] = field(default_factory=list)
    missing_symbols: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        features = [row if isinstance(row, FundamentalFeatureRow) else FundamentalFeatureRow.model_validate(row) for row in list(self.features or [])]
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "missing_symbols", [str(item) for item in list(self.missing_symbols or [])])

    def for_symbol(self, symbol: str) -> Optional[FundamentalFeatureRow]:
        wanted = str(symbol or "").strip().upper()
        for row in self.features:
            if row.symbol.upper() == wanted:
                return row
        return None


@dataclass(frozen=True)
class FundamentalsSymbolStatus(RawModelMixin):
    symbol: str
    statement_scope: str = "consolidated"
    status: str = ""
    last_checked_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _required_str(self.symbol, field_name="symbol"))
        object.__setattr__(self, "statement_scope", _optional_str(self.statement_scope) or "consolidated")
        object.__setattr__(self, "status", str(self.status or ""))
        object.__setattr__(self, "last_checked_at", _optional_str(self.last_checked_at))
        object.__setattr__(self, "last_success_at", _optional_str(self.last_success_at))
        object.__setattr__(self, "last_error", _optional_str(self.last_error))
        object.__setattr__(self, "raw", dict(self.raw or {}))


@dataclass(frozen=True)
class FundamentalsStatus(FundamentalsEnvelope):
    """Per-symbol freshness plus recent sync-run history."""

    symbols: List[FundamentalsSymbolStatus] = field(default_factory=list)
    missing_symbols: List[str] = field(default_factory=list)
    recent_runs: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        symbols = [row if isinstance(row, FundamentalsSymbolStatus) else FundamentalsSymbolStatus.model_validate(row) for row in list(self.symbols or [])]
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "missing_symbols", [str(item) for item in list(self.missing_symbols or [])])
        object.__setattr__(self, "recent_runs", [dict(run) for run in list(self.recent_runs or []) if isinstance(run, dict)])

    def fresh_within(self, symbol: str, hours: float, *, now: datetime) -> bool:
        """True when the symbol has a successful fetch within ``hours`` of ``now``."""
        wanted = str(symbol or "").strip().upper()
        for row in self.symbols:
            if row.symbol.upper() != wanted or not row.last_success_at:
                continue
            try:
                success_at = datetime.fromisoformat(str(row.last_success_at).replace("Z", "+00:00"))
            except ValueError:
                return False
            return (now - success_at).total_seconds() <= hours * 3600.0
        return False


@dataclass(frozen=True)
class FundamentalsStatements(FundamentalsEnvelope):
    """Raw statement rows for a single symbol and dataset."""

    symbol: str = ""
    statement_scope: str = "consolidated"
    dataset: str = ""
    rows: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "symbol", _required_str(self.symbol, field_name="symbol"))
        object.__setattr__(self, "statement_scope", _optional_str(self.statement_scope) or "consolidated")
        object.__setattr__(self, "dataset", _required_str(self.dataset, field_name="dataset"))
        object.__setattr__(self, "rows", [dict(row) for row in list(self.rows or []) if isinstance(row, dict)])


@dataclass(frozen=True)
class FundamentalsSyncRun(RawModelMixin):
    """Summary of one fundamentals sync run (the only mutating SDK response)."""

    run_id: str
    scope: Dict[str, str] = field(default_factory=dict)
    mode: str = "incremental"
    symbols_requested: int = 0
    symbols_changed: int = 0
    symbols_unchanged: int = 0
    symbols_failed: int = 0
    symbols_skipped: int = 0
    failed_symbols: List[Dict[str, str]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required_str(self.run_id, field_name="run_id"))
        object.__setattr__(self, "scope", {str(key): str(value) for key, value in dict(self.scope or {}).items()})
        object.__setattr__(self, "mode", str(self.mode or "incremental"))
        for name in ("symbols_requested", "symbols_changed", "symbols_unchanged", "symbols_failed", "symbols_skipped"):
            object.__setattr__(self, name, _coerce_int(getattr(self, name)))
        object.__setattr__(self, "failed_symbols", [dict(item) for item in list(self.failed_symbols or []) if isinstance(item, dict)])
        object.__setattr__(self, "raw", dict(self.raw or {}))


__all__ = [
    "FundamentalFeatureRow",
    "FundamentalFeatures",
    "FundamentalsEnvelope",
    "FundamentalsStatus",
    "FundamentalsStatements",
    "FundamentalsSymbolStatus",
    "FundamentalsSyncRun",
]
