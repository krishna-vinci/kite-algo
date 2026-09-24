"""The schedule runtime: occurrence materialisation, fencing, misfire, overlap (G11).

The stored schedule table already described *what* to run; this module decides
*when* it runs and guarantees that it runs at most once. Three rules carry it:

* **One occurrence, one row.** ``UNIQUE (schedule_id, occurrence_key)`` is the
  fencing mechanism for *materialisation*. Two schedulers racing the same tick
  collide on the index, so a duplicate row is impossible by construction rather
  than by a read-then-write that both of them could pass.

* **A missed occurrence fires late at most once.** Within
  ``SCHEDULE_MISFIRE_GRACE_SECONDS`` a due occurrence is fired late; beyond it the
  occurrence is recorded with a terminal reason. Neither path is silent, and
  neither fires twice.

* **A new evaluation never starts while the previous one is unresolved.** Overlap
  is not "skip forever" and not "run anyway": the occurrence stays ``pending``
  with the reason journalled and is retried on the next tick, so a slow month does
  not silently consume the next month's decision.

Fire means **create the pinned hosted job** for the occurrence, never a proposal:
the supervisor's existing lifecycle launches the child from that job, and only
the child — by executing Python — decides what to propose. ``HostedJobSubmitter``
is that production wiring, and ``uq_strategy_jobs_occurrence`` is what makes it
exactly-once across concurrent schedulers and a lost response.

Each fired occurrence carries a **new** ``evaluation_id`` — R3 §6's cardinality
rule — derived deterministically from the occurrence, and reuses the SAME strategy
book, which is what makes month-over-month continuity real.

A pending occurrence is *not* lost when a tick cannot finish it: the row is
resumed by the next tick (within the misfire grace, after which it is recorded
terminally), which is what makes a transient failure or an unresolved overlap
recoverable without ever firing twice.
"""

from __future__ import annotations

import calendar as _calendar
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.strategies.attribution_models import StrategyScheduleOccurrence

logger = logging.getLogger(__name__)

#: Kinds this runtime drives: every kind the stored schedule vocabulary allows
#: (``ck_hosted_strategy_schedules_kind`` in ``backend/strategies/models.py``).
#: There is no other driver for ``hosted_strategy_schedules`` — the pre-existing
#: worker-job runner drains *queued jobs*, not schedules — so a kind this runtime
#: skips is a kind that never runs at all.
SCHEDULE_KINDS = ("daily", "weekly", "monthly", "calendar")

#: How far back a daily/weekly schedule looks for occurrences that were due
#: during an outage. Bounded so a long outage still yields every occurrence the
#: misfire policy may fire, without walking to the beginning of time.
LOOKBACK_DAYS = 24

DEFAULT_MISFIRE_GRACE_SECONDS = 3600

#: What the runtime does when an occurrence is due while the previous evaluation
#: is still unresolved: the row stays ``pending`` and is retried on a later tick
#: (never fired early, never silently skipped). Reported verbatim by the
#: operator schedule view so the UI cannot invent a "skip" policy.
OVERLAP_POLICY = "defer_until_resolved"

#: Occurrence statuses that mean "this occurrence is done with".
SETTLED_OCCURRENCE_STATUSES = ("fired", "skipped", "expired")

#: Reservation statuses that still represent unresolved work.
UNRESOLVED_RESERVATION_STATUSES = ("active", "renewed", "action_required")

#: Job statuses that mean the attempt is over: a child that died without ever
#: producing a proposal no longer blocks the strategy's next occurrence once its
#: job reached one of these (or was explicitly reconciled).
QUIET_JOB_STATUSES = ("stopped", "failed", "hung")

#: How long one actor's occurrence-decision claim is honoured before another
#: actor may assume the claimer died mid-decision and take over. The claim is
#: ``fired_at`` on a still-``pending`` row: a pending row with a fresh
#: ``fired_at`` is a decision in flight, not a decision already made.
DECISION_CLAIM_STALE_SECONDS = 300

#: Sentinel: this write is a diagnostic and is not fenced on the decision claim.
NO_CLAIM = "no-claim"
#: Sentinels returned by :meth:`ScheduleScheduler._claim_decision`.
DECISION_SETTLED = "decision-settled"
DECISION_BUSY = "decision-busy"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands datetimes back naive; every schedule instant is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def misfire_grace_seconds() -> int:
    raw = os.environ.get("SCHEDULE_MISFIRE_GRACE_SECONDS")
    if raw is None:
        return DEFAULT_MISFIRE_GRACE_SECONDS
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_MISFIRE_GRACE_SECONDS


def _local_timezone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(name or "Asia/Kolkata"))
    except Exception:  # noqa: BLE001 - an unknown zone must not stop the scheduler
        return timezone.utc


def _parse_hhmm(value: str) -> time:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except Exception:  # noqa: BLE001
        return time(9, 30)


@dataclass(frozen=True)
class Occurrence:
    occurrence_key: str
    due_at: datetime
    evaluation_id: str


class ScheduleRefusal(Exception):
    """A **terminal** refusal: retrying this occurrence can never succeed.

    Raised by a submitter. The occurrence is recorded with ``reason_code`` (and
    the detail) so the record says why the scheduled work never ran, instead of
    the row being retried forever or skipped silently.
    """

    reason_code = "SCHEDULE_ERROR"

    def __init__(
        self,
        detail: Optional[Mapping[str, Any]] = None,
        *,
        reason_code: Optional[str] = None,
    ) -> None:
        if reason_code is not None:
            self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)


class ScheduleDeferred(Exception):
    """A **transient** refusal: the work is still valid, retry on a later tick.

    Overlap (an earlier occurrence is unresolved) and a blocked strategy are
    transient by design — the occurrence stays ``pending`` so the next tick can
    take the decision once the blocker clears.
    """


class ScheduleScheduler:
    """Materialises and fires occurrences. Never places an order itself."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        job_submitter: Optional[Callable[[Any, Occurrence, Mapping[str, Any]], bool]] = None,
        proposal_submitter: Optional[Callable[[Any, Occurrence, Mapping[str, Any]], bool]] = None,
    ) -> None:
        if session_factory is None:
            # The hosted-strategy tables live on the app database (the same
            # session factory the strategy owner routes use). The old default
            # imported a name that does not exist, so a production construction
            # raised ImportError instead of scheduling anything.
            from backend.app.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        # ``proposal_submitter`` is the retired name of ``job_submitter`` (a
        # scheduled occurrence creates a job, never a proposal). It is accepted
        # for existing callers only; new wiring must pass ``job_submitter``.
        self._submitter = job_submitter if job_submitter is not None else proposal_submitter

    # -- schedule reads -----------------------------------------------------

    def enabled_schedules(self) -> List[Dict[str, Any]]:
        """Schedules this runtime drives: the new kinds, enabled, unpaused.

        Read through the ORM because the schedule table has a model: on SQLite the
        ORM's tables live in the main schema, so ``public.``-qualified SQL would
        only work on PostgreSQL.
        """
        from backend.strategies.models import HostedStrategySchedule

        # A read failure here is NOT "no schedules": silently reporting an empty
        # set would look like a healthy tick while nothing is ever driven. Let it
        # propagate so the tick reports the error and the component degrades.
        with self.session_factory() as session:
            rows = session.execute(
                select(HostedStrategySchedule).where(
                    HostedStrategySchedule.enabled.is_(True),
                    HostedStrategySchedule.manual_paused_at.is_(None),
                    HostedStrategySchedule.schedule_kind.in_(SCHEDULE_KINDS),
                )
            ).scalars().all()
        return [
            {
                "id": str(row.id),
                "strategy_id": str(row.strategy_id),
                "version_id": str(row.version_id),
                "owner_id": str(row.owner_id),
                "account_scope": str(row.account_scope),
                "execution_mode": str(row.execution_mode),
                "schedule_kind": str(row.schedule_kind),
                "at_time": str(row.at_time),
                "timezone": str(row.timezone),
                "day_of_month": row.day_of_month,
                "weekday": row.weekday,
                "calendar_dates": list(row.calendar_dates or []),
                "job_kind": str(row.job_kind),
                "max_duration_s": int(row.max_duration_s),
                "progress_deadline_s": int(row.progress_deadline_s),
                "params_snapshot": dict(row.params_snapshot or {}),
                "capabilities_snapshot": dict(row.capabilities_snapshot or {}),
                "policy_snapshot": dict(row.policy_snapshot or {}),
            }
            for row in rows
        ]

    # -- occurrence computation --------------------------------------------

    @staticmethod
    def due_occurrences(schedule: Mapping[str, Any], *, now: datetime) -> List[Occurrence]:
        """Every occurrence of this schedule whose due time has passed.

        Deterministic and pure: the same schedule and the same instant always
        produce the same occurrences, which is what lets the unique index be the
        only deduplication needed.
        """
        kind = str(schedule.get("schedule_kind") or "")
        zone = _local_timezone(str(schedule.get("timezone") or "Asia/Kolkata"))
        at_time = _parse_hhmm(str(schedule.get("at_time") or "09:30"))
        moment = now.astimezone(zone)
        schedule_id = str(schedule.get("id") or "")
        occurrences: List[Occurrence] = []

        if kind == "monthly":
            day = int(schedule.get("day_of_month") or 1)
            # Walk months backwards from this one so a long outage still yields
            # every occurrence that was due, and let the misfire policy decide
            # which of them may still fire.
            cursor = date(moment.year, moment.month, 1)
            for _ in range(24):
                day_clamped = min(day, _calendar.monthrange(cursor.year, cursor.month)[1])
                due_local = datetime.combine(
                    date(cursor.year, cursor.month, day_clamped), at_time, tzinfo=zone
                )
                if due_local <= moment:
                    occurrences.append(
                        Occurrence(
                            occurrence_key=f"{schedule_id}:{due_local.date().isoformat()}",
                            due_at=due_local.astimezone(timezone.utc),
                            evaluation_id=f"sched:{schedule_id}:{due_local.date().isoformat()}",
                        )
                    )
                cursor = (cursor - timedelta(days=1)).replace(day=1)
        elif kind == "calendar":
            raw = schedule.get("calendar_dates") or []
            if isinstance(raw, str):
                import json

                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = []
            for entry in raw:
                try:
                    day = date.fromisoformat(str(entry))
                except ValueError:
                    continue
                due_local = datetime.combine(day, at_time, tzinfo=zone)
                if due_local <= moment:
                    occurrences.append(
                        Occurrence(
                            occurrence_key=f"{schedule_id}:{day.isoformat()}",
                            due_at=due_local.astimezone(timezone.utc),
                            evaluation_id=f"sched:{schedule_id}:{day.isoformat()}",
                        )
                    )
        elif kind == "daily":
            today = moment.date()
            for offset in range(LOOKBACK_DAYS):
                day = today - timedelta(days=offset)
                due_local = datetime.combine(day, at_time, tzinfo=zone)
                if due_local <= moment:
                    occurrences.append(
                        Occurrence(
                            occurrence_key=f"{schedule_id}:{day.isoformat()}",
                            due_at=due_local.astimezone(timezone.utc),
                            evaluation_id=f"sched:{schedule_id}:{day.isoformat()}",
                        )
                    )
        elif kind == "weekly":
            weekday = schedule.get("weekday")
            if isinstance(weekday, int) and not isinstance(weekday, bool) and 0 <= weekday <= 6:
                today = moment.date()
                for offset in range(LOOKBACK_DAYS * 7):
                    day = today - timedelta(days=offset)
                    if day.weekday() != weekday:
                        continue
                    due_local = datetime.combine(day, at_time, tzinfo=zone)
                    if due_local <= moment:
                        occurrences.append(
                            Occurrence(
                                occurrence_key=f"{schedule_id}:{day.isoformat()}",
                                due_at=due_local.astimezone(timezone.utc),
                                evaluation_id=f"sched:{schedule_id}:{day.isoformat()}",
                            )
                        )
        # Ascending, so a long outage replays oldest-first. Deterministic for a
        # given (schedule, instant), which is what lets the unique index be the
        # only deduplication needed.
        occurrences.sort(key=lambda item: (item.due_at, item.occurrence_key))
        return occurrences

    # -- materialisation ----------------------------------------------------

    def materialize(self, schedule: Mapping[str, Any], occurrence: Occurrence) -> Optional[str]:
        """Record the occurrence, returning its id, or ``None`` if it already exists.

        The unique index does the deduplication: the loser of a race gets an
        ``IntegrityError`` and returns ``None`` rather than raising, because
        "another scheduler already recorded this tick" is a normal outcome, not a
        failure.
        """
        occurrence_id = str(uuid.uuid4())
        session = self.session_factory()
        try:
            session.add(
                StrategyScheduleOccurrence(
                    id=occurrence_id,
                    schedule_id=str(schedule.get("id") or ""),
                    strategy_id=str(schedule.get("strategy_id") or ""),
                    occurrence_key=occurrence.occurrence_key,
                    due_at=occurrence.due_at,
                    status="pending",
                    evaluation_id=occurrence.evaluation_id,
                )
            )
            session.commit()
            return occurrence_id
        except IntegrityError:
            session.rollback()
            return None
        except SQLAlchemyError:
            session.rollback()
            raise
        finally:
            session.close()

    # -- unresolved work ----------------------------------------------------

    def previous_work_unresolved(
        self,
        *,
        schedule_id: str,
        strategy_id: str,
        account_scope: str,
        before_due_at: datetime,
    ) -> bool:
        """Whether an earlier occurrence of this schedule is still unresolved.

        Two kinds of evidence, both attributed: the envelope/reservation chain
        (scoped to this strategy and account, never by ``evaluation_id`` alone)
        and, when no envelope exists yet, the occurrence's **job**. A child that
        died without proposing must not block the strategy forever: once its job
        is stopped/failed/hung - or explicitly reconciled - the work is over, and
        the next occurrence is free to take its decision.

        Unreadable evidence is treated as unresolved ("fail closed"): a tick that
        cannot prove the previous decision finished must not start the next one.
        """
        from backend.strategies.attribution_models import (
            StrategyPlan,
            StrategyProposal,
            StrategyReservation,
        )
        from backend.strategies.models import StrategyJob

        try:
            with self.session_factory() as session:
                rows = session.execute(
                    select(
                        StrategyProposal.status,
                        StrategyReservation.status,
                        StrategyJob.status,
                        StrategyJob.reconciled_at,
                    )
                    .select_from(StrategyScheduleOccurrence)
                    .outerjoin(
                        StrategyProposal,
                        and_(
                            StrategyProposal.evaluation_id
                            == StrategyScheduleOccurrence.evaluation_id,
                            StrategyProposal.strategy_id == str(strategy_id),
                            StrategyProposal.account_id == str(account_scope),
                        ),
                    )
                    .outerjoin(StrategyPlan, StrategyPlan.proposal_id == StrategyProposal.proposal_id)
                    .outerjoin(StrategyReservation, StrategyReservation.plan_id == StrategyPlan.plan_id)
                    .outerjoin(
                        StrategyJob,
                        StrategyJob.occurrence_key == StrategyScheduleOccurrence.occurrence_key,
                    )
                    .where(
                        StrategyScheduleOccurrence.schedule_id == str(schedule_id),
                        StrategyScheduleOccurrence.status == "fired",
                        StrategyScheduleOccurrence.due_at < before_due_at,
                    )
                    .order_by(StrategyScheduleOccurrence.due_at.desc())
                ).all()
        except SQLAlchemyError:
            return True
        for proposal_status, reservation_status, job_status, reconciled_at in rows:
            proposal_status = str(proposal_status or "")
            reservation_status = str(reservation_status or "")
            if not proposal_status:
                # Fired, and the child has not produced an envelope yet. The job
                # is the authoritative evidence of whether that attempt is live.
                if job_status is None:
                    # No envelope and no job: the record is incomplete, and an
                    # incomplete record is not proof that the work finished.
                    return True
                if str(job_status) in QUIET_JOB_STATUSES:
                    continue
                if str(job_status) == "recovery_required" and reconciled_at is not None:
                    # Explicitly reconciled: the operator declared this attempt
                    # over, so it no longer blocks the next decision.
                    continue
                return True
            if proposal_status == "refused":
                continue
            if reservation_status in UNRESOLVED_RESERVATION_STATUSES:
                return True
            if proposal_status == "validated" and not reservation_status:
                return True
        return False

    # -- the tick -----------------------------------------------------------

    def tick(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """One scheduler pass with per-schedule failure isolation."""
        moment = now or _utcnow()
        grace = misfire_grace_seconds()
        fired: List[str] = []
        skipped: List[str] = []
        expired: List[str] = []
        deferred: List[str] = []
        errors: List[Dict[str, str]] = []

        for schedule in self.enabled_schedules():
            try:
                result = self._tick_schedule(schedule, moment=moment, grace=grace)
            except Exception as exc:  # noqa: BLE001 - one bad schedule must not stop the rest
                errors.append({"schedule_id": str(schedule.get("id") or ""), "error": repr(exc)})
                continue
            fired.extend(result["fired"])
            skipped.extend(result["skipped"])
            expired.extend(result["expired"])
            deferred.extend(result["deferred"])

        return {
            "fired": fired,
            "skipped": skipped,
            "expired": expired,
            "deferred": deferred,
            "errors": errors,
        }

    def _tick_schedule(
        self, schedule: Mapping[str, Any], *, moment: datetime, grace: int
    ) -> Dict[str, List[str]]:
        fired: List[str] = []
        skipped: List[str] = []
        expired: List[str] = []
        deferred: List[str] = []

        schedule_id = str(schedule.get("id") or "")
        for occurrence in self.due_occurrences(schedule, now=moment):
            materialized = self.materialize(schedule, occurrence)
            if materialized is None:
                # The row exists: either another scheduler already recorded this
                # tick, or a previous tick recorded it and could not finish it.
                # A settled row's decision stands; a still-pending row is
                # RESUMED, which is what makes a transient failure or an
                # unresolved overlap recoverable instead of a silent gap.
                existing = self.occurrence_row(
                    schedule_id=schedule_id, occurrence_key=occurrence.occurrence_key
                )
                if existing is None or str(existing["status"]) in SETTLED_OCCURRENCE_STATUSES:
                    continue
                occurrence_id = str(existing["id"])
                resumed = True
            else:
                occurrence_id = materialized
                resumed = False

            outcome = self._tick_occurrence(
                schedule,
                occurrence,
                occurrence_id=occurrence_id,
                resumed=resumed,
                moment=moment,
                grace=grace,
            )
            if outcome == "fired":
                fired.append(occurrence.occurrence_key)
            elif outcome == "expired":
                expired.append(occurrence.occurrence_key)
            elif outcome == "skipped":
                skipped.append(occurrence.occurrence_key)
            else:
                deferred.append(occurrence.occurrence_key)

        return {"fired": fired, "skipped": skipped, "expired": expired, "deferred": deferred}

    def _tick_occurrence(
        self,
        schedule: Mapping[str, Any],
        occurrence: Occurrence,
        *,
        occurrence_id: str,
        resumed: bool,
        moment: datetime,
        grace: int,
    ) -> str:
        """Decide one occurrence: ``fired`` / ``skipped`` / ``expired`` / ``deferred``.

        Every path runs inside the occurrence's **decision claim**. Taking the
        claim is what serialises the decision: an actor that does not hold it
        defers instead of expiring, skipping or launching, and every write is
        fenced by the exact claim value, so a claim that went stale and was taken
        over cannot be used to write anything.
        """
        if self._submitter is None:
            return self._defer(
                occurrence_id, error="no submitter is wired", release_claim=False
            )
        claim = self._claim_decision(occurrence_id)
        if claim is DECISION_SETTLED:
            return self._settled_outcome(occurrence_id)
        if claim is DECISION_BUSY:
            return self._defer(
                occurrence_id,
                error="another actor holds this occurrence's decision",
                release_claim=False,
            )
        try:
            return self._decide(
                schedule,
                occurrence,
                occurrence_id=occurrence_id,
                resumed=resumed,
                moment=moment,
                grace=grace,
                claim=claim,
            )
        except Exception as exc:  # noqa: BLE001 - unknown state is retried, never guessed
            return self._defer(
                occurrence_id, error=repr(exc), release_claim=True, claim=claim
            )

    def _decide(
        self,
        schedule: Mapping[str, Any],
        occurrence: Occurrence,
        *,
        occurrence_id: str,
        resumed: bool,
        moment: datetime,
        grace: int,
        claim: Any,
    ) -> str:
        """The claimed decision itself. Every write below carries ``claim``."""
        schedule_id = str(schedule.get("id") or "")
        if resumed:
            # Lost-response recovery FIRST: if this occurrence's launch already
            # exists, its real outcome is "fired" - never "expired", and never a
            # skip because the strategy has since been paused or disabled.
            resolved = self._resolve_existing_launch(schedule, occurrence)
            if resolved is True:
                settled = self._settle(
                    occurrence_id,
                    status="fired",
                    claim=claim,
                    fired_at=True,
                    clear_skip_reason=True,
                    detail={"evaluation_id": occurrence.evaluation_id, "recovered": True},
                )
                return "fired" if settled else self._settled_outcome(occurrence_id)
            if resolved is False:
                # A job holds this occurrence key but does not match the pinned
                # launch: terminal, recorded, never silently re-launched.
                settled = self._settle(
                    occurrence_id,
                    status="skipped",
                    claim=claim,
                    skip_reason="occurrence_conflict",
                    detail={"occurrence_key": occurrence.occurrence_key},
                )
                return "skipped" if settled else self._settled_outcome(occurrence_id)

        lateness = (moment - occurrence.due_at).total_seconds()
        if lateness > grace:
            # Never after the grace: the decision it would have made is no longer
            # the decision the strategy needs. A pending row that was deferred
            # until its window passed is recorded ``expired`` so the gap has a
            # terminal, named reason rather than staying pending.
            wanted = "expired" if resumed else "skipped"
            settled = self._settle(
                occurrence_id,
                status=wanted,
                claim=claim,
                skip_reason="misfire_window_passed" if resumed else "misfire_beyond_grace",
                detail={"lateness_seconds": lateness, "grace_seconds": grace},
            )
            return wanted if settled else self._settled_outcome(occurrence_id)

        if self.previous_work_unresolved(
            schedule_id=schedule_id,
            strategy_id=str(schedule.get("strategy_id") or ""),
            account_scope=str(schedule.get("account_scope") or ""),
            before_due_at=occurrence.due_at,
        ):
            # Overlap policy: not skipped forever and not run anyway. The row
            # stays pending so the next tick retries it - and our claim is given
            # back, so a decision we did not take does not block the next tick.
            self._note_overlap(occurrence_id, lateness=lateness, claim=claim)
            self._release_claim(occurrence_id, claim=claim)
            return "deferred"

        return self._fire(schedule, occurrence, occurrence_id, claim=claim)

    def _resolve_existing_launch(
        self, schedule: Mapping[str, Any], occurrence: Occurrence
    ) -> Optional[bool]:
        """Whether a launch already exists for this occurrence.

        ``True``  - a durable job exists and matches the pinned launch (fired).
        ``False`` - the key is held by a launch that does NOT match the pin.
        ``None``  - no job, or the submitter cannot answer (plain callables); the
                    normal grace/overlap path decides.
        """
        resolver = getattr(self._submitter, "existing_job", None)
        if resolver is None:
            return None
        return resolver(schedule, occurrence)

    def _fire(
        self,
        schedule: Mapping[str, Any],
        occurrence: Occurrence,
        occurrence_id: str,
        *,
        claim: Any,
    ) -> str:
        """Fire one occurrence: ``"fired"``, ``"skipped"``, ``"expired"`` or ``"deferred"``.

        In production the launch and the occurrence decision are ONE transaction
        (``submit_with_decision``): the job row and the ``pending -> fired``
        compare-and-set commit together, so a job can never exist for an
        occurrence some other actor expired, and an actor whose claim was taken
        over cannot leave a job behind. A test double without that hook falls
        back to submit-then-settle, which the claim fence still protects.

        Lock order for the atomic path (documented, and the only order used):
        take the occurrence decision claim (occurrence row only, committed) ->
        inside one transaction: lock the strategy row (``create_job``) -> insert
        the job -> compare-and-set the occurrence. No transaction is ever held
        while taking another claim, so the order cannot invert.
        """
        if self._submitter is None:
            return self._defer(
                occurrence_id,
                error="no submitter is wired",
                release_claim=False,
                claim=claim,
            )
        atomic = getattr(self._submitter, "submit_with_decision", None)
        if atomic is not None:
            return self._fire_atomically(
                schedule, occurrence, occurrence_id, claim=claim, submit=atomic
            )
        try:
            submitted = bool(self._submitter(schedule, occurrence, {"occurrence_id": occurrence_id}))
        except ScheduleRefusal as refusal:
            settled = self._settle(
                occurrence_id,
                status="skipped",
                claim=claim,
                skip_reason=refusal.reason_code,
                detail=refusal.detail,
            )
            return "skipped" if settled else self._settled_outcome(occurrence_id)
        except ScheduleDeferred as deferred:
            return self._defer(
                occurrence_id,
                error=str(deferred) or "deferred",
                release_claim=True,
                claim=claim,
            )
        except Exception as exc:  # noqa: BLE001 - unknown state is retried, never guessed
            return self._defer(
                occurrence_id, error=repr(exc), release_claim=True, claim=claim
            )
        if not submitted:
            return self._defer(
                occurrence_id,
                error="submitter reported no submission",
                release_claim=True,
                claim=claim,
            )
        settled = self._settle(
            occurrence_id,
            status="fired",
            claim=claim,
            fired_at=True,
            detail={"evaluation_id": occurrence.evaluation_id},
            clear_skip_reason=True,
        )
        if settled:
            return "fired"
        # Another actor decided this occurrence first (a duplicate successful
        # submission, or a stale tick). Report what is actually recorded.
        return self._settled_outcome(occurrence_id)

    def _fire_atomically(
        self,
        schedule: Mapping[str, Any],
        occurrence: Occurrence,
        occurrence_id: str,
        *,
        claim: Any,
        submit: Any,
    ) -> str:
        """Run the production submitter, whose job insert and decision commit together."""

        def decision(session: Any) -> bool:
            """The occurrence half of the atomic launch: same transaction as the job."""
            return self._settle(
                occurrence_id,
                status="fired",
                claim=claim,
                fired_at=True,
                clear_skip_reason=True,
                allowed_from=("pending",),
                session=session,
            )

        try:
            outcome = submit(
                schedule,
                occurrence,
                {"occurrence_id": occurrence_id},
                session_factory=self.session_factory,
                decision=decision,
            )
        except ScheduleRefusal as refusal:
            settled = self._settle(
                occurrence_id,
                status="skipped",
                claim=claim,
                skip_reason=refusal.reason_code,
                detail=refusal.detail,
            )
            return "skipped" if settled else self._settled_outcome(occurrence_id)
        except ScheduleDeferred as deferred:
            return self._defer(
                occurrence_id,
                error=str(deferred) or "deferred",
                release_claim=True,
                claim=claim,
            )
        except Exception as exc:  # noqa: BLE001 - unknown state is retried, never guessed
            return self._defer(
                occurrence_id, error=repr(exc), release_claim=True, claim=claim
            )
        if outcome in ("created", "replayed"):
            self._note_launch_detail(
                occurrence_id, occurrence, recovered=(outcome == "replayed")
            )
            return "fired"
        if outcome == "stale":
            # Our claim was taken over while we were launching: the transaction
            # rolled the job back, so nothing was left behind.
            return self._settled_outcome(occurrence_id)
        return self._defer(
            occurrence_id,
            error=f"unexpected submit outcome: {outcome}",
            release_claim=True,
            claim=claim,
        )

    def _note_launch_detail(
        self, occurrence_id: str, occurrence: Occurrence, *, recovered: bool = False
    ) -> None:
        """Diagnostics only: the decision itself is already committed."""
        detail: Dict[str, Any] = {"evaluation_id": occurrence.evaluation_id}
        if recovered:
            detail["recovered"] = True
        self._transition(occurrence_id, allowed_from=("fired",), detail=detail)

    def _defer(
        self,
        occurrence_id: str,
        *,
        error: str,
        release_claim: bool,
        claim: Any = NO_CLAIM,
    ) -> str:
        """Record a retryable failure, releasing our claim when we held one.

        Releasing matters: a claim left behind would make every later tick see
        "busy" until the claim went stale, turning a transient failure into a
        five-minute stall. The release is fenced on the claim we hold, so it can
        only ever give back *our* claim - never one a later actor took over.
        """
        self._note_transient_failure(occurrence_id, error=error)
        if release_claim:
            self._release_claim(occurrence_id, claim=claim)
        return "deferred"

    def _release_claim(self, occurrence_id: str, *, claim: Any) -> bool:
        return self._transition(
            occurrence_id, allowed_from=("pending",), claim=claim, clear_fired_at=True
        )

    def _settled_outcome(self, occurrence_id: str) -> str:
        """The outcome a decided occurrence actually carries."""
        status = self.occurrence_status(occurrence_id)
        if status == "fired":
            return "fired"
        if status in ("skipped", "expired"):
            return status
        return "deferred"

    def _claim_decision(self, occurrence_id: str) -> Any:
        """Claim the right to decide one occurrence.

        Returns the **claim value** (a UTC datetime) on success,
        :data:`DECISION_SETTLED` when the row is already decided, or
        :data:`DECISION_BUSY` when another actor holds a live claim.

        The claim is a compare-and-set on the occurrence row itself (no new
        column, no held transaction), and its **value** is what fences every
        later write: only the actor holding that exact value may settle, release
        or annotate the decision. A claim older than
        :data:`DECISION_CLAIM_STALE_SECONDS` may be taken over by another actor,
        which changes the value and therefore invalidates the previous holder's
        right to write anything - including the right to create the launch.
        """
        now = _utcnow()
        stale_before = now - timedelta(seconds=DECISION_CLAIM_STALE_SECONDS)
        with self.session_factory() as session:
            row = session.execute(
                select(
                    StrategyScheduleOccurrence.status,
                    StrategyScheduleOccurrence.fired_at,
                ).where(StrategyScheduleOccurrence.id == str(occurrence_id))
            ).fetchone()
            if row is None or str(row[0]) != "pending":
                return DECISION_SETTLED
            current_claim = row[1]
            if current_claim is not None and _as_utc(current_claim) > stale_before:
                return DECISION_BUSY
            statement = update(StrategyScheduleOccurrence).where(
                StrategyScheduleOccurrence.id == str(occurrence_id),
                StrategyScheduleOccurrence.status == "pending",
            )
            if current_claim is None:
                statement = statement.where(
                    StrategyScheduleOccurrence.fired_at.is_(None)
                )
            else:
                statement = statement.where(
                    StrategyScheduleOccurrence.fired_at == current_claim
                )
            result = session.execute(statement.values(fired_at=now))
            won = bool(getattr(result, "rowcount", 0))
            session.commit()
            return now if won else DECISION_BUSY

    # -- occurrence writes --------------------------------------------------

    def _transition(
        self,
        occurrence_id: str,
        *,
        allowed_from: Any,
        claim: Any = NO_CLAIM,
        status: Optional[str] = None,
        skip_reason: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
        fired_at: bool = False,
        clear_fired_at: bool = False,
        clear_skip_reason: bool = False,
        session: Any = None,
    ) -> bool:
        """Apply one occurrence transition atomically. ``True`` when this call won.

        The compare-and-set on ``status`` is the fencing contract: a stale tick
        (or a concurrent duplicate) cannot rewrite a decided occurrence, so
        ``fired`` is never overwritten by a later ``skipped``/``expired`` and a
        terminal outcome is never silently flipped. ``detail`` is merged only
        after the transition is won, so diagnostics cannot resurrect a decision.

        ``claim`` adds the *ownership* fence. A decision write passes the exact
        claim value it took from :meth:`_claim_decision`, and the statement then
        also requires ``fired_at`` to still equal it - so an actor that lost its
        claim to a stale-takeover cannot settle, release or expire the new
        holder's decision. ``NO_CLAIM`` means "do not fence on the claim" and is
        used only by diagnostics that do not change the decision.

        ``session`` lets the caller compose this write into its own transaction
        (the atomic job+decision launch); the caller then owns the commit.
        """
        own_session = session is None
        session = session if session is not None else self.session_factory()
        try:
            values: Dict[str, Any] = {
                "status": (
                    StrategyScheduleOccurrence.status if status is None else str(status)
                )
            }
            if skip_reason is not None:
                values["skip_reason"] = str(skip_reason)
            elif clear_skip_reason:
                values["skip_reason"] = None
            if fired_at:
                values["fired_at"] = _utcnow()
            elif clear_fired_at:
                values["fired_at"] = None
            statement = update(StrategyScheduleOccurrence).where(
                StrategyScheduleOccurrence.id == str(occurrence_id),
                StrategyScheduleOccurrence.status.in_(tuple(allowed_from)),
            )
            if claim is not NO_CLAIM:
                if claim is None:
                    statement = statement.where(
                        StrategyScheduleOccurrence.fired_at.is_(None)
                    )
                else:
                    statement = statement.where(
                        StrategyScheduleOccurrence.fired_at == claim
                    )
            result = session.execute(statement.values(**values))
            won = bool(getattr(result, "rowcount", 0))
            if own_session:
                session.commit()
            if won and detail:
                row = session.execute(
                    select(StrategyScheduleOccurrence).where(
                        StrategyScheduleOccurrence.id == str(occurrence_id)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    merged = dict(row.detail or {})
                    merged.update(dict(detail))
                    row.detail = merged
                    if own_session:
                        session.commit()
            return won
        finally:
            if own_session:
                session.close()

    def _settle(
        self,
        occurrence_id: str,
        *,
        status: str,
        claim: Any = NO_CLAIM,
        skip_reason: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
        fired_at: bool = False,
        clear_skip_reason: bool = False,
        allowed_from: Any = ("pending",),
        session: Any = None,
    ) -> bool:
        """Terminal settlement: only an undecided (``pending``) occurrence moves.

        ``fired_at`` doubles as the in-flight decision claim, so a terminal
        outcome that is not ``fired`` clears it: a skipped/expired row must not
        look like a decision that is still running.
        """
        return self._transition(
            occurrence_id,
            allowed_from=allowed_from,
            claim=claim,
            status=status,
            skip_reason=skip_reason,
            detail=detail,
            fired_at=(fired_at and str(status) == "fired"),
            clear_fired_at=str(status) != "fired",
            clear_skip_reason=clear_skip_reason,
            session=session,
        )

    def _note_overlap(
        self, occurrence_id: str, *, lateness: float, claim: Any = NO_CLAIM
    ) -> bool:
        return self._transition(
            occurrence_id,
            allowed_from=("pending",),
            claim=claim,
            skip_reason="overlap_skipped",
            detail={"overlap_skipped": True, "lateness_seconds": lateness},
        )

    def _note_transient_failure(self, occurrence_id: str, *, error: str) -> bool:
        """Record why a still-pending occurrence has not fired yet.

        The row stays ``pending`` (it is retryable), but the attempt is visible:
        an operator reading the occurrence sees the last transient error instead
        of an unexplained silence.
        """
        return self._transition(
            occurrence_id,
            allowed_from=("pending",),
            detail={
                "last_transient_error": str(error),
                "last_transient_at": _utcnow().isoformat(),
            },
        )

    def occurrence_status(self, occurrence_id: str) -> Optional[str]:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyScheduleOccurrence.status).where(
                    StrategyScheduleOccurrence.id == str(occurrence_id)
                )
            ).scalar_one_or_none()
            return None if row is None else str(row)

    def occurrence_row(self, *, schedule_id: str, occurrence_key: str) -> Optional[Dict[str, Any]]:
        """The materialised row for one occurrence, or ``None``."""
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyScheduleOccurrence).where(
                    StrategyScheduleOccurrence.schedule_id == str(schedule_id),
                    StrategyScheduleOccurrence.occurrence_key == str(occurrence_key),
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": str(row.id),
                "status": str(row.status),
                "detail": dict(row.detail or {}),
            }

    # -- reads --------------------------------------------------------------

    def occurrences_for_schedule(self, *, schedule_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(
                select(StrategyScheduleOccurrence)
                .where(StrategyScheduleOccurrence.schedule_id == str(schedule_id))
                .order_by(StrategyScheduleOccurrence.due_at.desc())
                .limit(int(limit))
            ).scalars().all()
            return [
                {
                    "id": str(row.id),
                    "schedule_id": str(row.schedule_id),
                    "strategy_id": str(row.strategy_id),
                    "occurrence_key": str(row.occurrence_key),
                    "due_at": row.due_at.isoformat() if row.due_at else None,
                    "status": str(row.status),
                    "fired_at": row.fired_at.isoformat() if row.fired_at else None,
                    "evaluation_id": row.evaluation_id,
                    "skip_reason": row.skip_reason,
                    "detail": dict(row.detail or {}),
                }
                for row in rows
            ]


def next_occurrence(schedule: Mapping[str, Any], *, now: datetime) -> Optional[Occurrence]:
    """The next occurrence at or after ``now``, mirroring the due-time rules.

    ``due_occurrences`` walks *backwards* to find work whose time has passed;
    the operator's "next run" question walks the same rules forward. Both read
    the same kind/timezone/clock fields and derive the same occurrence key, so a
    displayed next run cannot disagree with the row the scheduler materialises.
    Pure and bounded: it never touches the database and returns ``None`` rather
    than raising when no future occurrence exists (e.g. a calendar schedule
    whose dates have all passed).
    """
    kind = str(schedule.get("schedule_kind") or "")
    zone = _local_timezone(str(schedule.get("timezone") or "Asia/Kolkata"))
    at_time = _parse_hhmm(str(schedule.get("at_time") or "09:30"))
    moment = now.astimezone(zone)
    schedule_id = str(schedule.get("id") or "")

    def occurrence_for(day: date) -> Optional[Occurrence]:
        due_local = datetime.combine(day, at_time, tzinfo=zone)
        if due_local <= moment:
            return None
        return Occurrence(
            occurrence_key=f"{schedule_id}:{day.isoformat()}",
            due_at=due_local.astimezone(timezone.utc),
            evaluation_id=f"sched:{schedule_id}:{day.isoformat()}",
        )

    if kind == "monthly":
        day = int(schedule.get("day_of_month") or 1)
        cursor = date(moment.year, moment.month, 1)
        for _ in range(24):
            day_clamped = min(day, _calendar.monthrange(cursor.year, cursor.month)[1])
            candidate = occurrence_for(date(cursor.year, cursor.month, day_clamped))
            if candidate is not None:
                return candidate
            cursor = (cursor + timedelta(days=31)).replace(day=1)
        return None
    if kind == "calendar":
        raw = schedule.get("calendar_dates") or []
        if isinstance(raw, str):
            import json

            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        candidates: List[Occurrence] = []
        for entry in raw:
            try:
                day = date.fromisoformat(str(entry))
            except ValueError:
                continue
            candidate = occurrence_for(day)
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item.due_at, item.occurrence_key))
    if kind == "daily":
        today = moment.date()
        for offset in range(LOOKBACK_DAYS):
            candidate = occurrence_for(today + timedelta(days=offset))
            if candidate is not None:
                return candidate
        return None
    if kind == "weekly":
        weekday = schedule.get("weekday")
        if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
            return None
        today = moment.date()
        for offset in range(LOOKBACK_DAYS * 7):
            day = today + timedelta(days=offset)
            if day.weekday() != weekday:
                continue
            candidate = occurrence_for(day)
            if candidate is not None:
                return candidate
        return None
    return None


def pinned_launch_identity(
    schedule: Mapping[str, Any], occurrence: Occurrence
) -> Dict[str, Any]:
    """The bound evaluation identity a scheduled launch must carry.

    Deterministic and complete: every field the launch pins is present, so a job
    found under the same occurrence key can be compared against it rather than
    trusted because the key matched.
    """
    return {
        "source": "schedule_occurrence",
        "schedule_id": str(schedule.get("id") or ""),
        "occurrence_key": occurrence.occurrence_key,
        "evaluation_id": occurrence.evaluation_id,
        "evaluation_kind": "scheduled_occurrence",
        "due_at": occurrence.due_at.astimezone(timezone.utc).isoformat(),
    }


def matches_pinned_launch(
    job: Any, schedule: Mapping[str, Any], occurrence: Occurrence
) -> bool:
    """Whether a persisted job is exactly this schedule's pinned launch.

    Ownership, strategy, version, account, mode, kind, parameters, occurrence key
    and the bound identity must all agree. Anything else is a conflict, never a
    replay: an idempotency key held by another owner or another launch request
    must fail closed rather than hand that job's outcome to this schedule.
    """
    return (
        str(getattr(job, "owner_id", "") or "") == str(schedule.get("owner_id") or "")
        and str(getattr(job, "strategy_id", "") or "") == str(schedule.get("strategy_id") or "")
        and str(getattr(job, "version_id", "") or "") == str(schedule.get("version_id") or "")
        and str(getattr(job, "account_scope", "") or "")
        == str(schedule.get("account_scope") or "")
        and str(getattr(job, "job_kind", "") or "") == str(schedule.get("job_kind") or "")
        and str(getattr(job, "execution_mode", "") or "")
        == str(schedule.get("execution_mode") or "")
        and dict(getattr(job, "params_snapshot", None) or {})
        == dict(schedule.get("params_snapshot") or {})
        and str(getattr(job, "occurrence_key", "") or "") == occurrence.occurrence_key
        and dict(getattr(job, "identity_json", None) or {})
        == pinned_launch_identity(schedule, occurrence)
    )


class HostedJobSubmitter:
    """Production submitter: a due occurrence creates the pinned hosted job.

    It does **not** manufacture a proposal. The child - launched from this job by
    the supervisor's existing lifecycle - is what executes Python and decides what
    to propose. The occurrence's ``evaluation_id`` travels with the job as its
    bound evaluation identity, so the child's proposal must name it and the
    platform refuses a mismatch.

    Exactly one job per occurrence, under concurrency and after a lost response:
    ``create_job`` keys on ``occurrence_key`` (``uq_strategy_jobs_occurrence``) and
    returns the original row for an identical replay while refusing a different
    launch request for the same key.

    The launch and the occurrence decision are committed **together**
    (:meth:`submit_with_decision`), so a job can never survive an occurrence that
    another actor expired, and an actor whose decision claim was taken over
    cannot leave a job behind. Terminal conditions (a strategy that no longer
    exists, is disabled, or changed its account scope) raise
    :class:`ScheduleRefusal` and are recorded as the occurrence's reason;
    transient conditions raise :class:`ScheduleDeferred` and are retried.
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        repository: Any = None,
    ) -> None:
        if session_factory is not None:
            self.session_factory = session_factory
        if repository is None:
            if session_factory is None:
                from backend.app.database import SessionLocal

                session_factory = SessionLocal
                self.session_factory = session_factory
            from backend.strategies.repository import SqlAlchemyStrategyRepository

            repository = SqlAlchemyStrategyRepository(session_factory)
        self.repository = repository

    def existing_job(
        self, schedule: Mapping[str, Any], occurrence: Occurrence
    ) -> Optional[bool]:
        """Look up this occurrence's launch for lost-response recovery.

        ``True`` when a job under the key matches the pinned launch, ``False``
        when the key is held by a different launch, ``None`` when there is no job.
        """
        existing = self.repository.get_job_by_occurrence_key(occurrence.occurrence_key)
        if existing is None:
            return None
        return matches_pinned_launch(existing, schedule, occurrence)

    def finish_predecessor_continuation(self, *, owner_id: str, strategy_id: str) -> Optional[Dict[str, Any]]:
        """Best-effort automatic continuation for a blocked predecessor.

        The scheduled-job path shares the Run now path's rule: an eligible
        finished finite evaluation clears its own block so the next scheduled
        evaluation reads the same durable book. An ineligible predecessor is
        untouched and the occurrence still defers.
        """
        try:
            from backend.strategies.continuation import COMPLETION_UNKNOWN, ContinuationService

            service = ContinuationService(
                session_factory=self.session_factory, repository=self.repository
            )
            return service.attempt(
                owner_id=str(owner_id),
                strategy_id=str(strategy_id),
                completion_state=COMPLETION_UNKNOWN,
                actor_id="host:scheduler",
            )
        except Exception:  # noqa: BLE001 - never break the tick on a continuation attempt
            logger.exception(
                "scheduler_continuation_attempt_failed",
                extra={"strategy_id": str(strategy_id), "owner_id": str(owner_id)},
            )
            return None

    def __call__(
        self,
        schedule: Mapping[str, Any],
        occurrence: Occurrence,
        detail: Mapping[str, Any],
    ) -> bool:
        """Plain-callable compatibility: create the launch, commit it on its own."""
        outcome = self.submit_with_decision(schedule, occurrence, detail)
        return outcome in ("created", "replayed")

    # -- launchability ------------------------------------------------------

    def _strategy_for(self, schedule: Mapping[str, Any]) -> Any:
        """The schedule's strategy, or a terminal refusal naming why not.

        The schedule pins its launch inputs at creation time; the strategy is the
        authority for whether they are still launchable.
        """
        schedule_id = str(schedule.get("id") or "")
        strategy_id = str(schedule.get("strategy_id") or "")
        owner_id = str(schedule.get("owner_id") or "")
        strategy = self.repository.get_strategy(owner_id, strategy_id)
        if strategy is None:
            raise ScheduleRefusal(
                {"schedule_id": schedule_id, "strategy_id": strategy_id},
                reason_code="strategy_missing",
            )
        if str(strategy.status or "") != "active":
            raise ScheduleRefusal(
                {"schedule_id": schedule_id, "strategy_status": str(strategy.status or "")},
                reason_code="strategy_disabled",
            )
        scheduled_scope = str(schedule.get("account_scope") or "")
        if str(strategy.default_account_scope or "") != scheduled_scope:
            raise ScheduleRefusal(
                {
                    "schedule_id": schedule_id,
                    "scheduled_account_scope": scheduled_scope,
                    "strategy_account_scope": str(strategy.default_account_scope or ""),
                },
                reason_code="account_scope_changed",
            )
        return strategy

    def _create_pinned_job(
        self, schedule: Mapping[str, Any], occurrence: Occurrence, *, session: Any = None
    ) -> Any:
        """Insert the pinned job; ``session`` composes it into a caller's transaction."""
        from backend.strategies import repository as repository_module

        strategy_id = str(schedule.get("strategy_id") or "")
        owner_id = str(schedule.get("owner_id") or "")
        # Shared scheduled-job path: finish an eligible predecessor's continuation
        # proof first, so a healthy finite evaluation hands its held book to the
        # next scheduled evaluation (including after a host restart) instead of
        # deferring forever behind an unreconciled block.
        self.finish_predecessor_continuation(owner_id=owner_id, strategy_id=strategy_id)
        try:
            return self.repository.create_job(
                strategy_id=strategy_id,
                version_id=str(schedule.get("version_id") or ""),
                owner_id=owner_id,
                job_kind=str(schedule.get("job_kind") or "finite"),
                execution_mode=str(schedule.get("execution_mode") or "paper"),
                params=dict(schedule.get("params_snapshot") or {}),
                occurrence_key=occurrence.occurrence_key,
                identity=pinned_launch_identity(schedule, occurrence),
                session=session,
            )
        except repository_module.StrategyFenceError as exc:
            raise ScheduleDeferred(str(exc)) from exc
        except repository_module.StrategyConflict as exc:
            existing = self.repository.get_job_by_occurrence_key(occurrence.occurrence_key)
            if existing is None:
                raise ScheduleDeferred(str(exc)) from exc
            if not matches_pinned_launch(existing, schedule, occurrence):
                raise ScheduleRefusal(
                    {
                        "schedule_id": str(schedule.get("id") or ""),
                        "occurrence_key": occurrence.occurrence_key,
                        "owner_id": str(getattr(existing, "owner_id", "") or ""),
                        "strategy_id": str(getattr(existing, "strategy_id", "") or ""),
                    },
                    reason_code="occurrence_conflict",
                ) from exc
            return existing
        except repository_module.StrategyValidationError as exc:
            raise ScheduleRefusal(
                {"schedule_id": str(schedule.get("id") or ""), "reason": str(exc)},
                reason_code="launch_invalid",
            ) from exc
        except (
            repository_module.StrategyDisabled,
            repository_module.StrategyNotFound,
            repository_module.StrategyIdentityError,
            repository_module.StrategyIdempotencyConflict,
        ) as exc:
            raise ScheduleRefusal(
                {
                    "schedule_id": str(schedule.get("id") or ""),
                    "occurrence_key": occurrence.occurrence_key,
                },
                reason_code=_terminal_reason_for(exc),
            ) from exc

    # -- atomic launch + decision ------------------------------------------

    def submit_with_decision(
        self,
        schedule: Mapping[str, Any],
        occurrence: Occurrence,
        detail: Mapping[str, Any],
        *,
        session_factory: Any = None,
        decision: Any = None,
    ) -> str:
        """Create the launch and the occurrence decision in ONE transaction.

        ``decision(session)`` is the scheduler's ``pending -> fired``
        compare-and-set, already fenced on the decision claim it took. Ordering
        inside the single transaction: the strategy row lock taken by
        ``create_job`` -> the job insert -> the occurrence compare-and-set. If the
        claim was taken over (or the occurrence moved) the compare-and-set fails,
        the whole transaction rolls back, and the job row never exists.

        Returns ``"created"``, ``"replayed"`` (an identical launch already
        existed, and the occurrence is settled to that same decision) or
        ``"stale"`` (our claim was taken over; nothing was written).
        """
        self._strategy_for(schedule)
        factory = session_factory or getattr(self, "session_factory", None)
        if factory is None or decision is None:
            # A minimal repository double with no session factory: create the job
            # plainly. The scheduler's claim fence still protects the record.
            self._create_pinned_job(schedule, occurrence)
            return "created"
        session = factory()
        try:
            existing = self.repository.get_job_by_occurrence_key(
                occurrence.occurrence_key
            )
            if existing is not None:
                if not matches_pinned_launch(existing, schedule, occurrence):
                    raise ScheduleRefusal(
                        {
                            "schedule_id": str(schedule.get("id") or ""),
                            "occurrence_key": occurrence.occurrence_key,
                        },
                        reason_code="occurrence_conflict",
                    )
                won = bool(decision(session))
                if not won:
                    session.rollback()
                    return "stale"
                session.commit()
                return "replayed"
            self._create_pinned_job(schedule, occurrence, session=session)
            won = bool(decision(session))
            if not won:
                # Our claim is gone: nothing about this launch may survive.
                session.rollback()
                return "stale"
            session.commit()
            return "created"
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

def _terminal_reason_for(exc: Exception) -> str:
    from backend.strategies import repository as repository_module

    if isinstance(exc, repository_module.StrategyDisabled):
        return "strategy_disabled"
    if isinstance(exc, repository_module.StrategyNotFound):
        return "strategy_missing"
    if isinstance(exc, repository_module.StrategyIdentityError):
        return "version_mismatch"
    if isinstance(exc, repository_module.StrategyIdempotencyConflict):
        return "occurrence_conflict"
    return "launch_invalid"
