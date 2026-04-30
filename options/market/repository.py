from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .selection import resolve_offset_strike


@dataclass(frozen=True)
class OffsetResolutionRequest:
    underlying: str
    expiry: date
    option_type: str
    offset: str
    atm_strike: float


class StrikeUniverseProvider(Protocol):
    def get_distinct_strikes(self, underlying: str, expiry: date) -> list[float]:
        ...


def resolve_offset_from_repository(
    request: OffsetResolutionRequest,
    *,
    provider: StrikeUniverseProvider,
) -> float:
    """Light repository-facing helper to resolve offsets against live strike universes."""
    strikes = provider.get_distinct_strikes(request.underlying, request.expiry)
    return resolve_offset_strike(
        option_type=request.option_type,
        offset=request.offset,
        atm_strike=request.atm_strike,
        available_strikes=strikes,
    )
