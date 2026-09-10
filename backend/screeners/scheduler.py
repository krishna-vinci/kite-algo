"""Screener scheduler (Phase 3 F9): due-occurrence execution + attachments.

Duties (one poll pass):

1. list active screener workflow revisions (active revision, un-archived
   workflow, document with a screener block);
2. compute each schedule's latest DUE bucket (IST-anchored, NSE-calendar
   gated, coalesced — E-19: only the latest due occurrence is ever run);
3. claim the occurrence (unique occurrence_key + lease; concurrent workers
   produce exactly one logical run, stale owners are fenced);
4. resolve universe membership, run the stored-data pipeline, evaluate
   attachments on COMPLETE runs, publish atomically.

Failure semantics: any pipeline error finalizes the run as ``failed`` with
the reason (visible, never silent); the claim's unique key prevents the same
occurrence from running twice within a lease, and a crashed execution is
recovered by lease takeover after expiry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from sqlalchemy import select

from backend.workflows.repository import Workflow, WorkflowRevision
from backend.workflows.screener_repository import (
    ScreenerRun,
    ScreenerRunRepository,
    record_attachment_event,
)

logger = logging.getLogger(__name__)

__all__ = ["ScreenerScheduler", "evaluate_attachments"]

_IST = timezone(timedelta(hours=5, minutes=30))


def _no_channels(owner_id: str, names):
    """Defensive default resolver: no channels resolve (events still record,
    deliveries are skipped). Production wiring passes the real resolver."""
    logger.warning("no channel resolver wired; screener attachment %s sends nothing", names)
    return {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScreenerScheduler:
    """Async loop executing due screener occurrences in the alerts worker."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        workflow_repo,
        run_repo: Optional[ScreenerRunRepository] = None,
        pipeline,
        universe_service=None,
        fundamentals_loader=None,
        session_gate: Optional[Callable[[datetime], Tuple[bool, str]]] = None,
        owner_id: str = "screener-scheduler",
        channel_resolver: Optional[Callable[[str, Sequence[str]], Dict[str, str]]] = None,
        poll_interval_s: float = 30.0,
        lease_ttl_s: float = 300.0,
        window_bars: int = 120,
        max_events_per_attachment: int = 100,
        health: Optional[dict] = None,
    ) -> None:
        self._sessions = session_factory
        self.workflow_repo = workflow_repo
        self.run_repo = run_repo or ScreenerRunRepository(session_factory)
        from backend.screeners.runner import ScreenerPipeline

        self.pipeline = pipeline or ScreenerPipeline(candle_history=None)
        self.universe_service = universe_service
        self.fundamentals_loader = fundamentals_loader
        self.session_gate = session_gate
        self.owner_id = owner_id
        self._channel_resolver = channel_resolver
        self.poll_interval_s = max(5.0, float(poll_interval_s))
        self.lease_ttl_s = max(30.0, float(lease_ttl_s))
        self.max_events_per_attachment = int(max_events_per_attachment)
        self.health = health if health is not None else {
            "runs_executed": 0,
            "runs_failed": 0,
            "attachment_events": 0,
            "claim_conflicts": 0,
            "last_pass_at": None,
            "last_error": None,
        }
        self._running = False

    # ------------------------------------------------------------------
    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.poll_once)
            except Exception:
                self.health["last_error"] = "poll_failed"
                logger.exception("screener scheduler pass failed")
            await asyncio.sleep(self.poll_interval_s)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    def poll_once(self, *, now: Optional[datetime] = None) -> int:
        """One scheduler pass; returns the number of runs executed."""
        timestamp = now or _utcnow()
        executed = 0
        for workflow, revision in self._active_screener_revisions():
            try:
                if self._execute_workflow(workflow, revision, timestamp):
                    executed += 1
            except Exception:
                self.health["last_error"] = "run_failed"
                logger.exception(
                    "screener run failed for workflow %s", workflow.id
                )
        self.health["last_pass_at"] = timestamp.isoformat()
        return executed

    # ------------------------------------------------------------------
    def _active_screener_revisions(self) -> List[Tuple[Workflow, WorkflowRevision]]:
        session = self._sessions()
        try:
            rows = session.execute(
                select(Workflow, WorkflowRevision)
                .join(
                    WorkflowRevision,
                    WorkflowRevision.workflow_id == Workflow.id,
                )
                .where(
                    WorkflowRevision.status == "active",
                    Workflow.archived_at.is_(None),
                )
            ).all()
            out = []
            for workflow, revision in rows:
                document = revision.document or {}
                if isinstance(document, dict) and document.get("screener"):
                    out.append((workflow, revision))
            return out
        finally:
            session.close()

    # ------------------------------------------------------------------
    def _execute_workflow(
        self, workflow: Workflow, revision: WorkflowRevision, now: datetime
    ) -> bool:
        from backend.workflows.parser import parse_workflow_dict

        try:
            document = parse_workflow_dict(revision.document)
        except Exception:
            logger.warning(
                "screener workflow %s revision %s no longer parses; skipping",
                workflow.id, revision.id,
            )
            return False
        screener = document.screener
        if screener is None:
            return False
        bucket = self._latest_bucket(screener.schedule, now)
        if bucket is None:
            return False
        occurrence_key = f"{workflow.id}:{int(bucket.timestamp())}"
        run = self.run_repo.claim_run(
            owner_id=workflow.owner_id,
            workflow_id=workflow.id,
            revision_id=revision.id,
            occurrence_key=occurrence_key,
            scheduled_for=bucket,
            lease_owner=self.owner_id,
            lease_ttl_s=self.lease_ttl_s,
            triggered_by="schedule",
            now=now,
        )
        if run is None:
            self.health["claim_conflicts"] += 1
            return False
        self._run_pipeline(workflow, revision, document, run, now)
        return True

    def _latest_bucket(self, schedule, now: datetime) -> Optional[datetime]:
        from backend.screeners.runner import compute_screener_bucket

        every_s = parse_duration_seconds(schedule.every)
        gate = None
        if self.session_gate is not None:
            gate = lambda at: self.session_gate(at)  # noqa: E731
        return compute_screener_bucket(every_s, schedule.at, now, session_gate=gate)

    # ------------------------------------------------------------------
    def execute_manual(
        self,
        workflow: Workflow,
        revision: WorkflowRevision,
        *,
        idempotency_key: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[ScreenerRun]:
        from backend.workflows.parser import parse_workflow_dict

        timestamp = now or _utcnow()
        document = parse_workflow_dict(revision.document)
        nonce = idempotency_key or uuid4().hex
        occurrence_key = f"{workflow.id}:manual:{nonce}"
        run = self.run_repo.claim_run(
            owner_id=workflow.owner_id,
            workflow_id=workflow.id,
            revision_id=revision.id,
            occurrence_key=occurrence_key,
            scheduled_for=timestamp,
            lease_owner=self.owner_id,
            lease_ttl_s=self.lease_ttl_s,
            triggered_by="manual",
            now=timestamp,
        )
        if run is None:
            # already finalized for this idempotency key
            return None
        self._run_pipeline(workflow, revision, document, run, timestamp)
        return self.run_repo.get_run(run.id)

    # ------------------------------------------------------------------
    def _run_pipeline(
        self,
        workflow: Workflow,
        revision: WorkflowRevision,
        document,
        run,
        now: datetime,
    ) -> None:
        members_by_key = self._resolve_universe(workflow.owner_id, document)
        universe_revision = members_by_key.get("__universe_revision__")
        member_keys = members_by_key.get("__members__", [])
        if document.universe is not None and not members_by_key.get("__resolution_ok__", True):
            self.health["runs_failed"] += 1
            published = self.run_repo.finalize_run(
                run.id,
                self.owner_id,
                status="failed",
                as_of=run.scheduled_for,
                coverage={"expected": len(member_keys), "universe_resolution": "failed"},
                data_freshness={},
                members=[],
                failure_reason="universe_resolution_failed",
                now=now,
            )
            if published:
                self.health["runs_executed"] += 1
            return
        context_loader = (
            self.fundamentals_loader.context_for if self.fundamentals_loader else None
        )
        try:
            outcome = self.pipeline.evaluate(
                document,
                member_keys,
                as_of=run.scheduled_for,
                context_loader=context_loader,
            )
        except Exception as exc:
            self.health["runs_failed"] += 1
            published = self.run_repo.finalize_run(
                run.id,
                self.owner_id,
                status="failed",
                as_of=run.scheduled_for,
                coverage={"expected": 0, "error": "pipeline_exception"},
                data_freshness={},
                members=[],
                failure_reason=str(exc)[:500],
                universe_revision=universe_revision,
                now=now,
            )
            if published:
                self.health["runs_executed"] += 1
            return

        results = outcome["members"]
        coverage = outcome["coverage"]
        attachment_summary: Dict[str, Any] = {}
        if outcome["status"] == "complete":
            attachment_summary = evaluate_attachments(
                workflow=workflow,
                revision=revision,
                document=document,
                run=run,
                results=results,
                run_repo=self.run_repo,
                session_factory=self._sessions,
                channel_resolver=self._channel_resolver or _no_channels,
                owner_id=workflow.owner_id,
                max_events=self.max_events_per_attachment,
                now=now,
            )
            self.health["attachment_events"] += attachment_summary.get("events", 0)
            if attachment_summary.get("suppressed_events"):
                coverage["attachment_events_suppressed"] = attachment_summary["suppressed_events"]
        member_payloads = [
            {
                "instrument_key": m.instrument_key,
                "passed": m.passed,
                "exclusion_reason": m.exclusion_reason,
                "values": m.values,
                "rank": m.rank,
                "score": m.score,
            }
            for m in results
        ]
        published = self.run_repo.finalize_run(
            run.id,
            self.owner_id,
            status=outcome["status"],
            as_of=run.scheduled_for,
            coverage=coverage,
            data_freshness=outcome["data_freshness"],
            members=member_payloads,
            universe_revision=universe_revision if isinstance(universe_revision, int) else None,
            now=now,
        )
        if published:
            self.health["runs_executed"] += 1
            if outcome["status"] == "complete":
                self._refresh_dependent_universes(workflow, now)

    def _refresh_dependent_universes(self, workflow: Workflow, now: datetime) -> None:
        """Re-materialize screener-sourced universes fed by this workflow.

        The evaluation worker consumes the latest persisted universe revision;
        without this refresh a new complete run would never reach downstream
        alerts. Best-effort: failures are logged and surfaced in health, and
        partial runs never refresh (E-18 — never replace a complete
        downstream universe with partial data)."""
        if self.universe_service is None:
            return
        try:
            session = self._sessions()
            try:
                from backend.workflows.universes import Universe

                dependents = session.execute(
                    select(Universe).where(
                        Universe.owner_id == workflow.owner_id,
                        Universe.kind == "screener",
                    )
                ).scalars().all()
            finally:
                session.close()
            for universe in dependents:
                source_ref = str((universe.source_config or {}).get("workflow") or "").strip()
                if source_ref not in (workflow.name, str(workflow.id)):
                    continue
                try:
                    self.universe_service.resolve_membership(
                        workflow.owner_id, universe.name
                    )
                except Exception:
                    logger.warning(
                        "dependent universe %s refresh failed after run of %s",
                        universe.name, workflow.name, exc_info=True,
                    )
        except Exception:
            logger.warning(
                "dependent universe refresh pass failed for %s", workflow.name,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    def _resolve_universe(self, owner_id: str, document) -> Dict[str, Any]:
        """Resolve the document universe expression to member keys.

        Returns {"__members__": [...], "__universe_revision__": int|None,
        "__resolution_ok__": bool}. A failed reference resolution marks the
        run failed with an explicit reason — recorded in history, never
        silently skipped and never presented as a successful empty scan.
        """
        members: Set[str] = {str(inst.key()) for inst in document.instruments}
        universe_revision = None
        resolution_ok = True
        if document.universe is not None:
            if self.universe_service is None:
                resolution_ok = False
            else:
                for ref in document.universe.refs:
                    resolved = self._resolve_ref(owner_id, ref)
                    if resolved is None:
                        resolution_ok = False
                        continue
                    revision, keys = resolved
                    if isinstance(revision, int):
                        universe_revision = max(universe_revision or 0, revision)
                    members |= keys
                for ref in document.universe.intersect:
                    resolved = self._resolve_ref(owner_id, ref)
                    if resolved is None:
                        resolution_ok = False
                        continue
                    revision, keys = resolved
                    if isinstance(revision, int):
                        universe_revision = max(universe_revision or 0, revision)
                    members &= keys
                for ref in document.universe.exclude:
                    resolved = self._resolve_ref(owner_id, ref)
                    if resolved is None:
                        resolution_ok = False
                        continue
                    _revision, keys = resolved
                    members -= keys
        return {
            "__members__": sorted(members),
            "__universe_revision__": universe_revision,
            "__resolution_ok__": resolution_ok,
        }

    def _resolve_ref(self, owner_id: str, ref) -> Optional[Tuple[Optional[int], set]]:
        try:
            if ref.kind in ("universe", "watchlist"):
                latest = self.universe_service.latest_revision(owner_id, ref.name)
                if latest is None:
                    return None
                return int(latest.get("revision") or 0) or None, set(latest.get("members") or ())
            preview = self.universe_service.preview_membership(
                owner_id, "index", {"source_list": ref.name}
            )
            return None, set(preview.get("members") or ())
        except Exception:
            logger.warning("screener universe ref %s failed", ref.name, exc_info=True)
            return None


# ---------------------------------------------------------------------------
# attachments (Phase 3B)
# ---------------------------------------------------------------------------


def evaluate_attachments(
    *,
    workflow: Workflow,
    revision: WorkflowRevision,
    document,
    run,
    results: Sequence,
    run_repo: ScreenerRunRepository,
    session_factory: Callable[[], Any],
    channel_resolver: Callable[[str, Sequence[str]], Dict[str, str]],
    owner_id: str,
    max_events: int = 100,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Entry/exit/top-N/rank-delta attachment evaluation for a COMPLETE run.

    Baseline semantics: the first complete run of a (revision, attachment)
    initializes state silently unless ``initial_match`` is set. Partial runs
    never reach this function (no exits, no baseline advance — E-18).

    Hysteresis: ``top_n`` enters at rank <= entry_rank and only exits when
    rank > exit_rank (entry_rank < exit_rank by validation, E-17);
    ``entry``/``exit`` triggers use ``exit_after`` consecutive absences
    (default 1). ``rank_delta`` fires on |current_rank - last_rank| >=
    threshold using the previous complete run's rank (a distinct concept
    from hysteresis bands — it compares ranks, it does not gate presence).
    """
    timestamp = now or _utcnow()
    summary: Dict[str, Any] = {"events": 0, "suppressed_events": 0}
    if document.screener is None:
        return summary
    screener_name = document.name
    for attachment in document.screener.attachments:
        states = run_repo.attachment_states(
            owner_id, workflow.id, revision.id, attachment.id
        )
        baseline = not states
        current = {m.instrument_key: m for m in results}
        events: List[Tuple[str, dict]] = []
        state_updates: List[Tuple[str, bool, Optional[int], int]] = []

        for key, member in sorted(current.items()):
            previous = states.get(key)
            prev_rank = previous.last_rank if previous is not None else None
            was_present = bool(previous.present) if previous is not None else False

            if attachment.trigger == "top_n":
                enters = member.passed and member.rank is not None and member.rank <= (attachment.entry_rank or 0)
                if was_present and (not member.passed or member.rank is None or member.rank > (attachment.exit_rank or 0)):
                    # still inside the hysteresis band: neither exit nor entry
                    stays = member.passed and member.rank is not None and member.rank <= (attachment.exit_rank or 0)
                    if stays:
                        state_updates.append((key, True, member.rank, 0))
                        continue
                    state_updates.append((key, False, member.rank, 0))
                    events.append((key, {"action": "exit", "rank": member.rank, "prev_rank": prev_rank}))
                    continue
                if enters and not was_present:
                    state_updates.append((key, True, member.rank, 0))
                    events.append((key, {"action": "entry", "rank": member.rank, "prev_rank": prev_rank}))
                    continue
                state_updates.append((key, bool(enters or was_present), member.rank, 0))
                continue

            if attachment.trigger in ("entry", "exit"):
                present = bool(member.passed)
                absent_streak = 0 if present else ((previous.consecutive_absent if previous else 0) + 1)
                threshold = attachment.exit_after or 1
                if present and not was_present:
                    events.append((key, {"action": "entry", "rank": member.rank, "prev_rank": prev_rank}))
                if was_present and not present and absent_streak >= threshold:
                    events.append((key, {"action": "exit", "rank": member.rank, "prev_rank": prev_rank}))
                state_updates.append((key, present, member.rank, absent_streak))
                continue

            if attachment.trigger == "rank_delta":
                if member.passed and member.rank is not None and prev_rank is not None:
                    delta = abs(member.rank - prev_rank)
                    if delta >= (attachment.rank_delta or 0):
                        events.append((
                            key,
                            {
                                "action": "rank_change",
                                "rank": member.rank,
                                "prev_rank": prev_rank,
                                "delta": delta,
                                "direction": "up" if member.rank < prev_rank else "down",
                            },
                        ))
                state_updates.append((key, bool(member.passed), member.rank, 0))
                continue

        # instruments present in prior state but absent from this run's
        # results (universe departure): they cannot be ranked any more —
        # treat as absent for entry/exit triggers, exited for top_n bands.
        for key in sorted(set(states) - set(current)):
            previous = states[key]
            if attachment.trigger == "top_n":
                if previous.present:
                    # a departed instrument can no longer hold a rank band
                    events.append((key, {"action": "exit", "rank": None, "prev_rank": previous.last_rank}))
                    state_updates.append((key, False, None, 0))
                continue
            if attachment.trigger in ("entry", "exit"):
                # streak advances once per complete run; the exit fires at
                # the exact crossing (streak == threshold), never repeats
                streak = (previous.consecutive_absent or 0) + 1
                threshold = attachment.exit_after or 1
                if streak == threshold:
                    events.append((key, {"action": "exit", "rank": None, "prev_rank": previous.last_rank}))
                state_updates.append((key, False, None, streak))

        if baseline and not attachment.initial_match:
            # first complete run: initialize state, notify nothing
            summary["events"] += 0
            state_updates = [
                (key, bool(current[key].passed), current[key].rank, 0)
                for key in current
            ]
            events = []

        emitted = 0
        suppressed = 0
        for key, payload in events:
            if emitted >= max_events:
                suppressed += 1
                continue
            member = current.get(key)
            member_values = dict(member.values) if member is not None else {}
            channel_ids = channel_resolver(owner_id, list(attachment.channels)) or {}
            evidence = {
                "screener": screener_name,
                "attachment_id": attachment.id,
                "trigger": attachment.trigger,
                "action": payload.get("action"),
                "instrument_key": key,
                "rank": payload.get("rank"),
                "prev_rank": payload.get("prev_rank"),
                "rank_delta": payload.get("delta"),
                "direction": payload.get("direction"),
                "run_id": run.id,
                "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
                "message": attachment.message,
                "values": {k: v for k, v in member_values.items() if k in ("close", "change_pct", "turnover", "score", "candle_ts")},
                "message_kind": "screener_attachment",
            }
            occurrence_key = (
                f"{workflow.id}:{revision.id}:{attachment.id}:{run.id}:{key}"
            )
            event = record_attachment_event(
                session_factory,
                workflow_id=workflow.id,
                occurrence_key=occurrence_key,
                fired_at=run.scheduled_for or timestamp,
                evidence=evidence,
                channel_ids=list(channel_ids.values()),
                now=timestamp,
            )
            if event is not None:
                summary["events"] += 1
            emitted += 1
        summary["suppressed_events"] += suppressed

        for key, present, rank, absent_streak in state_updates:
            run_repo.upsert_attachment_state(
                owner_id=owner_id,
                workflow_id=workflow.id,
                revision_id=revision.id,
                attachment_id=attachment.id,
                instrument_key=key,
                present=present,
                run_id=run.id,
                rank=rank,
                consecutive_absent=absent_streak,
                now=timestamp,
            )
    return summary


def parse_duration_seconds(value: str) -> int:
    """Schedule durations were parsed/validated by the workflow parser and are
    stored normalized as ``<seconds>s``."""
    text = str(value).strip()
    if text.endswith("s") and text[:-1].isdigit():
        return int(text[:-1])
    if text.isdigit():
        return int(text)
    raise ValueError(f"unsupported schedule duration {value!r}")
