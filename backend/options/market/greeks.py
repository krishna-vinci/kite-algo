from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, Tuple


def _extract_greek_packet(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    return {
        "token": value.get("token"),
        "tsym": value.get("tsym") or value.get("tradingsymbol"),
        "iv": value.get("iv"),
        "ltp": value.get("ltp"),
        "delta": value.get("delta"),
        "gamma": value.get("gamma"),
        "theta": value.get("theta"),
        "vega": value.get("vega"),
        "rho": value.get("rho"),
        "updated_at": value.get("updated_at"),
    }


def build_greeks_view(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for row in rows:
        contracts.append(
            {
                "strike": row.get("strike"),
                "ce": _extract_greek_packet(row.get("ce") or row.get("CE")),
                "pe": _extract_greek_packet(row.get("pe") or row.get("PE")),
            }
        )
    return contracts


def _packet_by_tradingsymbol(contracts: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    lookup: dict[str, Mapping[str, Any]] = {}
    for row in contracts:
        for side in ("ce", "pe"):
            packet = row.get(side)
            tsym = packet.get("tsym") if isinstance(packet, Mapping) else None
            if tsym:
                lookup[str(tsym).upper()] = packet
    return lookup


def _parse_updated_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _unavailable_greeks(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
    }


def aggregate_run_greeks(
    legs: Sequence[Tuple[str, float]],
    contracts: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    max_age_seconds: float = 10.0,
) -> dict[str, Any]:
    """Sum signed-quantity greeks for a set of OPEN legs against one chain snapshot.

    ``legs`` is ``(tradingsymbol, signed_quantity)`` pairs, one per OPEN leg -
    positive for a long (BUY) position, negative for a short (SELL) one.
    ``contracts`` is the ``get_greeks()`` ``"contracts"`` list, keyed here by
    tradingsymbol. A contract that is missing, has no per-contract Greeks, or
    is older than ``max_age_seconds`` fails the WHOLE aggregate rather than
    silently dropping one leg's contribution: a partial sum would be a guess.
    """

    if not legs:
        return _unavailable_greeks("no_open_legs")

    lookup = _packet_by_tradingsymbol(contracts)
    totals = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for tradingsymbol, signed_quantity in legs:
        packet = lookup.get(str(tradingsymbol).upper())
        if packet is None:
            return _unavailable_greeks("missing")
        updated_at = _parse_updated_at(packet.get("updated_at"))
        if updated_at is None:
            return _unavailable_greeks("missing")
        age = (now - updated_at).total_seconds()
        if age > max_age_seconds:
            return _unavailable_greeks("stale")
        for key in totals:
            value = packet.get(key)
            if value is None:
                return _unavailable_greeks("missing")
            totals[key] += float(signed_quantity) * float(value)

    return {"available": True, "reason": None, **totals}
