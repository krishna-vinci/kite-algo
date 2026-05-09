from __future__ import annotations

from typing import Iterable

from .models import OptionRunState, OptionRunStatus


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    OptionRunStatus.CREATED.value: {
        OptionRunStatus.ENTRY_PREVIEWED.value,
        # Compatibility paths for existing direct state mutation helpers/tests.
        OptionRunStatus.ENTERING.value,
        OptionRunStatus.ENTERED.value,
        OptionRunStatus.PARTIAL_ENTRY.value,
        OptionRunStatus.CLEANUP_REQUIRED.value,
    },
    OptionRunStatus.ENTRY_PREVIEWED.value: {OptionRunStatus.ENTERING.value},
    OptionRunStatus.ENTERING.value: {
        OptionRunStatus.ENTERED.value,
        OptionRunStatus.PARTIAL_ENTRY.value,
    },
    OptionRunStatus.PARTIAL_ENTRY.value: {OptionRunStatus.CLEANUP_REQUIRED.value},
    OptionRunStatus.CLEANUP_REQUIRED.value: {OptionRunStatus.EXIT_PREVIEWED.value},
    OptionRunStatus.ENTERED.value: {
        OptionRunStatus.EXIT_PREVIEWED.value,
        # Compatibility for direct partial-exit helpers.
        OptionRunStatus.PARTIAL_EXIT.value,
    },
    OptionRunStatus.EXIT_PREVIEWED.value: {OptionRunStatus.EXITING.value},
    OptionRunStatus.EXITING.value: {
        OptionRunStatus.PARTIAL_EXIT.value,
        OptionRunStatus.EXITED.value,
    },
    OptionRunStatus.PARTIAL_EXIT.value: {
        OptionRunStatus.EXITING.value,
        # Compatibility for direct close helper.
        OptionRunStatus.EXITED.value,
    },
    OptionRunStatus.EXITED.value: set(),
}


def transition_to(state: OptionRunState, target_status: OptionRunStatus | str) -> OptionRunState:
    target = target_status.value if isinstance(target_status, OptionRunStatus) else str(target_status)
    if target not in _ALLOWED_TRANSITIONS.get(state.status, set()):
        raise ValueError(f"Invalid option run status transition: {state.status} -> {target}")
    return OptionRunState(
        strategy_run_id=state.strategy_run_id,
        strategy_name=state.strategy_name,
        product=state.product,
        legs=list(state.legs),
        protection=dict(state.protection) if state.protection is not None else None,
        metadata=dict(state.metadata),
        status=target,
        completed_legs=list(state.completed_legs),
        failed_legs=list(state.failed_legs),
        pending_legs=list(state.pending_legs),
        orders=list(state.orders),
        trades=list(state.trades),
    )


def mark_entry_previewed(state: OptionRunState) -> OptionRunState:
    return transition_to(state, OptionRunStatus.ENTRY_PREVIEWED)


def mark_entering(state: OptionRunState) -> OptionRunState:
    return transition_to(state, OptionRunStatus.ENTERING)


def mark_entered(state: OptionRunState, *, completed_legs: Iterable[str] | None = None) -> OptionRunState:
    next_state = transition_to(state, OptionRunStatus.ENTERED)
    if completed_legs is None:
        return next_state
    next_state.completed_legs = list(completed_legs)
    next_state.pending_legs = []
    next_state.failed_legs = []
    return next_state


def mark_cleanup_required(state: OptionRunState) -> OptionRunState:
    return transition_to(state, OptionRunStatus.CLEANUP_REQUIRED)


def mark_exit_previewed(state: OptionRunState) -> OptionRunState:
    return transition_to(state, OptionRunStatus.EXIT_PREVIEWED)


def mark_exiting(state: OptionRunState, *, pending_legs: Iterable[str] = ()) -> OptionRunState:
    next_state = transition_to(state, OptionRunStatus.EXITING)
    next_state.pending_legs = list(pending_legs)
    return next_state


def mark_exit_pending(state: OptionRunState, *, pending_legs: Iterable[str]) -> OptionRunState:
    """Compatibility alias for older lifecycle helper name."""
    return mark_exiting(state, pending_legs=pending_legs)


def mark_partial_entry(
    state: OptionRunState,
    *,
    completed_legs: Iterable[str],
    failed_legs: Iterable[str],
    pending_legs: Iterable[str] = (),
) -> OptionRunState:
    completed = list(completed_legs)
    failed = list(failed_legs)
    pending = list(pending_legs)

    if failed:
        status = OptionRunStatus.CLEANUP_REQUIRED
    elif pending:
        status = OptionRunStatus.PARTIAL_ENTRY
    elif completed:
        status = OptionRunStatus.ENTERED
    else:
        status = OptionRunStatus.ENTERING

    next_state = transition_to(state, status)
    next_state.completed_legs = completed
    next_state.failed_legs = failed
    next_state.pending_legs = pending
    return next_state


def mark_partial_exit(
    state: OptionRunState,
    *,
    remaining_open_legs: Iterable[str],
    failed_legs: Iterable[str] = (),
) -> OptionRunState:
    failed = list(failed_legs)
    remaining = list(remaining_open_legs)
    status = OptionRunStatus.CLEANUP_REQUIRED if failed else OptionRunStatus.PARTIAL_EXIT
    next_state = transition_to(state, status)
    next_state.completed_legs = remaining
    next_state.failed_legs = failed
    next_state.pending_legs = remaining
    return next_state


def mark_closed(state: OptionRunState) -> OptionRunState:
    next_state = transition_to(state, OptionRunStatus.EXITED)
    next_state.pending_legs = []
    return next_state
