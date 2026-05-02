from __future__ import annotations

from typing import Any, Mapping, Sequence


def compute_put_call_ratio(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """Compute bounded OI-based PCR across provided rows only."""
    total_calls = 0.0
    total_puts = 0.0

    for row in rows:
        ce = row.get("ce") or row.get("CE") or {}
        pe = row.get("pe") or row.get("PE") or {}
        total_calls += float((ce or {}).get("oi") or 0.0)
        total_puts += float((pe or {}).get("oi") or 0.0)

    if total_calls <= 0:
        return None
    return round(total_puts / total_calls, 2)
