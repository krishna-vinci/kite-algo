"""Read-only coherent portfolio observations for worker-authenticated clients."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping


class PortfolioSnapshotUnavailable(RuntimeError):
    """One required broker component was unavailable or inconsistent."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _holding(row: Mapping[str, Any]) -> Dict[str, Any]:
    item = _as_dict(row)
    names = (
        "exchange", "tradingsymbol", "instrument_token", "isin", "product", "quantity",
        "t1_quantity", "used_quantity", "realised_quantity", "authorised_quantity",
        "authorised_date", "authorisation", "opening_quantity", "short_quantity",
        "collateral_quantity", "collateral_type", "average_price", "last_price",
        "close_price", "pnl", "day_change", "day_change_percentage", "discrepancy", "mtf",
    )
    return {name: item.get(name) for name in names}


def _position(row: Mapping[str, Any]) -> Dict[str, Any]:
    item = _as_dict(row)
    names = (
        "exchange", "tradingsymbol", "instrument_token", "product", "quantity",
        "overnight_quantity", "day_buy_quantity", "day_sell_quantity", "buy_quantity",
        "sell_quantity", "buy_price", "sell_price", "average_price", "last_price",
        "pnl", "unrealised", "realised",
    )
    return {name: item.get(name) for name in names}


def build_portfolio_snapshot(kite: Any, account_scope: str, *, clock: Callable[[], datetime] = _utcnow) -> Dict[str, Any]:
    """Read funds, holdings, positions and profile once; never issue a broker mutation."""
    calls = {"funds": kite.margins, "holdings": kite.holdings, "positions": kite.positions, "profile": kite.profile}
    values: Dict[str, Any] = {}
    times: Dict[str, str] = {}
    first: datetime | None = None
    last: datetime | None = None
    failures: list[str] = []
    for name, call in calls.items():
        observed_at = clock()
        first = first or observed_at
        last = observed_at
        try:
            values[name] = call()
            times[name] = _iso(observed_at)
        except Exception:
            failures.append(name)
    retrieved_at = clock()
    if failures:
        raise PortfolioSnapshotUnavailable("required component unavailable: " + ",".join(sorted(failures)))
    holdings = [_holding(item) for item in list(values["holdings"] or [])]
    identities = [(item["exchange"], item["tradingsymbol"], item["instrument_token"], item["product"]) for item in holdings]
    if len(identities) != len(set(identities)):
        raise PortfolioSnapshotUnavailable("duplicate holding identity")
    positions = _as_dict(values["positions"])
    profile = _as_dict(values["profile"])
    meta = _as_dict(profile.get("meta"))
    return {
        "schema_version": 1,
        "account_scope": account_scope,
        "source": "kite_connect_portfolio",
        "source_as_of": _iso(last or retrieved_at),
        "retrieved_at": _iso(retrieved_at),
        "component_times": times,
        "coherent": True,
        "coherence_skew_ms": int(((last or retrieved_at) - (first or retrieved_at)).total_seconds() * 1000),
        "funds": values["funds"],
        "holdings": holdings,
        "net_positions": [_position(item) for item in list(positions.get("net") or [])],
        "day_positions": [_position(item) for item in list(positions.get("day") or [])],
        "profile_capabilities": {"demat_consent": meta.get("demat_consent") if "demat_consent" in meta else None},
    }
