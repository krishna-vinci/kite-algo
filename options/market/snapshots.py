from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


def _ordered_unique_strikes(strikes: Iterable[float]) -> list[float]:
    try:
        return sorted({float(value) for value in strikes})
    except (TypeError, ValueError) as exc:
        raise ValueError("strikes must contain numeric values") from exc


def build_bounded_strike_window(*, strikes: Sequence[float], atm_strike: float, window: int) -> list[float]:
    """Return a bounded strike window around the nearest ATM strike.

    Window semantics match existing usage in sessions: total size is up to
    ``2 * window + 1`` strikes, clipped to available boundaries.
    """
    if window < 0:
        raise ValueError("window must be >= 0")

    ordered = _ordered_unique_strikes(strikes)
    if not ordered:
        return []

    atm = float(atm_strike)
    atm_index = min(range(len(ordered)), key=lambda idx: abs(ordered[idx] - atm))
    start = max(0, atm_index - window)
    end = min(len(ordered), atm_index + window + 1)
    return ordered[start:end]


def build_mini_chain_view(
    *,
    atm_strike: float,
    window: int,
    strikes: Sequence[float],
    strike_rows: Mapping[float, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build a bounded mini-chain row view centered around ATM.

    This helper is intentionally pure and deterministic over provided inputs.
    Missing rows for selected strikes are skipped.
    """
    selected_strikes = build_bounded_strike_window(
        strikes=strikes,
        atm_strike=atm_strike,
        window=window,
    )

    normalized_rows = {float(strike): dict(row) for strike, row in strike_rows.items()}
    return [normalized_rows[strike] for strike in selected_strikes if strike in normalized_rows]
