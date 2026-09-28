from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np


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

    valid_rows = []
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
        valid_rows.append(
            (
                row_strike,
                _extract_numeric((ce or {}).get("oi")),
                _extract_numeric((pe or {}).get("oi")),
            )
        )

    row_strikes = np.asarray([row[0] for row in valid_rows], dtype=np.float64)
    ce_oi = np.asarray([row[1] for row in valid_rows], dtype=np.float64)
    pe_oi = np.asarray([row[2] for row in valid_rows], dtype=np.float64)
    candidates = np.asarray(strikes, dtype=np.float64)
    call_pain = np.maximum(candidates[:, None] - row_strikes[None, :], 0.0) * ce_oi
    put_pain = np.maximum(row_strikes[None, :] - candidates[:, None], 0.0) * pe_oi
    pains = np.sum(call_pain + put_pain, axis=1)

    # Deterministic tie-break: lowest strike among minimal pain values.
    minimum = np.min(pains)
    return float(np.min(candidates[pains == minimum]))
