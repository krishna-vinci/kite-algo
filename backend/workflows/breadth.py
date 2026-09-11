"""Windowed distinct-symbol participation (Phase 4 F10 breadth).

Meaning, precisely: **"at least K different instruments triggered during the
last W"** — windowed participation, never simultaneous breadth (that mode is
reserved and rejected at validation).

Durable state lives in ``advanced_repository`` (never in per-subscription
checkpoints) and is serialized per stage with an advisory transaction lock.
The engine here is pure orchestration: it reads the locked state, counts
current members whose latest trigger is inside the window, applies the
threshold state machine and reports what should be published. The caller
commits everything in its own transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from backend.workflows import advanced_repository as repo

__all__ = ["BreadthOutcome", "evaluate_breadth"]


@dataclass(frozen=True)
class BreadthOutcome:
    """What one breadth evaluation decided.

    ``matched`` is always truthful (downstream filters see ``count >= K`` even
    while an already-satisfied threshold suppresses a repeat notification);
    ``fired`` is True only on the bar that mints a new crossing.
    ``unknown_reason`` is set when the stage could not be evaluated at all
    (stale or unavailable membership, capacity exceeded, a stale observation).
    """

    matched: Optional[bool]
    fired: bool
    count: int
    members: int
    reason: Optional[str]
    crossing_seq: int
    contributors: Tuple[str, ...] = ()
    window_start: Optional[datetime] = None


def evaluate_breadth(
    session: Session,
    *,
    owner_id: str,
    workflow_id: str,
    revision_id: str,
    stage_id: str,
    spec: Any,
    member_keys: Sequence[str],
    member_universe_revision: Optional[int],
    membership_resolved_at: Optional[datetime],
    triggering: Optional[str],
    observed_at: datetime,
    max_instruments: int,
    membership_max_age_s: int,
) -> BreadthOutcome:
    """Evaluate one breadth stage for one observation.

    ``triggering`` is the instrument that fired this evaluation (None when the
    evaluation is a periodic/per-instrument sweep that only needs to re-check
    the window). The stage's threshold row is locked first, then the baseline
    is read UNDER that lock, so an interleaved publication can never leave
    this evaluation reasoning about superseded state.
    """
    repo.lock_breadth_stage(
        session,
        owner_id=owner_id,
        workflow_id=workflow_id,
        revision_id=revision_id,
        stage_id=stage_id,
    )
    state = repo.read_breadth_state(
        session,
        owner_id=owner_id,
        workflow_id=workflow_id,
        revision_id=revision_id,
        stage_id=stage_id,
        for_update=True,
    )

    current = list(dict.fromkeys(member_keys))
    member_count = len(current)

    # Capacity (D17): never resolve an overflow by pruning a live
    # contribution — a partial count is indistinguishable from real absence
    # of breadth, so the stage reports unknown instead.
    if member_count > max_instruments:
        return BreadthOutcome(
            matched=None,
            fired=False,
            count=0,
            members=member_count,
            reason="breadth_capacity_exceeded",
            crossing_seq=int(state.crossing_seq) if state else 0,
        )

    # Membership freshness (D16): a stale membership snapshot must not support
    # a signal, so the stage is unknown rather than silently counting.
    if membership_resolved_at is None:
        return BreadthOutcome(
            matched=None,
            fired=False,
            count=0,
            members=member_count,
            reason="membership_unavailable",
            crossing_seq=int(state.crossing_seq) if state else 0,
        )
    membership_age_s = (observed_at - membership_resolved_at).total_seconds()
    if membership_age_s > membership_max_age_s:
        return BreadthOutcome(
            matched=None,
            fired=False,
            count=0,
            members=member_count,
            reason="membership_stale",
            crossing_seq=int(state.crossing_seq) if state else 0,
        )

    watermark = state.aggregation_watermark if state else None
    # The aggregate is evaluated at the LATEST logical time seen, never
    # backwards. A contribution that arrives late still participates, because
    # the count that matters is the count as of the watermark — otherwise a
    # crossing could be missed entirely when the newest-timestamped
    # observation happened to be evaluated before its peers committed. This is
    # not a retroactive notification: the crossing is minted at the watermark,
    # which is the current logical time, not at the late bar's time.
    effective_at = observed_at if watermark is None else max(observed_at, watermark)
    stale_observation = watermark is not None and observed_at < watermark

    # Record this instrument's contribution unconditionally. The write is
    # monotonic, so a late observation records its own (older) trigger without
    # rewinding a newer one — recording it before anything else is what makes
    # the aggregate independent of arrival order.
    if triggering is not None:
        repo.upsert_breadth_contribution(
            session,
            owner_id=owner_id,
            workflow_id=workflow_id,
            revision_id=revision_id,
            stage_id=stage_id,
            instrument_key=triggering,
            trigger_ts=observed_at,
            bar_ts=observed_at,
            universe_revision=member_universe_revision,
        )

    window_start = effective_at - timedelta(seconds=int(spec.window_s))
    count, contributors = repo.count_breadth_contributions(
        session,
        owner_id=owner_id,
        workflow_id=workflow_id,
        revision_id=revision_id,
        stage_id=stage_id,
        window_start=window_start,
        evaluated_at=effective_at,
        members=current,
        limit=max(1, min(member_count or max_instruments, max_instruments)),
    )

    threshold = int(spec.distinct_instruments)
    satisfied = bool(state.satisfied) if state else False
    crossing_seq = int(state.crossing_seq) if state else 0
    satisfied_since = state.satisfied_since_ts if state else None
    last_fired = state.last_fired_ts if state else None
    fired = False

    if count >= threshold and not satisfied:
        # Crossing: a new durable identity, minted inside the serialized
        # transaction so two transitions sharing an event timestamp cannot
        # collide (the sequence number, never the timestamp, is the identity).
        crossing_seq += 1
        satisfied = True
        satisfied_since = effective_at
        last_fired = effective_at
        fired = True
    elif count < threshold and satisfied:
        # Rearm only on an OBSERVED count below the threshold: triggers aged
        # out of the window, or membership contracted. Time passing alone
        # never rearms.
        satisfied = False
        satisfied_since = None

    repo.upsert_breadth_state(
        session,
        owner_id=owner_id,
        workflow_id=workflow_id,
        revision_id=revision_id,
        stage_id=stage_id,
        satisfied=satisfied,
        crossing_seq=crossing_seq,
        satisfied_since_ts=satisfied_since,
        last_fired_ts=last_fired,
        count=count,
        member_count=member_count,
        aggregation_watermark=effective_at,
        membership_resolved_at=membership_resolved_at,
    )

    return BreadthOutcome(
        matched=bool(count >= threshold),
        fired=fired,
        count=count,
        members=member_count,
        # Informational: the observation was older than the watermark, so the
        # evaluation happened at the watermark instead. Recorded for evidence
        # and health, never a silent skip.
        reason="breadth_stale_observation" if stale_observation else None,
        crossing_seq=crossing_seq,
        contributors=tuple(k for k, _ in contributors),
        window_start=window_start,
    )
