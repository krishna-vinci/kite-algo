from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .models import ResolvedOptionContract


def _normalize_option_type(option_type: str) -> str:
    cleaned = str(option_type or "").strip().upper()
    if cleaned not in {"CE", "PE"}:
        raise ValueError(f"Unsupported option_type: {option_type}")
    return cleaned


def _normalize_offset(offset: str) -> tuple[str, int]:
    cleaned = str(offset or "").strip().upper()
    if cleaned == "ATM":
        return "ATM", 0
    if cleaned.startswith("ITM") or cleaned.startswith("OTM"):
        side = cleaned[:3]
        steps_raw = cleaned[3:]
        if not steps_raw:
            raise ValueError(f"Offset requires explicit steps: {offset}")
        try:
            steps = int(steps_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid offset steps in {offset}") from exc
        if steps < 0:
            raise ValueError(f"Offset steps must be >= 0: {offset}")
        return side, steps
    raise ValueError(f"Unsupported offset value: {offset}")


def _ordered_strikes(available_strikes: Iterable[float]) -> list[float]:
    try:
        ordered = sorted({float(value) for value in available_strikes})
    except (TypeError, ValueError) as exc:
        raise ValueError("available_strikes must contain numeric values") from exc
    if not ordered:
        raise ValueError("available_strikes must not be empty")
    return ordered


def _nearest_index(values: Sequence[float], target: float) -> int:
    return min(range(len(values)), key=lambda i: abs(values[i] - target))


def resolve_offset_index(*, option_type: str, offset_kind: str, steps: int, atm_index: int) -> int:
    """Pure index resolver for ATM/ITM/OTM semantics."""
    if offset_kind == "ATM":
        return atm_index

    if steps < 0:
        raise ValueError("steps must be >= 0")

    normalized_type = _normalize_option_type(option_type)
    if offset_kind not in {"ITM", "OTM"}:
        raise ValueError(f"Unsupported offset_kind: {offset_kind}")

    # Increasing strike direction:
    # CE: OTM is +, ITM is -
    # PE: OTM is -, ITM is +
    if normalized_type == "CE":
        sign = 1 if offset_kind == "OTM" else -1
    else:
        sign = -1 if offset_kind == "OTM" else 1

    return atm_index + (sign * steps)


def resolve_offset_strike(*, option_type: str, offset: str, atm_strike: float, available_strikes: Iterable[float]) -> float:
    """Resolve a strike from ATM/ITM/OTM offset semantics over real strike arrays."""
    normalized_type = _normalize_option_type(option_type)
    offset_kind, steps = _normalize_offset(offset)
    ordered = _ordered_strikes(available_strikes)

    atm_idx = _nearest_index(ordered, float(atm_strike))
    target_idx = resolve_offset_index(
        option_type=normalized_type,
        offset_kind=offset_kind,
        steps=steps,
        atm_index=atm_idx,
    )
    if target_idx < 0 or target_idx >= len(ordered):
        raise ValueError(
            f"Offset {offset} from ATM {ordered[atm_idx]} exceeds available strike range"
        )
    return float(ordered[target_idx])


def resolve_offset_contract(
    *,
    underlying: str,
    expiry: date,
    option_type: str,
    offset: str,
    atm_strike: float,
    available_strikes: Iterable[float],
    tradingsymbol_by_strike: Mapping[float, str],
    instrument_token_by_strike: Mapping[float, Any],
    lot_size: int,
    tick_size: float,
    ltp_by_strike: Mapping[float, float],
) -> ResolvedOptionContract:
    resolved_strike = resolve_offset_strike(
        option_type=option_type,
        offset=offset,
        atm_strike=atm_strike,
        available_strikes=available_strikes,
    )

    if resolved_strike not in tradingsymbol_by_strike:
        raise ValueError(f"Missing tradingsymbol for strike {resolved_strike}")
    if resolved_strike not in instrument_token_by_strike:
        raise ValueError(f"Missing instrument token for strike {resolved_strike}")
    try:
        instrument_token = int(instrument_token_by_strike[resolved_strike])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid instrument token for strike {resolved_strike}") from exc
    if instrument_token <= 0:
        raise ValueError(f"Invalid instrument token for strike {resolved_strike}")
    if resolved_strike not in ltp_by_strike:
        raise ValueError(f"Missing ltp for strike {resolved_strike}")

    normalized_option_type = _normalize_option_type(option_type)
    normalized_underlying = str(underlying or "").strip().upper()

    return ResolvedOptionContract(
        underlying=normalized_underlying,
        expiry=expiry,
        strike=float(resolved_strike),
        option_type=normalized_option_type,
        tradingsymbol=str(tradingsymbol_by_strike[resolved_strike]),
        instrument_token=instrument_token,
        lot_size=int(lot_size),
        tick_size=float(tick_size),
        ltp=float(ltp_by_strike[resolved_strike]),
        resolver="offset",
        resolution_meta={
            "offset": str(offset).upper(),
            "atm_strike": float(atm_strike),
            "resolved_strike": float(resolved_strike),
        },
    )


def resolve_delta_contract(
    *,
    underlying: str,
    expiry: date,
    option_type: str,
    delta_target: float,
    contracts_by_strike: Mapping[float, Mapping[str, Any]],
    lot_size: int = 1,
    tick_size: float = 0.05,
) -> ResolvedOptionContract:
    """Resolve nearest contract by already-computed snapshot delta.

    This intentionally consumes only per-contract snapshot Greeks. It does not
    recompute IV/Greeks from spot, preserving the option-session
    synthetic-forward/Black-76 source of truth.
    """

    normalized_type = _normalize_option_type(option_type)
    try:
        target = float(delta_target)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid delta_target: {delta_target}") from exc

    candidates: list[tuple[float, float, Mapping[str, Any]]] = []
    for strike, payload in contracts_by_strike.items():
        if not isinstance(payload, Mapping):
            continue
        raw_delta = payload.get("delta")
        if raw_delta is None:
            continue
        try:
            delta = float(raw_delta)
            strike_key = float(strike)
        except (TypeError, ValueError):
            continue
        candidates.append((strike_key, delta, payload))

    if not candidates:
        raise ValueError("No contracts with snapshot delta are available for delta-target selection")

    # Positive PE targets are treated as magnitudes for worker ergonomics
    # (e.g. PE delta_target=0.30 means nearest |delta| ~= 0.30). Negative PE
    # targets and all CE targets compare against the raw signed delta.
    use_magnitude = normalized_type == "PE" and target >= 0

    def score(item: tuple[float, float, Mapping[str, Any]]) -> tuple[float, float]:
        strike, delta, _payload = item
        observed = abs(delta) if use_magnitude else delta
        return (abs(observed - target), strike)

    resolved_strike, resolved_delta, resolved_payload = min(candidates, key=score)
    if not resolved_payload.get("tsym"):
        raise ValueError(f"Missing tradingsymbol for strike {resolved_strike}")
    token = resolved_payload.get("token")
    if token is None:
        raise ValueError(f"Missing instrument token for strike {resolved_strike}")

    normalized_underlying = str(underlying or "").strip().upper()
    return ResolvedOptionContract(
        underlying=normalized_underlying,
        expiry=expiry,
        strike=float(resolved_strike),
        option_type=normalized_type,
        tradingsymbol=str(resolved_payload.get("tsym")),
        instrument_token=int(token),
        lot_size=int(lot_size),
        tick_size=float(tick_size),
        ltp=float(resolved_payload.get("ltp") or 0.0),
        resolver="delta",
        resolution_meta={
            "delta_target": target,
            "resolved_delta": resolved_delta,
            "resolved_strike": float(resolved_strike),
            "delta_comparison": "magnitude" if use_magnitude else "signed",
        },
    )
