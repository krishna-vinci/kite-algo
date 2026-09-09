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
  A bar arriving more than 1.5 expected intervals after the stored
  ``last_bar_ts`` is a feed gap: the rule gets fresh epoch semantics (the
  stale bar initializes state, never fires) and the caller sees the
  ``"feed_gap"`` suppression reason for health reporting.

Duplicate completed candles produce the same occurrence key; the repository's
unique constraint makes the second write a no-op (E-7). Stale bars arriving
AFTER newer state (``ts <= state["last_bar_ts"]`` for ``candle_close`` rules)
are ignored entirely: they are never re-processed against newer state.

Warmup (``allow_emit=False``) runs predicates + engine fully and saves the
checkpoint so the first live bar can fire on a real crossing, but never
records a signal event or advances subscription lifecycle; a would-be emit
is reported with the ``"warmup"`` suppression reason.

Session identity: a contextual ``session_provider(session_name,
instrument_key, obs_ts)`` returns ``(session_active, session_id)`` or
``None``; timestamp-only providers remain supported for isolated callers.
No provider falls back to ``(True, observation-date-in-IST)``.
``once_per_session`` alerts re-arm when the session id changes. Every
suppression is logged at INFO (rule/alert id, instrument, reason) so audit can
see why an alert is silent.

``ensure_subscriptions`` materializes ``alert_subscriptions`` rows for an
activated revision's document (alerts x instruments) and is idempotent; the
worker calls it on every start so freshly activated workflows are picked up.

This module is import-safe without redis and has no network dependencies.
"""

from __future__ import annotations

import logging
import hashlib
import inspect
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

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

# Gap threshold multiplier: a bar later than 1.5 expected intervals is a gap.
GAP_MULTIPLIER = 1.5

# Channel resolver: turns (owner_id, channel names) into a ``{name: channel_id}``
# mapping. Names absent from the mapping are unresolved: the service skips
# (and logs) them; the worker counts them in its health counters.
ChannelResolver = Callable[[str, Sequence[str]], Dict[str, str]]

# Session provider: maps workflow session + instrument + observation timestamp
# to ``(session_active, session_id)`` or ``None`` (caller falls back to
# ``(True, observation-date-in-IST)``). Timestamp-only providers remain
# supported for isolated callers and older tests.
SessionProvider = Callable[..., Optional[Tuple[bool, str]]]

_IST = ZoneInfo("Asia/Kolkata")


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
        session_provider: Optional[SessionProvider] = None,
        owner_id: Optional[str] = None,
        ownership_lease_s: float = 120.0,
    ) -> None:
        self.workflow_repo = workflow_repo
        self.session_factory = session_factory
        self.clock = clock
        self.channel_resolver = channel_resolver
        self.session_provider = session_provider
        self._session_provider_with_context = self._supports_session_context(session_provider)
        self.owner_id = owner_id or f"evaluation-worker:{uuid.uuid4()}"
        self.ownership_lease_s = max(1.0, float(ownership_lease_s))

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
            rebound = 0
            for alert in document.alerts:
                for instrument in document.instruments:
                    key = (alert.id, instrument.key())
                    if key in known:
                        # C6: existing subscriptions receive a controlled
                        # binding when catalog data becomes available, or when
                        # the mapping/generation moved; metadata is not only
                        # populated on newly created rows.
                        row = next(
                            (r for r in existing if r.alert_id == alert.id and r.instrument_key == instrument.key()),
                            None,
                        )
                        if row is not None:
                            binding = self._resolve_catalog_binding(instrument.key(), session)
                            if binding is not None and binding != (row.config or {}).get("instrument_binding"):
                                config = dict(row.config or {})
                                config["instrument_binding"] = binding
                                row.config = config
                                rebound += 1
                        continue
                    config = self._alert_config(alert)
                    binding = self._resolve_catalog_binding(instrument.key(), session)
                    if binding is not None:
                        config["instrument_binding"] = binding
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
                            config=config,
                            state="active",
                        )
                    )
                    known.add(key)
                    created += 1
            if owned:
                session.commit()
            if rebound:
                logger.info(
                    "refreshed catalog binding on %d existing subscription(s) for revision %s",
                    rebound, revision.id,
                )
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

    @staticmethod
    def _resolve_catalog_binding(instrument_key: str, session: Session) -> Optional[dict]:
        """Persist catalog provenance when the catalog migration is present.

        Alert tests and pre-migration deployments continue to materialize
        subscriptions without a binding; the worker resolves them on startup.
        """
        try:
            from backend.broker_api.instruments.catalog import (
                CatalogUnavailableError,
                InstrumentCatalog,
                InstrumentNotFoundError,
            )

            descriptor = InstrumentCatalog(db=session).resolve_public_key(instrument_key)
            return {
                "instrument_id": descriptor.instrument_id,
                "public_key": descriptor.public_key,
                "broker": descriptor.broker,
                "broker_token": descriptor.broker_token,
                "catalog_generation": descriptor.catalog_generation,
                "lifecycle_status": descriptor.lifecycle_status,
            }
        except (CatalogUnavailableError, InstrumentNotFoundError):
            return None

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------

    def handle_observation(
        self,
        sub: ActiveSubscription,
        obs: Observation,
        *,
        db: Optional[Session] = None,
        allow_emit: bool = True,
        context: Optional[dict] = None,
    ) -> HandleResult:
        """Evaluate one observation for one active subscription.

        Runs predicate + trigger decision and commits signal event +
        deliveries + checkpoint in a single transaction. On a checkpoint
        lease conflict (another worker owns the subscription) the whole
        transaction is rolled back and ``lease_lost`` is reported.

        ``allow_emit=False`` (warmup): predicates + engine run fully and the
        checkpoint is saved, but no signal event is recorded and the
        subscription lifecycle never advances; a would-be emit is reported
        as ``suppression_reason="warmup"``.

        ``context``: optional mapping (e.g. ``prev_day_high``/``prev_day_low``
        floats) passed through to the predicates for context-resolved levels.
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
            ownership_now = self.clock() if self.clock is not None else obs.ts
            owner_epoch = self.workflow_repo.claim_evaluation(
                sub.id,
                sub.instrument_key,
                self.owner_id,
                lease_seconds=self.ownership_lease_s,
                now=ownership_now,
                db=session,
            )
            if owner_epoch is None:
                self._log_suppression(sub, "not_owner")
                return HandleResult(False, False, "not_owner", False)

            checkpoint = self.workflow_repo.load_checkpoint(
                sub.id, sub.instrument_key, obs.epoch_id, db=session,
            )
            if checkpoint is None and stage.clock == "ltp":
                latest = self.workflow_repo.load_latest_checkpoint(
                    sub.id, sub.instrument_key, db=session,
                )
                if latest is not None:
                    _previous_epoch, latest_state, latest_owner_epoch = latest
                    checkpoint = (latest_state, latest_owner_epoch)
            if checkpoint is None:
                state, stored_epoch = {}, 0
            else:
                state, stored_epoch = checkpoint

            observation_epoch_changed = bool(
                state.get("epoch_id") is not None
                and state.get("epoch_id") != obs.epoch_id
            )

            correction = False
            if stage.clock == "candle_close" and self._is_correction(state, obs):
                prior_state = state.get("last_bar_state")
                if isinstance(prior_state, dict):
                    state = dict(prior_state)
                    correction = True
                else:
                    self._log_suppression(sub, "stale_bar")
                    return HandleResult(False, False, "stale_bar", False)

            # A bar at or before the stored last_bar_ts was already processed.
            # Exact duplicate payloads are ignored; a changed same-timestamp
            # final bar takes the correction path above and recomputes from the
            # checkpoint immediately before the corrected bar.
            if stage.clock == "candle_close" and self._is_stale_bar(state, obs) and not correction:
                self._log_suppression(sub, "stale_bar")
                return HandleResult(
                    fired=False,
                    emitted=False,
                    suppression_reason="stale_bar",
                    rule_completed=False,
                )

            gap = False
            if stage.clock == "candle_close":
                gap = self._detect_gap(stage, state, obs)
                if gap:
                    state = self._reset_observation_state(state)

            # LTP worker epochs reset crossing/baseline continuity but retain
            # durable trigger bookkeeping (once/session/cooldown/rearm). The
            # predicate layer clears its condition-local epoch keys below.
            if stage.clock == "ltp" and observation_epoch_changed:
                state = dict(state)

            session_active, session_id = self._resolve_session(sub, document, obs)

            pred = evaluate_stage(stage, obs, state, context)
            engine_state = dict(pred.state)
            if stage.clock == "ltp" and observation_epoch_changed:
                # Make the first observation of a new epoch pass through the
                # activation guard without erasing fired_once/last_session or
                # other durable trigger bookkeeping.
                engine_state.pop("initialized", None)
            now = self.clock() if self.clock is not None else obs.ts
            # The engine chains on the predicate's OUTPUT state (pred.state
            # carries per-condition prev/baseline/... for this observation).
            # The E-9 activation decision applies whenever the stored state
            # has no "initialized" marker (fresh checkpoint or post-gap reset).
            engine = decide(
                alert,
                fired=pred.fired,
                state=engine_state,
                now=now,
                session_active=session_active,
                current_value=_stage_current_value(stage, obs),
                already_true=(
                    pred.matched
                    if observation_epoch_changed or not state.get("initialized")
                    else None
                ),
                session_id=session_id,
                matched=pred.matched,
            )

            emitted = False
            event = None
            correction_audited = False
            base_occurrence_key = (
                f"{sub.workflow_id}:{sub.revision_id}:{sub.id}:"
                f"{sub.alert_id}:{sub.instrument_key}:{obs.ts.isoformat()}"
            )
            evidence = {
                **pred.evidence,
                "epoch_id": obs.epoch_id,
                "stage_id": sub.stage_id,
                "timeframe": stage.timeframe,
            }
            # C6: every event carries the binding in force at evaluation time.
            # Rebinding later must not overwrite the meaning of old events —
            # this snapshot is copied into the immutable event row.
            binding = (sub.config or {}).get("instrument_binding")
            if isinstance(binding, dict) and binding:
                evidence["instrument_binding"] = dict(binding)
            if engine.emit and allow_emit:
                self.workflow_repo.assert_evaluation_owner(
                    sub.id,
                    sub.instrument_key,
                    self.owner_id,
                    owner_epoch,
                    now=ownership_now,
                    db=session,
                )
                occurrence_key = base_occurrence_key
                existing_event = self.workflow_repo.get_signal_by_occurrence(
                    occurrence_key, db=session
                )
                if existing_event is None and stage.clock == "candle_close":
                    # Compatibility with Phase 1 rows written before the
                    # revision-aware occurrence contract landed. New writes
                    # always use the revision/subscription key above.
                    legacy_key = (
                        f"{sub.workflow_id}:{sub.alert_id}:{sub.instrument_key}:"
                        f"candle:{obs.ts.isoformat()}"
                    )
                    existing_event = self.workflow_repo.get_signal_by_occurrence(
                        legacy_key, db=session
                    )
                try:
                    if existing_event is None:
                        with session.begin_nested():
                            event = self.workflow_repo.record_signal(
                                sub.id,
                                occurrence_key,
                                fired_at=obs.ts,
                                evidence=evidence,
                                channel_ids=channel_ids,
                                now=now,
                                db=session,
                            )
                    else:
                        event = None
                except IntegrityError:
                    # E-2/E-7: a racing writer (or a re-delivered bar) already
                    # committed this occurrence key; the whole transaction
                    # rolls back and the loser skips without crashing.
                    logger.info(
                        "occurrence %s already recorded for subscription %s; skipping",
                        occurrence_key, sub.id,
                    )
                    self._log_suppression(sub, "duplicate_occurrence")
                    event = None
                    existing_event = True
                if correction and existing_event is not None:
                    correction_audited = self._record_correction_notice(
                        sub, obs, base_occurrence_key, evidence, now, session
                    )
                elif existing_event is not None:
                    self._log_suppression(sub, "duplicate_occurrence")
                    return HandleResult(
                        fired=bool(pred.fired),
                        emitted=False,
                        suppression_reason="duplicate_occurrence",
                        rule_completed=False,
                    )
                emitted = event is not None  # None: occurrence key already exists

            # A correction can legitimately remove the crossing that caused
            # the original event. It still needs an auditable, silent
            # correction record; the `engine.emit` branch above only handles
            # corrections that remain trigger-eligible after recomputation.
            if correction and allow_emit and not correction_audited and event is None:
                existing_event = self.workflow_repo.get_signal_by_occurrence(
                    base_occurrence_key, db=session
                )
                if existing_event is None and stage.clock == "candle_close":
                    legacy_key = (
                        f"{sub.workflow_id}:{sub.alert_id}:{sub.instrument_key}:"
                        f"candle:{obs.ts.isoformat()}"
                    )
                    existing_event = self.workflow_repo.get_signal_by_occurrence(
                        legacy_key, db=session
                    )
                if existing_event is not None:
                    self.workflow_repo.assert_evaluation_owner(
                        sub.id,
                        sub.instrument_key,
                        self.owner_id,
                        owner_epoch,
                        now=ownership_now,
                        db=session,
                    )
                    correction_audited = self._record_correction_notice(
                        sub, obs, base_occurrence_key, evidence, now, session
                    )

            # Always persist the checkpoint (even for suppressed evaluations)
            # so state continuity survives restarts.
            new_state = dict(engine.new_state)
            if not allow_emit:
                # Warmup replays history to establish predicate continuity
                # (prev values, baselines). A crossing that happened before
                # activation must not consume the alert's live lifecycle
                # (once-completion, rearm, cooldown, reminders).
                for key in ("fired_once", "last_session", "last_emitted_ts",
                            "cooldown_until", "armed"):
                    new_state.pop(key, None)
            new_state["epoch_id"] = obs.epoch_id
            if obs.final:
                prior_bar_state = dict(state)
                prior_bar_state.pop("last_bar_state", None)
                prior_bar_state.pop("last_bar_payload", None)
                new_state["last_bar_state"] = prior_bar_state
                new_state["last_bar_payload"] = self._bar_payload(obs)
                new_state["last_bar_ts"] = obs.ts.isoformat()
            # non-final observations keep the stored last_bar_ts untouched

            try:
                self.workflow_repo.assert_evaluation_owner(
                    sub.id,
                    sub.instrument_key,
                    self.owner_id,
                    owner_epoch,
                    now=ownership_now,
                    db=session,
                )
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
                self._log_suppression(sub, "lease_lost")
                return HandleResult(
                    fired=bool(pred.fired or engine.emit),
                    emitted=False,
                    suppression_reason="lease_lost",
                    rule_completed=False,
                )

            if engine.rule_completed and allow_emit:
                # Repository is frozen; flip the lifecycle via a scoped UPDATE
                # inside the same transaction. Warmup never completes a rule.
                session.execute(
                    update(AlertSubscription)
                    .where(AlertSubscription.id == sub.id)
                    .values(state="completed")
                )

            if owned:
                session.commit()

            if correction and not emitted:
                suppression_reason = "candle_correction"
            elif not allow_emit and engine.emit:
                suppression_reason: Optional[str] = "warmup"
            elif engine.emit:
                suppression_reason = None if emitted else "duplicate_occurrence"
            elif gap:
                suppression_reason = "feed_gap"
            else:
                suppression_reason = engine.suppression_reason

            result = HandleResult(
                fired=bool(pred.fired or engine.emit),
                emitted=emitted,
                suppression_reason=suppression_reason,
                rule_completed=engine.rule_completed if allow_emit else False,
            )
            if suppression_reason:
                self._log_suppression(sub, suppression_reason)
            return result
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

    @staticmethod
    def _reset_observation_state(state: dict) -> dict:
        """Reset continuity while retaining durable trigger bookkeeping."""
        reset = dict(state or {})
        for key in ("conds", "epoch_id", "last_bar_ts", "initialized"):
            reset.pop(key, None)
        reset.pop("last_bar_state", None)
        reset.pop("last_bar_payload", None)
        return reset

    @staticmethod
    def _bar_payload(obs: Observation) -> dict:
        return {
            "ts": obs.ts.isoformat(),
            "ltp": obs.ltp,
            "open": obs.open,
            "high": obs.high,
            "low": obs.low,
            "close": obs.close,
            "volume": obs.volume,
            "final": bool(obs.final),
        }

    def _record_correction_notice(
        self,
        sub: ActiveSubscription,
        obs: Observation,
        base_occurrence_key: str,
        evidence: dict,
        now: datetime,
        session: Session,
    ) -> bool:
        """Record one silent, idempotent audit event for a changed final bar."""
        correction_hash = hashlib.sha256(
            json.dumps(self._bar_payload(obs), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        correction_key = f"correction:{base_occurrence_key}:{correction_hash}"
        if self.workflow_repo.get_signal_by_occurrence(correction_key, db=session) is not None:
            return True
        correction_evidence = {
            **evidence,
            "correction_of": base_occurrence_key,
            "correction_hash": correction_hash,
        }
        try:
            with session.begin_nested():
                self.workflow_repo.record_signal(
                    sub.id,
                    correction_key,
                    fired_at=obs.ts,
                    evidence=correction_evidence,
                    channel_ids=(),
                    now=now,
                    db=session,
                )
        except IntegrityError:
            # Another correction writer won the unique occurrence key. The
            # checkpoint can still be committed by the current owner.
            return True
        return True

    @classmethod
    def _is_correction(cls, state: dict, obs: Observation) -> bool:
        last_raw = state.get("last_bar_ts")
        payload = state.get("last_bar_payload")
        if not last_raw or not isinstance(payload, dict) or not obs.final:
            return False
        try:
            last_ts = datetime.fromisoformat(str(last_raw))
        except (TypeError, ValueError):
            return False
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        return obs.ts == last_ts.astimezone(obs.ts.tzinfo or timezone.utc) and payload != cls._bar_payload(obs)

    @staticmethod
    def _is_stale_bar(state: dict, obs: Observation) -> bool:
        """True when the observation is at or before the stored last bar.

        Such a bar was already folded into the stored state; re-processing
        it against newer state could manufacture phantom crossings.
        """
        last_raw = state.get("last_bar_ts")
        if not last_raw:
            return False
        try:
            last_ts = datetime.fromisoformat(str(last_raw))
        except (TypeError, ValueError):
            return False
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=obs.ts.tzinfo)
        return obs.ts <= last_ts

    @staticmethod
    def _fallback_session_id(obs: Observation) -> str:
        """Observation date in IST — the default trading-session identity."""
        ts = obs.ts if obs.ts.tzinfo is not None else obs.ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(_IST).date().isoformat()

    @staticmethod
    def _supports_session_context(provider: Optional[SessionProvider]) -> bool:
        if provider is None:
            return False
        try:
            parameters = inspect.signature(provider).parameters.values()
        except (TypeError, ValueError):
            return True
        if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
            return True
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        return len(positional) >= 3

    def _resolve_session(
        self,
        sub: ActiveSubscription,
        document: WorkflowDocument,
        obs: Observation,
    ) -> tuple[bool, str]:
        """(session_active, session_id) from the provider, or the IST fallback."""
        if self.session_provider is not None:
            if self._session_provider_with_context:
                resolved = self.session_provider(document.session, sub.instrument_key, obs.ts)
            else:
                resolved = self.session_provider(obs.ts)
            if resolved is not None:
                return bool(resolved[0]), str(resolved[1])
        return True, self._fallback_session_id(obs)

    def _log_suppression(self, sub: ActiveSubscription, reason: str) -> None:
        """Every suppression is visible at INFO (audit requirement)."""
        logger.info(
            "alert %s (stage %s) suppressed for %s on %s: %s",
            sub.alert_id, sub.stage_id, sub.instrument_key, sub.id, reason,
        )

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
