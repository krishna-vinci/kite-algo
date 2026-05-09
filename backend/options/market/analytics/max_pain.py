from __future__ import annotations

from typing import Any, Mapping, Sequence


def _extract_numeric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compute_bounded_max_pain(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """Compute a deterministic max-pain strike over provided bounded rows.

    This intentionally remains a small helper over already-bounded chain rows.
    """
    if not rows:
        return None

    # Pre-extract strikes for deterministic iteration.
    strikes: list[float] = []
    for row in rows:
        strike_raw = row.get("strike")
        if strike_raw is None:
            continue
        try:
            strikes.append(float(strike_raw))
        except (TypeError, ValueError):
            continue

    if not strikes:
        return None

    pains: dict[float, float] = {}
    for candidate in strikes:
        pain = 0.0
        for row in rows:
            strike_raw = row.get("strike")
            if strike_raw is None:
                continue
            try:
                row_strike = float(strike_raw)
            except (TypeError, ValueError):
                continue

            ce = row.get("ce") or row.get("CE") or {}
            pe = row.get("pe") or row.get("PE") or {}
            ce_oi = _extract_numeric((ce or {}).get("oi"))
            pe_oi = _extract_numeric((pe or {}).get("oi"))

            # CE writers lose when settlement is above their strike.
            call_pain = max(0.0, candidate - row_strike) * ce_oi
            # PE writers lose when settlement is below their strike.
            put_pain = max(0.0, row_strike - candidate) * pe_oi
            pain += call_pain + put_pain

        pains[candidate] = pain

    # Deterministic tie-break: lowest strike among minimal pain values.
    return min(sorted(pains), key=lambda strike: pains[strike])
