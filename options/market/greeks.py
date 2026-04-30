from __future__ import annotations

from typing import Any, Mapping, Sequence


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
