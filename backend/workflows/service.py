"""Evaluation service: observation -> rule evaluation -> atomic commit (Task 7).

``EvaluationService.handle_observation`` is the single commit point of the
alerts platform (spec v2 §7): predicate evaluation + trigger decision +
signal event + delivery fan-out + checkpoint save all share one database
transaction, so a crash or a lost checkpoint lease (E-3) rolls back the
whole evaluation.

Epoch / continuity semantics (spec §5.7, E-5, E-6):

- ``ltp`` clock: checkpoints are keyed by the observation's ``epoch_id``
  (the runtime issues a fresh epoch id per worker boot). A new epoch finds
  no checkpoint -> fresh state -> the first observation only initializes
  and never fires.
- ``candle_close`` clock: the runtime stamps completed candles with the
  stable ``"candle"`` epoch, so state survives restarts via the checkpoint.
  A bar arriving more than 2.5 expected intervals after the stored
  ``last_bar_ts`` is a feed gap: the rule gets fresh epoch semantics (the
  stale bar initializes state, never fires) and the caller sees the
  ``"feed_gap"`` suppression reason for health reporting.

Duplicate completed candles produce the same occurrence key; the repository's
unique constraint makes the second write a no-op (E-7). When the trigger is
``once`` and the rule completed, the subscription row is flipped to
``completed`` inside the same transaction.

``ensure_subscriptions`` materializes ``alert_subscriptions`` rows for an
activated revision's document (alerts x instruments) and is idempotent; the
worker calls it on every start so freshly activated workflows are picked up.

This module is import-safe without redis and has no network dependencies.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.alerts.engine import decide
from backend.alerts.predicates import Observation, evaluate_stage
from backend.workflows.models import AlertSpec, Stage, WorkflowDocument
from backend.workflows.parser import WorkflowParseError, parse_workflow_dict
from backend.workflows.repository import (
    ActiveSubscription,
    AlertSubscription,
    LeaseConflict,
    SqlAlchemyWorkflowRepository,
)

__all__ = ["HandleResult", "EvaluationService"]

logger = logging.getLogger(__name__)

# Expected bar spacing per timeframe (seconds) for candle gap detection.
TIMEFRAME_SECONDS: dict[str, int] = {
    "minute": 60,
    "3minute": 180,
    "5minute": 300,
    "10minute": 600,
    "15minute": 900,
    "30minute": 1800,
    "60minute": 3600,
    "day": 86400,
}

# Gap threshold multiplier: a bar later than 2.5 expected intervals is a gap.
GAP_MULTIPLIER = 2.5

# Channel resolver: turns (owner_id, channel names) into a ``{name: channel_id}``
# mapping. Names absent from the mapping are unresolved: the service skips
# (and logs) them; the worker counts them in its health counters.
ChannelResolver = Callable[[str, Sequence[str]], Dict[str, str]]


@dataclass(frozen=True)
class HandleResult:
    """Outcome of evaluating one observation for one subscription."""

    fired: bool                          # the rule fired (crossing / promoted match)
    emitted: bool                        # a signal event was committed
    suppression_reason: Optional[str]    # cooldown | already_fired | feed_gap |
                                         # lease_lost | duplicate_occurrence | ...
    rule_completed: bool                 # trigger == "once" and fired


_NOOP = HandleResult(fired=False, emitted=False, suppression_reason=None, rule_completed=False)


def _stage_current_value(stage: Stage, obs: Observation) -> Optional[float]:
    """The stage operand's current value (rearm input); observation fields only."""
    for cond in stage.conditions:
        left = cond.left
        if left.kind == "field" and left.name:
            value = getattr(obs, left.name, None)
            if value is not None:
                return float(value)
    return None


class EvaluationService:
    """Evaluates active alert subscriptions against market observations."""

    def __init__(
        self,
        workflow_repo: SqlAlchemyWorkflowRepository,
        session_factory: Callable[[], Session],
        *,
        clock: Optional[Callable[[], datetime]] = None,
        channel_resolver: Optional[ChannelResolver] = None,
    ) -> None:
        self.workflow_repo = workflow_repo
        self.session_factory = session_factory
        self.clock = clock
        self.channel_resolver = channel_resolver

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------

    def ensure_subscriptions(self, revision: Any, *, db: Optional[Session] = None) -> int:
        """Create ``alert_subscriptions`` rows for a revision's document.

        One row per (alert, instrument); rows that already exist are left
        alone, so calling this repeatedly (every worker start) is safe.
        Returns the number of rows created. ``revision`` is any object with
        ``id`` and ``document`` (a ``WorkflowRevision`` row).
        """
        try:
            document = parse_workflow_dict(revision.document)
        except (WorkflowParseError, ValueError, TypeError, AttributeError) as exc:
            logger.warning(
                "cannot materialize subscriptions for revision %s: %s",
                getattr(revision, "id", "?"), exc,
            )
            return 0

        owned = db is None
        session = db if db is not None else self.session_factory()
        try:
            existing = session.execute(
                select(AlertSubscription).where(
                    AlertSubscription.revision_id == revision.id
                )
            ).scalars().all()
            known = {(row.alert_id, row.instrument_key) for row in existing}
            created = 0
            for alert in document.alerts:
                for instrument in document.instruments:
                    key = (alert.id, instrument.key())
                    if key in known:
                        continue
                    session.add(
                        AlertSubscription(
                            id=str(uuid.uuid4()),
                            revision_id=revision.id,
                            alert_id=alert.id,
                            stage_id=alert.source,
                            instrument_symbol=instrument.symbol,
                            instrument_exchange=instrument.exchange,
                            instrument_key=instrument.key(),
                            trigger=alert.trigger,
                            config=self._alert_config(alert),
                            state="active",
                        )
                    )
                    known.add(key)
                    created += 1
            if owned:
                session.commit()
            return created
        except IntegrityError:
            # A concurrent creator won the unique (revision, alert, instrument)
            # key: everything needed already exists.
            if owned:
                session.rollback()
                return 0
            raise
        except Exception:
            if owned:
                session.rollback()
            raise
        finally:
            if owned:
                session.close()

    @staticmethod
    def _alert_config(alert: AlertSpec) -> dict:
        return {
            "cooldown_s": alert.cooldown_s,
            "rearm_level": alert.rearm_level,
            "rearm_direction": alert.rearm_direction,
            "reminder_interval_s": alert.reminder_interval_s,
            "notify_if_already_true": alert.notify_if_already_true,
            "expires_at": alert.expires_at,
            "channels": list(alert.channels),
            "message": alert.message,
        }

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------

    def handle_observation(
        self,
        sub: ActiveSubscription,
        obs: Observation,
        *,
        db: Optional[Session] = None,
    ) -> HandleResult:
        """Evaluate one observation for one active subscription.

        Runs predicate + trigger decision and commits signal event +
        deliveries + checkpoint in a single transaction. On a checkpoint
        lease conflict (another worker owns the subscription) the whole
        transaction is rolled back and ``lease_lost`` is reported.
        """
        document = self._parse_document(sub)
        if document is None:
            return _NOOP
        stage = next((s for s in document.stages if s.id == sub.stage_id), None)
        alert = next((a for a in document.alerts if a.id == sub.alert_id), None)
        if stage is None or alert is None:
            logger.warning(
                "subscription %s references missing stage/alert %s/%s in revision document",
                sub.id, sub.stage_id, sub.alert_id,
            )
            return _NOOP

        # Resolve channel names -> ids BEFORE opening the transaction (the
        # resolver may need its own session).
        channel_ids = self._resolve_channels(sub.owner_id, alert.channels)

        owned = db is None
        session = db if db is not None else self.session_factory()
        try:
            checkpoint = self.workflow_repo.load_checkpoint(
                sub.id, sub.instrument_key, obs.epoch_id, db=session,
            )
            if checkpoint is None:
                state, stored_epoch = {}, 0
            else:
                state, stored_epoch = checkpoint

            gap = False
            if stage.clock == "candle_close":
                gap = self._detect_gap(stage, state, obs)
                if gap:
                    state = {}  # new epoch semantics: nothing carries over

            pred = evaluate_stage(stage, obs, state)
            now = self.clock() if self.clock is not None else obs.ts
            # The engine chains on the predicate's OUTPUT state (pred.state
            # carries prev/baseline/... for this observation). The E-9
            # activation decision applies whenever the stored state has no
            # "initialized" marker (fresh checkpoint or post-gap reset).
            engine = decide(
                alert,
                fired=pred.fired,
                state=pred.state,
                now=now,
                session_active=True,
                current_value=_stage_current_value(stage, obs),
                already_true=pred.matched if not state.get("initialized") else None,
            )

            emitted = False
            if engine.emit:
                occurrence_key = (
                    f"{sub.workflow_id}:{sub.alert_id}:{sub.instrument_key}:"
                    f"{obs.epoch_id}:{obs.ts.isoformat()}"
                )
                evidence = {
                    **pred.evidence,
                    "epoch_id": obs.epoch_id,
                    "stage_id": sub.stage_id,
                    "timeframe": stage.timeframe,
                }
                try:
                    event = self.workflow_repo.record_signal(
                        sub.id,
                        occurrence_key,
                        fired_at=obs.ts,
                        evidence=evidence,
                        channel_ids=channel_ids,
                        db=session,
                    )
                except IntegrityError:
                    # E-2/E-7: a racing writer (or a re-delivered bar) already
                    # committed this occurrence key; the whole transaction
                    # rolls back and the loser skips without crashing.
                    session.rollback()
                    logger.info(
                        "occurrence %s already recorded for subscription %s; skipping",
                        occurrence_key, sub.id,
                    )
                    return HandleResult(
                        fired=bool(pred.fired),
                        emitted=False,
                        suppression_reason="duplicate_occurrence",
                        rule_completed=False,
                    )
                emitted = event is not None  # None: occurrence key already exists

            # Always persist the checkpoint (even for suppressed evaluations)
            # so state continuity survives restarts.
            new_state = dict(engine.new_state)
            new_state["epoch_id"] = obs.epoch_id
            if obs.final:
                new_state["last_bar_ts"] = obs.ts.isoformat()
            # non-final observations keep the stored last_bar_ts untouched

            try:
                self.workflow_repo.save_checkpoint(
                    sub.id,
                    sub.instrument_key,
                    obs.epoch_id,
                    new_state,
                    stored_epoch,
                    db=session,
                )
            except LeaseConflict:
                # Stale worker: the whole transaction (event + deliveries +
                # this checkpoint write) rolls back together.
                session.rollback()
                logger.info(
                    "checkpoint lease lost for subscription %s (%s/%s); evaluation skipped",
                    sub.id, sub.instrument_key, obs.epoch_id,
                )
                return HandleResult(
                    fired=bool(pred.fired or engine.emit),
                    emitted=False,
                    suppression_reason="lease_lost",
                    rule_completed=False,
                )

            if engine.rule_completed:
                # Repository is frozen; flip the lifecycle via a scoped UPDATE
                # inside the same transaction.
                session.execute(
                    update(AlertSubscription)
                    .where(AlertSubscription.id == sub.id)
                    .values(state="completed")
                )

            if owned:
                session.commit()

            if engine.emit:
                suppression_reason: Optional[str] = (
                    None if emitted else "duplicate_occurrence"
                )
            elif gap:
                suppression_reason = "feed_gap"
            else:
                suppression_reason = engine.suppression_reason

            return HandleResult(
                fired=bool(pred.fired or engine.emit),
                emitted=emitted,
                suppression_reason=suppression_reason,
                rule_completed=engine.rule_completed,
            )
        except Exception:
            if owned:
                session.rollback()
            raise
        finally:
            if owned:
                session.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _parse_document(self, sub: ActiveSubscription) -> Optional[WorkflowDocument]:
        try:
            return parse_workflow_dict(sub.document)
        except (WorkflowParseError, ValueError, TypeError) as exc:
            logger.warning(
                "cannot parse revision document for subscription %s: %s", sub.id, exc,
            )
            return None

    @staticmethod
    def _detect_gap(stage: Stage, state: dict, obs: Observation) -> bool:
        last_raw = state.get("last_bar_ts")
        if not last_raw:
            return False
        try:
            last_ts = datetime.fromisoformat(str(last_raw))
        except (TypeError, ValueError):
            return False
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=obs.ts.tzinfo)
        expected = TIMEFRAME_SECONDS.get(stage.timeframe or "", 60)
        elapsed = (obs.ts - last_ts).total_seconds()
        return elapsed > expected * GAP_MULTIPLIER

    def _resolve_channels(self, owner_id: str, names: Sequence[str]) -> list[str]:
        """Resolve channel names to ids before the transaction opens.

        Unresolved names are logged and skipped; when nothing resolves the
        event is still recorded with zero deliveries (history must exist;
        deliveries can be replayed manually).
        """
        names = list(names or ())
        if not names:
            return []
        if self.channel_resolver is None:
            return names  # names used as ids (single-repo test setups)
        mapping = self.channel_resolver(owner_id, names) or {}
        resolved = [mapping[name] for name in names if name in mapping]
        skipped = [name for name in names if name not in mapping]
        for name in skipped:
            logger.warning(
                "channel %r not resolvable for owner %s; event recorded without it",
                name, owner_id,
            )
        return resolved
