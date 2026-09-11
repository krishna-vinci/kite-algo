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
from backend.alerts.predicates import Observation, PredicateResult, evaluate_stage
from backend.workflows import advanced_repository, breadth
from backend.workflows.models import (
    AlertSpec,
    Condition,
    ConditionGroup,
    Stage,
    WorkflowDocument,
)
from backend.workflows.parser import WorkflowParseError, parse_workflow_dict
from backend.workflows.registry import stage_uses_fundamentals
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


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Normalize an already-validated timestamp; never fabricate one.

    Mirrors the runtime's parser so the service can read timestamps the
    runtime put into the membership context (which arrives as ISO text).
    """
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# Breadth member conditions are evaluated through the ordinary predicate
# machinery, so they need a Stage-shaped view. The synthesized stage carries
# only the member-level groups: no clock, timeframe, input or alerts, because
# none of those participate in "does this member satisfy the condition now".
_BREADTH_MEMBER_STAGE_ID = "__breadth_member__"


def _breadth_member_stage(stage: Stage) -> Stage:
    """A Stage view over a breadth spec's member condition groups."""
    spec = stage.breadth
    groups = list(spec.condition or ())
    all_conditions = tuple(
        cond for group in groups if group.kind == "all" for cond in group.conditions
    )
    any_conditions = tuple(
        cond for group in groups if group.kind == "any" for cond in group.conditions
    )
    not_conditions = tuple(
        cond for group in groups if group.kind == "not" for cond in group.conditions
    )
    return Stage(
        id=f"{_BREADTH_MEMBER_STAGE_ID}:{stage.id}",
        type="signal",
        clock=stage.clock,
        timeframe=stage.timeframe,
        conditions=all_conditions,
        any_conditions=any_conditions,
        not_conditions=not_conditions,
    )


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
        breadth_max_instruments: int = 1000,
        breadth_membership_max_age_s: float = 900.0,
    ) -> None:
        self.workflow_repo = workflow_repo
        self.session_factory = session_factory
        self.clock = clock
        self.channel_resolver = channel_resolver
        self.session_provider = session_provider
        self._session_provider_with_context = self._supports_session_context(session_provider)
        self.owner_id = owner_id or f"evaluation-worker:{uuid.uuid4()}"
        self.ownership_lease_s = max(1.0, float(ownership_lease_s))
        # E-25 delivery-storm budget: per (workflow, alert) rolling emissions
        # window, worker-local (ownership fencing makes one live writer).
        import os as _os
        try:
            self.delivery_budget_per_window = max(
                1, int(_os.environ.get("ALERTS_DELIVERY_BUDGET_PER_WINDOW", "60"))
            )
        except ValueError:
            self.delivery_budget_per_window = 60
        self.delivery_budget_window_s = 60.0
        self._emission_windows: dict = {}
        # Phase 4 F10 breadth bounds. Capacity is reported as unknown rather
        # than resolved by pruning a still-valid contribution, and a stale
        # membership snapshot never supports a signal.
        self.breadth_max_instruments = max(1, int(breadth_max_instruments))
        self.breadth_membership_max_age_s = max(1.0, float(breadth_membership_max_age_s))

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

    def sync_universe_members(
        self,
        revision: Any,
        members: Sequence[str],
        *,
        universe_revision: Optional[int] = None,
        owner_id: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> dict:
        """Materialize one subscription per (alert, universe member) (F7).

        New members are admitted with a fresh checkpoint (the initialization
        guard keeps them silent until warmed); departed members are PAUSED
        with a visible reason — their event history is retained.

        A member that REJOINS is resumed, and its retained breadth
        contribution is cleared (Phase 4 F10): re-entry must not restore
        participation from before the departure, so the rule is honestly
        "an instrument contributes after admission". That eviction happens
        under the same advisory lock the aggregate takes, so it cannot race a
        concurrent count.

        Returns ``{"created": n, "departed": m, "readmitted": k}``.
        """
        try:
            document = parse_workflow_dict(revision.document)
        except (WorkflowParseError, ValueError, TypeError, AttributeError):
            return {"created": 0, "departed": 0}

        wanted: dict = {}
        for alert in document.alerts:
            for member in members:
                key = str(member).strip().upper()
                exchange, _, symbol = key.partition(":")
                wanted[(alert.id, key)] = (symbol, exchange)

        owned = db is None
        session = db if db is not None else self.session_factory()
        try:
            existing = session.execute(
                select(AlertSubscription).where(
                    AlertSubscription.revision_id == revision.id,
                    AlertSubscription.instrument_symbol == AlertSubscription.instrument_symbol,
                )
            ).scalars().all()
            member_rows = [
                row for row in existing
                if isinstance(row.config or {}, dict) and row.config.get("universe_member")
            ]
            known_pairs = {(r.alert_id, r.instrument_key) for r in member_rows}
            created = 0
            readmitted = 0
            # (stage_id, instrument_key) pairs whose breadth contribution must
            # be cleared because the member is only now (re)joining.
            breadth_resets: list = []
            breadth_stage_ids = {
                stage.id for stage in document.stages if stage.breadth is not None
            }
            for alert in document.alerts:
                for (_alert_id, member), (symbol, exchange) in wanted.items():
                    if (alert.id, member) in known_pairs:
                        # Already materialized. A row that had DEPARTED is
                        # rejoining: resume it and clear its retained
                        # participation, or the member would stay paused
                        # forever and a stale contribution could count.
                        row = next(
                            (
                                r for r in member_rows
                                if r.alert_id == alert.id and r.instrument_key == member
                            ),
                            None,
                        )
                        if row is not None and row.config.get("universe_departed"):
                            config = dict(row.config or {})
                            config.pop("universe_departed", None)
                            if universe_revision is not None:
                                config["universe_revision"] = universe_revision
                            row.config = config
                            row.state = "active"
                            readmitted += 1
                            if alert.source in breadth_stage_ids:
                                breadth_resets.append((alert.source, member))
                        continue
                    config = self._alert_config(alert)
                    binding = self._resolve_catalog_binding(member, session)
                    if binding is not None:
                        config["instrument_binding"] = binding
                    config["universe_member"] = True
                    if universe_revision is not None:
                        config["universe_revision"] = universe_revision
                    if alert.source in breadth_stage_ids:
                        breadth_resets.append((alert.source, member))
                    session.add(
                        AlertSubscription(
                            id=str(uuid.uuid4()),
                            revision_id=revision.id,
                            alert_id=alert.id,
                            stage_id=alert.source,
                            instrument_symbol=symbol,
                            instrument_exchange=exchange,
                            instrument_key=member,
                            trigger=alert.trigger,
                            config=config,
                            state="active",
                        )
                    )
                    created += 1

            departed = 0
            wanted_keys = set(wanted.keys())
            for row in member_rows:
                if (row.alert_id, row.instrument_key) in wanted_keys:
                    continue
                if row.state == "active":
                    config = dict(row.config or {})
                    config["universe_departed"] = True
                    row.config = config
                    row.state = "paused"
                    departed += 1
            if breadth_resets:
                self._clear_breadth_contributions(
                    session,
                    revision=revision,
                    owner_id=owner_id,
                    pairs=breadth_resets,
                )
            if owned:
                session.commit()
            if created or departed or readmitted:
                logger.info(
                    "universe membership sync for revision %s: %d admitted, "
                    "%d departed, %d re-admitted",
                    revision.id, created, departed, readmitted,
                )
            return {"created": created, "departed": departed, "readmitted": readmitted}
        except IntegrityError:
            if owned:
                session.rollback()
            return {"created": 0, "departed": 0, "readmitted": 0}
        except Exception:
            if owned:
                session.rollback()
            raise
        finally:
            if owned:
                session.close()

    def _clear_breadth_contributions(
        self,
        session: Session,
        *,
        revision: Any,
        owner_id: Optional[str],
        pairs: Sequence[tuple],
    ) -> None:
        """Drop retained breadth contributions for (re)admitted members.

        Serialized against concurrent aggregation by taking the SAME advisory
        transaction lock the aggregate takes, for every affected stage, before
        touching a row. Without that, a count running on another worker could
        observe a member's contribution being inserted or removed mid-window
        and produce a count that never existed at any instant.
        """
        stage_ids = list(dict.fromkeys(stage_id for stage_id, _member in pairs))
        for stage_id in stage_ids:
            advanced_repository.lock_breadth_stage(
                session,
                owner_id=owner_id or "",
                workflow_id=revision.workflow_id,
                revision_id=revision.id,
                stage_id=stage_id,
            )
        for stage_id, member in pairs:
            advanced_repository.evict_breadth_contribution(
                session,
                owner_id=owner_id or "",
                workflow_id=revision.workflow_id,
                revision_id=revision.id,
                stage_id=stage_id,
                instrument_key=member,
            )

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
        features: Optional[dict] = None,
        layers: Optional[list] = None,
        breadth_membership: Optional[dict] = None,
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

        ``features``: shared feature values (feature-id -> value) computed
        once per market event by the FeatureEngine; indicator operands
        resolve from here (Phase 2 F8).

        ``layers``: ancestor filter stages of a layered chain, each as
        ``(stage, features_dict)``. Their conditions are ANDed with the
        stage's own conditions under three-valued logic; unknown upstream
        never manufactures a firing. Evidence is recorded per layer.

        ``breadth_membership``: for a breadth stage, the member context this
        observation contributes to — ``{"members": [...],
        "universe_revision": int|None, "resolved_at": iso8601|None}``. A
        breadth observation is dispatch from ONE member instrument but the
        crossing it may mint is a WORKFLOW-level fact, so the event carries no
        instrument and exactly one is published per crossing.
        """
        document = self._parse_document(sub)
        if document is None:
            return _NOOP
        stage = next((s for s in document.stages if s.id == sub.stage_id), None)
        alert = next((a for a in document.alerts if a.id == sub.alert_id), None)
        if stage is None:
            logger.warning(
                "subscription %s references missing stage %s in revision document",
                sub.id, sub.stage_id,
            )
            return _NOOP
        # Phase 4 F10: a breadth stage is a different kind of rule — it
        # aggregates its member subscriptions into one workflow-level event —
        # so it takes its own path through the same fence and transaction.
        if stage.breadth is not None:
            if alert is None:
                logger.warning(
                    "breadth subscription %s references missing alert %s",
                    sub.id, sub.alert_id,
                )
                return _NOOP
            return self._handle_breadth_observation(
                sub, obs, stage, alert, document,
                allow_emit=allow_emit,
                context=context,
                features=features,
                membership=breadth_membership,
                db=db,
            )
        if alert is None:
            logger.warning(
                "subscription %s references missing alert %s in revision document",
                sub.id, sub.alert_id,
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

            pred = evaluate_stage(stage, obs, state, context, features)
            for layer_stage, layer_features in layers or ():
                layer_result = evaluate_stage(
                    layer_stage, obs, pred.state, context, layer_features
                )
                layer_evidence = {
                    f"layer:{layer_stage.id}:{key}": value
                    for key, value in layer_result.evidence.items()
                }
                pred = PredicateResult(
                    matched=(
                        None
                        if pred.matched is None or layer_result.matched is None
                        else (pred.matched and layer_result.matched)
                    ),
                    fired=bool(pred.fired and layer_result.matched is True),
                    evidence={**pred.evidence, **layer_evidence},
                    state=layer_result.state,
                )
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
            universe_revision = (sub.config or {}).get("universe_revision")
            if universe_revision is not None:
                # F7: events retain the membership revision in force at
                # evaluation time.
                evidence["universe_revision"] = universe_revision
            # Spec §5.8: fundamental observations carry acquisition metadata.
            # When fundamentals participate in this stage or any ancestor
            # layer being evaluated, copy the freshness the evaluation
            # actually saw into immutable event evidence.
            if context and (
                stage_uses_fundamentals(stage)
                or any(stage_uses_fundamentals(layer) for layer, _ in (layers or ()))
            ):
                acquired_at = context.get("fundamentals.acquired_at")
                if acquired_at:
                    evidence["fundamentals_acquired_at"] = str(acquired_at)
                as_of = context.get("fundamentals.as_of_date")
                if as_of:
                    evidence["fundamentals_as_of_date"] = str(as_of)
            if engine.emit and allow_emit and self._storm_budget_exceeded(sub, now):
                suppression = "storm_budget"
                engine_result = HandleResult(
                    fired=bool(pred.fired or engine.emit),
                    emitted=False,
                    suppression_reason=suppression,
                    rule_completed=False,
                )
                if owned:
                    session.rollback()
                self._log_suppression(sub, suppression)
                return engine_result
            # Per-session notification cap (Phase 4 F10). Unlike the storm
            # budget above, this SKIPS THE NOTIFICATION BUT ADVANCES STATE:
            # the checkpoint, streak/sequence progress and the counter all
            # commit, and only the signal event + outbox rows are skipped — a
            # capped bar must not silently drop out of a consecutive-bar
            # streak or an armed sequence. The slot is claimed with one guarded
            # upsert shared across every instrument of the alert, so concurrent
            # workers cannot both take the last slot.
            session_capped = False
            if engine.emit and allow_emit and alert.max_per_session:
                session_capped = not advanced_repo.reserve_session_slot(
                    session,
                    owner_id=sub.owner_id,
                    workflow_id=sub.workflow_id,
                    revision_id=sub.revision_id,
                    alert_id=sub.alert_id,
                    session_id=session_id or "default",
                    maximum=int(alert.max_per_session),
                    now=now,
                )
                if session_capped:
                    advanced_repo.record_suppression(
                        session,
                        owner_id=sub.owner_id,
                        workflow_id=sub.workflow_id,
                        revision_id=sub.revision_id,
                        alert_id=sub.alert_id,
                        session_id=session_id,
                        reason="session_cap",
                        instrument_key=sub.instrument_key,
                        stage_id=sub.stage_id,
                        now=now,
                    )
                    self._log_suppression(sub, "session_cap")
            if engine.emit and allow_emit and not session_capped:
                self._record_emission(sub, now)
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
            elif session_capped:
                # Notification suppressed, state advanced (see the cap above).
                suppression_reason = "session_cap"
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

    def _storm_budget_exceeded(self, sub: ActiveSubscription, now: datetime) -> bool:
        window = self._emission_windows.get((sub.workflow_id, sub.alert_id))
        if not window:
            return False
        cutoff = now.timestamp() - self.delivery_budget_window_s
        return sum(1 for ts in window if ts >= cutoff) >= self.delivery_budget_per_window

    def _record_emission(self, sub: ActiveSubscription, now: datetime) -> None:
        window = self._emission_windows.setdefault((sub.workflow_id, sub.alert_id), [])
        window.append(now.timestamp())
        cutoff = now.timestamp() - self.delivery_budget_window_s
        del window[: max(0, len(window) - self.delivery_budget_per_window - 10)]
        while window and window[0] < cutoff:
            window.pop(0)

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

    def _handle_breadth_observation(
        self,
        sub: ActiveSubscription,
        obs: Observation,
        stage: Stage,
        alert: AlertSpec,
        document: WorkflowDocument,
        *,
        allow_emit: bool,
        context: Optional[dict],
        features: Optional[dict],
        membership: Optional[dict],
        db: Optional[Session],
    ) -> HandleResult:
        """Evaluate one member's bar against a breadth stage (Phase 4 F10).

        A different kind of rule from a signal alert: the observation comes
        from ONE member instrument, but what the rule reports is an AGGREGATE
        fact about the workflow revision, so a crossing publishes exactly ONE
        workflow-level event (``subscription_id`` NULL,
        ``evidence.message_kind == "breadth"``) no matter how many members
        crossed. Per-instrument notifications are structurally impossible
        because the occurrence key carries the crossing number, not an
        instrument.

        Everything runs inside the ordinary publication boundary — the same
        ownership claim/assert fence and the same single commit as a signal
        alert — so a stale owner or an injected failure rolls the checkpoint,
        the contribution, the aggregate state, the event and the outbox rows
        back together.
        """
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
            if checkpoint is None:
                state, stored_epoch = {}, 0
            else:
                state, stored_epoch = checkpoint

            # The member's own condition, evaluated through the shared
            # predicate layer. Its state is per (subscription, instrument) and
            # epoch-scoped, exactly like any other condition state.
            member_matched = evaluate_stage(
                _breadth_member_stage(stage), obs, state, context, features
            )

            member_context = membership or {}
            outcome = breadth.evaluate_breadth(
                session,
                owner_id=sub.owner_id,
                workflow_id=sub.workflow_id,
                revision_id=sub.revision_id,
                stage_id=sub.stage_id,
                spec=stage.breadth,
                member_keys=list(member_context.get("members") or ()),
                member_universe_revision=member_context.get("universe_revision"),
                membership_resolved_at=_parse_ts(member_context.get("resolved_at")),
                # A contribution is recorded only when the member's condition
                # ACTUALLY held; unknown is not true (spec 5.3).
                triggering=(
                    sub.instrument_key if member_matched.matched is True else None
                ),
                observed_at=obs.ts,
                max_instruments=self.breadth_max_instruments,
                membership_max_age_s=self.breadth_membership_max_age_s,
                # Freshness is a WALL-CLOCK property of the resolution (how
                # long ago membership was materialized), so it is compared
                # against the service clock / real time — never against a
                # bar's event time, which says nothing about membership age.
                membership_now=(
                    self.clock() if self.clock is not None
                    else datetime.now(timezone.utc)
                ),
            )

            emitted = False
            suppression: Optional[str] = None
            if outcome.reason is not None:
                suppression = outcome.reason
                self._log_suppression(sub, outcome.reason)
            elif outcome.fired:
                if not allow_emit:
                    # Warmup replays history to establish window continuity. A
                    # crossing found there updates the aggregate state (so a
                    # live crossing is never double-reported) but does not
                    # notify — the activation baseline, by design.
                    suppression = "warmup"
                else:
                    emitted = self._publish_breadth_crossing(
                        sub, obs, stage, alert, outcome, channel_ids,
                        owner_epoch, ownership_now, session,
                        workflow_name=document.name,
                        universe_revision=member_context.get("universe_revision"),
                    )
                    if not emitted:
                        suppression = "duplicate_occurrence"

            # The member's checkpoint rides the same commit as the aggregate,
            # so a failure rolls both back together.
            new_state = dict(member_matched.state)
            new_state["epoch_id"] = obs.epoch_id
            if obs.final:
                new_state["last_bar_ts"] = obs.ts.isoformat()
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
                session.rollback()
                self._log_suppression(sub, "lease_lost")
                return HandleResult(False, False, "lease_lost", False)

            if owned:
                session.commit()
            return HandleResult(
                fired=bool(outcome.fired or emitted),
                emitted=emitted,
                suppression_reason=suppression,
                rule_completed=False,
            )
        except Exception:
            if owned:
                session.rollback()
            raise
        finally:
            if owned:
                session.close()

    def _publish_breadth_crossing(
        self,
        sub: ActiveSubscription,
        obs: Observation,
        stage: Stage,
        alert: AlertSpec,
        outcome,
        channel_ids: Sequence[str],
        owner_epoch: int,
        ownership_now,
        session: Session,
        *,
        workflow_name: str,
        universe_revision: Optional[int],
    ) -> bool:
        """Insert the ONE workflow-level breadth event plus its outbox rows.

        The occurrence key is ``...:<stage>:breadth:<crossing_seq>``: it carries
        the durable crossing identity and NO instrument, so a second member
        reaching the same crossing cannot create a second event even if it
        publishes concurrently (the unique key rejects it and this returns
        False).
        """
        evidence = {
            "message_kind": "breadth",
            "workflow_id": str(sub.workflow_id),
            # The workflow NAME is what an operator recognises in a message;
            # resolved from the parsed document the caller already holds.
            "workflow_name": workflow_name or "workflow",
            "stage_id": stage.id,
            "mode": stage.breadth.mode,
            "action": "breadth_crossing",
            "trigger": "on_transition",
            "count": outcome.count,
            "threshold": int(stage.breadth.distinct_instruments),
            "members": outcome.members,
            "instruments": list(outcome.contributors),
            "window_start": (
                outcome.window_start.isoformat() if outcome.window_start else None
            ),
            "event_time": obs.ts.isoformat(),
            "crossing_seq": outcome.crossing_seq,
            "universe_revision": universe_revision,
            "message": alert.message,
        }
        self.workflow_repo.assert_evaluation_owner(
            sub.id,
            sub.instrument_key,
            self.owner_id,
            owner_epoch,
            now=ownership_now,
            db=session,
        )
        occurrence_key = (
            f"{sub.workflow_id}:{sub.revision_id}:{stage.id}:"
            f"breadth:{outcome.crossing_seq}"
        )
        try:
            with session.begin_nested():
                event = self._record_workflow_event(
                    sub, occurrence_key, fired_at=obs.ts, evidence=evidence,
                    channel_ids=channel_ids, now=obs.ts, db=session,
                )
        except IntegrityError:
            # Another member published this same crossing first: exactly one
            # logical event exists, which is the required outcome.
            return False
        return event is not None

    def _record_workflow_event(
        self,
        sub: ActiveSubscription,
        occurrence_key: str,
        *,
        fired_at: datetime,
        evidence: dict,
        channel_ids: Sequence[str],
        now: datetime,
        db: Session,
    ):
        """Insert a workflow-level signal event (no subscription) + deliveries."""
        from backend.notifications.repository import Delivery
        from backend.workflows.repository import SignalEvent

        event = SignalEvent(
            id=str(uuid.uuid4()),
            subscription_id=None,
            workflow_id=str(sub.workflow_id),
            occurrence_key=occurrence_key,
            fired_at=fired_at,
            evidence=dict(evidence or {}),
            created_at=now,
        )
        db.add(event)
        db.flush()
        for channel_id in dict.fromkeys(channel_ids or ()):
            db.add(
                Delivery(
                    id=str(uuid.uuid4()),
                    event_id=event.id,
                    channel_id=channel_id,
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        return event

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
