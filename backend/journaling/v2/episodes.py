from __future__ import annotations

from collections.abc import Iterable
from enum import Enum


class PositionEffect(str, Enum):
    OPEN = "open"
    ADD = "add"
    REDUCE = "reduce"
    CLOSE = "close"
    FLIP = "flip"


def classify_position_effect(*, previous_qty: int, side: str, quantity: int) -> str:
    if quantity <= 0:
        raise ValueError("quantity must be > 0")

    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    delta = quantity if normalized_side == "BUY" else -quantity
    new_qty = int(previous_qty) + delta

    if previous_qty == 0:
        return PositionEffect.OPEN.value
    if new_qty == 0:
        return PositionEffect.CLOSE.value

    if previous_qty > 0:
        if normalized_side == "BUY":
            return PositionEffect.ADD.value
        return PositionEffect.REDUCE.value if new_qty > 0 else PositionEffect.FLIP.value

    if normalized_side == "SELL":
        return PositionEffect.ADD.value
    return PositionEffect.REDUCE.value if new_qty < 0 else PositionEffect.FLIP.value


def next_episode_sequence(existing_sequences: Iterable[int]) -> int:
    max_seen: int | None = None
    for sequence in existing_sequences:
        value = int(sequence)
        if max_seen is None or value > max_seen:
            max_seen = value
    return 1 if max_seen is None else (max_seen + 1)


def should_close_episode_after_fill(*, previous_qty: int, side: str, quantity: int) -> bool:
    return classify_position_effect(previous_qty=previous_qty, side=side, quantity=quantity) == PositionEffect.CLOSE.value
