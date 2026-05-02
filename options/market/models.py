from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict


@dataclass(frozen=True)
class ResolvedOptionContract:
    """Canonical resolved contract for option selection results."""

    underlying: str
    expiry: date
    strike: float
    option_type: str
    tradingsymbol: str
    instrument_token: int
    lot_size: int
    tick_size: float
    ltp: float
    resolver: str
    resolution_meta: Dict[str, Any] = field(default_factory=dict)
