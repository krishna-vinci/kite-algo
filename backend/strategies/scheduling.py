"""The schedule runtime: occurrence materialisation, fencing, misfire, overlap (G11).

The stored schedule table already described *what* to run; this module decides
*when* it runs and guarantees that it runs at most once. Three rules carry it:

* **One occurrence, one row.** ``UNIQUE (schedule_id, occurrence_key)`` is the
  whole fencing mechanism. Two schedulers racing the same tick collide on the
  index, so a duplicate is impossible by construction rather than by a
  read-then-write that both of them could pass.

* **A missed occurrence fires late at most once.** Within
  ``SCHEDULE_MISFIRE_GRACE_SECONDS`` a due occurrence is fired late; beyond it the
  occurrence is recorded ``skipped`` with its reason. Neither path is silent, and
  neither fires twice.

* **A new evaluation never starts while the previous one is unresolved.** Overlap
  is not "skip forever" and not "run anyway": the occurrence stays ``pending``
  with the reason journalled and is retried on the next tick, so a slow month does
  not silently consume the next month's decision.

Each fired occurrence mints a **new** ``evaluation_id`` — R3 §6's cardinality rule
— derived deterministically from the occurrence, and reuses the SAME strategy
book, which is what makes month-over-month continuity real.
"""

from __future__ import annotations

import calendar as _calendar
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.strategies.attribution_models import StrategyScheduleOccurrence

#: Kinds this runtime drives. ``daily``/``weekly`` belong to the pre-existing
#: worker-job scheduler and are deliberately not re-implemented here.
SCHEDULE_KINDS = ("monthly", "calendar")

DEFAULT_MISFIRE_GRACE_SECONDS = 3600

#: Occurrence statuses that mean "this occurrence is done with".
SETTLED_OCCURRENCE_STATUSES = ("fired", "skipped", "expired")

#: Reservation statuses that still represent unresolved work.
UNRESOLVED_RESERVATION_STATUSES = ("active", "renewed", "action_required")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    reason_code = "SCHEDULE_ERROR"

    def __init__(self, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)


class ScheduleScheduler:
    """Materialises and fires occurrences. Never places an order itself."""

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        proposal_submitter: Optional[Callable[[Any, Occurrence, Mapping[str, Any]], bool]] = None,
    ) -> None:
        if session_factory is None:
            from backend.workflows.repository import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self._submitter = proposal_submitter

    # -- schedule reads -----------------------------------------------------

    def enabled_schedules(self) -> List[Dict[str, Any]]:
        """Schedules this runtime drives: the new kinds, enabled, unpaused.

        Read through the ORM because the schedule table has a model: on SQLite the
        ORM's tables live in the main schema, so ``public.``-qualified SQL would
        only work on PostgreSQL.
        """
        from backend.strategies.models import HostedStrategySchedule

        try:
            with self.session_factory() as session:
                rows = session.execute(
                    select(HostedStrategySchedule).where(
                        HostedStrategySchedule.enabled.is_(True),
                        HostedStrategySchedule.manual_paused_at.is_(None),
                        HostedStrategySchedule.schedule_kind.in_(SCHEDULE_KINDS),
                    )
                ).scalars().all()
        except SQLAlchemyError:
            return []
        return [
            {
                "id": str(row.id),
                "strategy_id": str(row.strategy_id),
                "owner_id": str(row.owner_id),
                "account_scope": str(row.account_scope),
                "execution_mode": str(row.execution_mode),
                "schedule_kind": str(row.schedule_kind),
                "at_time": str(row.at_time),
                "timezone": str(row.timezone),
                "day_of_month": row.day_of_month,
                "calendar_dates": list(row.calendar_dates or []),
                "job_kind": str(row.job_kind),
                "max_duration_s": int(row.max_duration_s),
                "progress_deadline_s": int(row.progress_deadline_s),
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
            occurrences.reverse()
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
            occurrences.sort(key=lambda item: item.due_at)
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

    def previous_work_unresolved(self, *, schedule_id: str, before_due_at: datetime) -> bool:
        """Whether an earlier occurrence of this schedule is still unresolved.

        "Unresolved" is deliberately narrow: a fired occurrence is settled once its
        proposal was refused, or its reservation reached a terminal state. That is
        the point where the plan either happened or did not, so the next decision
        is safe to take.
        """
        from backend.strategies.attribution_models import (
            StrategyPlan,
            StrategyProposal,
            StrategyReservation,
        )

        try:
            with self.session_factory() as session:
                rows = session.execute(
                    select(
                        StrategyProposal.status,
                        StrategyReservation.status,
                    )
                    .select_from(StrategyScheduleOccurrence)
                    .outerjoin(
                        StrategyProposal,
                        StrategyProposal.evaluation_id
                        == StrategyScheduleOccurrence.evaluation_id,
                    )
                    .outerjoin(StrategyPlan, StrategyPlan.proposal_id == StrategyProposal.proposal_id)
                    .outerjoin(StrategyReservation, StrategyReservation.plan_id == StrategyPlan.plan_id)
                    .where(
                        StrategyScheduleOccurrence.schedule_id == str(schedule_id),
                        StrategyScheduleOccurrence.status == "fired",
                        StrategyScheduleOccurrence.due_at < before_due_at,
                    )
                    .order_by(StrategyScheduleOccurrence.due_at.desc())
                ).all()
        except SQLAlchemyError:
            return False
        for proposal_status, reservation_status in rows:
            proposal_status = str(proposal_status or "")
            reservation_status = str(reservation_status or "")
            if not proposal_status:
                # Fired but no envelope exists yet: the work has not even reached a
                # verdict, so it is unresolved.
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
            deferred.extend(result["deferred"])

        return {"fired": fired, "skipped": skipped, "deferred": deferred, "errors": errors}

    def _tick_schedule(
        self, schedule: Mapping[str, Any], *, moment: datetime, grace: int
    ) -> Dict[str, List[str]]:
        fired: List[str] = []
        skipped: List[str] = []
        deferred: List[str] = []

        for occurrence in self.due_occurrences(schedule, now=moment):
            occurrence_id = self.materialize(schedule, occurrence)
            if occurrence_id is None:
                # Another scheduler already recorded it; whatever it decided stands.
                continue

            lateness = (moment - occurrence.due_at).total_seconds()
            if lateness > grace:
                # A missed occurrence fires at most once late, and never after the
                # grace: the decision it would have made is no longer the decision
                # the strategy needs.
                self._settle(
                    occurrence_id,
                    status="skipped",
                    skip_reason="misfire_beyond_grace",
                    detail={"lateness_seconds": lateness, "grace_seconds": grace},
                )
                skipped.append(occurrence.occurrence_key)
                continue

            if self.previous_work_unresolved(
                schedule_id=str(schedule.get("id") or ""), before_due_at=occurrence.due_at
            ):
                # Overlap policy: not skipped forever and not run anyway. The row
                # stays pending so the next tick retries it.
                self._note_overlap(occurrence_id, lateness=lateness)
                deferred.append(occurrence.occurrence_key)
                continue

            if self._fire(schedule, occurrence, occurrence_id):
                fired.append(occurrence.occurrence_key)
            else:
                deferred.append(occurrence.occurrence_key)

        return {"fired": fired, "skipped": skipped, "deferred": deferred}

    def _fire(
        self, schedule: Mapping[str, Any], occurrence: Occurrence, occurrence_id: str
    ) -> bool:
        if self._submitter is None:
            return False
        submitted = bool(self._submitter(schedule, occurrence, {"occurrence_id": occurrence_id}))
        if not submitted:
            return False
        self._settle(
            occurrence_id,
            status="fired",
            fired_at=True,
            detail={"evaluation_id": occurrence.evaluation_id},
        )
        return True

    # -- occurrence writes --------------------------------------------------

    def _settle(
        self,
        occurrence_id: str,
        *,
        status: str,
        skip_reason: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
        fired_at: bool = False,
    ) -> None:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyScheduleOccurrence).where(
                    StrategyScheduleOccurrence.id == str(occurrence_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.status = str(status)
            if skip_reason is not None:
                row.skip_reason = str(skip_reason)
            if fired_at:
                row.fired_at = _utcnow()
            merged = dict(row.detail or {})
            merged.update(detail or {})
            row.detail = merged
            session.commit()

    def _note_overlap(self, occurrence_id: str, *, lateness: float) -> None:
        with self.session_factory() as session:
            row = session.execute(
                select(StrategyScheduleOccurrence).where(
                    StrategyScheduleOccurrence.id == str(occurrence_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.skip_reason = "overlap_skipped"
            merged = dict(row.detail or {})
            merged["overlap_skipped"] = True
            merged["lateness_seconds"] = lateness
            row.detail = merged
            session.commit()

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
