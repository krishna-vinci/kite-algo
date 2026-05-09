import pytest

from backend.journaling.v2.episodes import (
    PositionEffect,
    classify_position_effect,
    next_episode_sequence,
    should_close_episode_after_fill,
)


@pytest.mark.parametrize(
    ("previous_qty", "side", "quantity", "expected"),
    [
        (0, "BUY", 10, PositionEffect.OPEN.value),
        (10, "BUY", 5, PositionEffect.ADD.value),
        (10, "SELL", 4, PositionEffect.REDUCE.value),
        (10, "SELL", 10, PositionEffect.CLOSE.value),
        (10, "SELL", 15, PositionEffect.FLIP.value),
        (-10, "SELL", 5, PositionEffect.ADD.value),
        (-10, "BUY", 4, PositionEffect.REDUCE.value),
        (-10, "BUY", 10, PositionEffect.CLOSE.value),
        (-10, "BUY", 15, PositionEffect.FLIP.value),
    ],
)
def test_classify_position_effect_cases(previous_qty: int, side: str, quantity: int, expected: str) -> None:
    assert classify_position_effect(previous_qty=previous_qty, side=side, quantity=quantity) == expected


def test_classify_position_effect_rejects_invalid_quantity() -> None:
    with pytest.raises(ValueError, match="quantity must be > 0"):
        classify_position_effect(previous_qty=0, side="BUY", quantity=0)


def test_classify_position_effect_rejects_invalid_side() -> None:
    with pytest.raises(ValueError, match="side must be BUY or SELL"):
        classify_position_effect(previous_qty=0, side="HOLD", quantity=1)


def test_next_episode_sequence_empty_defaults_to_one() -> None:
    assert next_episode_sequence([]) == 1


def test_next_episode_sequence_uses_max_plus_one() -> None:
    assert next_episode_sequence([1, 4, 2]) == 5


def test_should_close_episode_after_fill_only_true_for_close() -> None:
    assert should_close_episode_after_fill(previous_qty=10, side="SELL", quantity=10) is True
    assert should_close_episode_after_fill(previous_qty=10, side="SELL", quantity=4) is False
    assert should_close_episode_after_fill(previous_qty=10, side="BUY", quantity=5) is False
    assert should_close_episode_after_fill(previous_qty=0, side="BUY", quantity=1) is False
    assert should_close_episode_after_fill(previous_qty=10, side="SELL", quantity=15) is False
