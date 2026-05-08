import pytest

from options.execution.lifecycle import (
    mark_closed,
    mark_entered,
    mark_entering,
    mark_entry_previewed,
    mark_exit_previewed,
    mark_exiting,
    mark_partial_entry,
    mark_partial_exit,
    transition_to,
)
from options.execution.models import OptionRunState, OptionRunStatus


def test_mark_partial_entry_sets_cleanup_required_when_some_legs_fail():
    state = mark_partial_entry(
        OptionRunState(status="created", completed_legs=[], failed_legs=[]),
        completed_legs=["A"],
        failed_legs=["B"],
    )

    assert state.status == "cleanup_required"
    assert state.completed_legs == ["A"]
    assert state.failed_legs == ["B"]


def test_mark_partial_entry_sets_entered_when_all_requested_legs_complete():
    state = mark_partial_entry(
        OptionRunState(status="created"),
        completed_legs=["A", "B"],
        failed_legs=[],
        pending_legs=[],
    )

    assert state.status == "entered"
    assert state.completed_legs == ["A", "B"]


def test_mark_partial_exit_sets_partially_exited_when_legs_remain_open():
    state = mark_partial_exit(
        OptionRunState(status="entered", completed_legs=["A", "B"]),
        remaining_open_legs=["B"],
    )

    assert state.status == "partial_exit"
    assert state.pending_legs == ["B"]


def test_mark_closed_sets_closed_status_without_pending_legs():
    state = mark_closed(OptionRunState(status="partial_exit", completed_legs=["A"], pending_legs=["A"]))

    assert state.status == "exited"
    assert state.pending_legs == []


def test_full_entry_path_transitions_created_to_entered():
    state = OptionRunState(status="created")
    state = mark_entry_previewed(state)
    state = mark_entering(state)
    state = mark_entered(state, completed_legs=["A", "B"])

    assert state.status == "entered"
    assert state.completed_legs == ["A", "B"]


def test_partial_entry_path_can_reach_cleanup_required():
    state = OptionRunState(status="created")
    state = mark_entry_previewed(state)
    state = mark_entering(state)
    state = mark_partial_entry(state, completed_legs=["A"], failed_legs=[], pending_legs=["B"])

    assert state.status == "partial_entry"

    state = transition_to(state, OptionRunStatus.CLEANUP_REQUIRED)
    assert state.status == "cleanup_required"


def test_exit_preview_and_exit_paths_cover_partial_and_full_exit():
    full = OptionRunState(status="entered", completed_legs=["A", "B"])
    full = mark_exit_previewed(full)
    full = mark_exiting(full, pending_legs=["A", "B"])
    full = mark_closed(full)
    assert full.status == "exited"

    partial = OptionRunState(status="entered", completed_legs=["A", "B"])
    partial = mark_exit_previewed(partial)
    partial = mark_exiting(partial, pending_legs=["A", "B"])
    partial = mark_partial_exit(partial, remaining_open_legs=["B"])
    assert partial.status == "partial_exit"


def test_invalid_transition_raises_value_error():
    with pytest.raises(ValueError, match="Invalid option run status transition"):
        transition_to(OptionRunState(status="created"), "exiting")
