"""Worker API for alert workflows (Alerts Platform Phase 1, Task 8).

Endpoints under ``/worker/workflows`` (mounted at ``/api`` like every other
worker router). Auth uses the shared ``require_worker_token`` dependency plus
per-action checks (``workflows:read`` / ``workflows:write`` /
``workflows:activate``); every row is scoped to an owner derived from the
token's account scope so one worker token can never touch another's alerts.

The workflows/notifications tables come from the alerts-platform repositories
(``backend.workflows.repository`` / ``backend.notifications.repository``).
The sessionmaker is injected: ``app.state.alerts_session_factory`` wins, else
the global ``SessionLocal``. Tests swap it via ``app.dependency_overrides``.

Importing this module must not require redis; the workflows layer is
stdlib + SQLAlchemy + PyYAML only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.api.routers.worker_shared import _require_action, require_worker_token
from backend.api.schemas.workflows import (
    ChannelResponse,
    EventPage,
    HealthResponse,
    IssueEnvelope,
    PreviewResponse,
    RevisionSummary,
    SignalEventItem,
    SubscriptionHealth,
    ValidationIssueModel,
    WorkflowActivateRequest,
    WorkflowCreateRequest,
    WorkflowExportResponse,
    WorkflowListResponse,
    WorkflowMutationResponse,
    WorkflowPatchRequest,
    WorkflowSummary,
    WorkflowValidateRequest,
    issue,
)
from backend.notifications.repository import Delivery, SqlAlchemyNotificationRepository
from backend.alerts.engine import decide
from backend.alerts.predicates import Observation, evaluate_stage
from backend.workflows.compiler import CompiledWorkflow, WorkflowValidationError, compile_document
from backend.workflows.models import AlertSpec, WorkflowDocument
from backend.workflows.parser import WorkflowParseError, parse_workflow_dict, parse_workflow_yaml
from backend.workflows.repository import (
    AlertSubscription,
    DomainConflict,
    EvaluationCheckpoint,
    IdempotencyConflict,
    RevisionConflict,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
    Workflow as WorkflowModel,
    WorkflowRevision,
)

router = APIRouter(prefix="/worker/workflows", tags=["Worker Workflows"])

# Client-facing staleness contract: a subscription whose last_evaluated_at is
# older than STALE_AFTER_SECONDS should be treated as stale (F11).
STALE_AFTER_SECONDS = 300

__all__ = [
    "router",
    "_alerts_db",
    "_notification_repository",
    "_workflow_repository",
]


# ---------------------------------------------------------------------------
# injectable dependencies
# ---------------------------------------------------------------------------


def _alerts_db(request: Request):
    """Sessionmaker for the alerts-platform tables (injectable for tests)."""
    factory = getattr(request.app.state, "alerts_session_factory", None)
    if factory is not None:
        return factory
    from backend.app.database import SessionLocal

    return SessionLocal


def _workflow_repository(
    request: Request,
    session_factory: Any = Depends(_alerts_db),
) -> SqlAlchemyWorkflowRepository:
    repository = getattr(request.app.state, "workflow_repository", None)
    if repository is not None:
        return repository
    return SqlAlchemyWorkflowRepository(session_factory)


def _notification_repository(
    request: Request,
    session_factory: Any = Depends(_alerts_db),
) -> SqlAlchemyNotificationRepository:
    repository = getattr(request.app.state, "notification_repository", None)
    if repository is not None:
        return repository
    return SqlAlchemyNotificationRepository(session_factory)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _owner_id_for_token(token: Any) -> str:
    scope = str(getattr(token, "account_scope", "") or "").strip()
    return scope or f"worker:{getattr(token, 'token_id', '')}"


async def _authorize(request: Request, action: str) -> Tuple[Any, str]:
    token = await require_worker_token(request)
    _require_action(token, action)
    return token, _owner_id_for_token(token)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _load_document(yaml_text: Optional[str], document: Optional[Dict[str, Any]]) -> WorkflowDocument:
    """Parse the request transport; 422 when the transport itself is bad."""
    if (yaml_text is None) == (document is None):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "issues": [issue("request", "bad_request", "provide exactly one of yaml_text or document").model_dump()]},
        )
    try:
        if yaml_text is not None:
            return parse_workflow_yaml(yaml_text)
        return parse_workflow_dict(document)
    except WorkflowParseError as exc:
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "issues": [_parse_issues(exc)[0].model_dump()]},
        ) from exc


def _parse_issues(exc: WorkflowParseError) -> List[ValidationIssueModel]:
    return [issue("document", "parse_error", str(exc))]


def _validation_issues(exc: WorkflowValidationError) -> List[ValidationIssueModel]:
    return [issue(item.where, item.code, item.message) for item in exc.issues]


def _compile_or_422(doc: WorkflowDocument) -> CompiledWorkflow:
    try:
        return compile_document(doc)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "issues": [item.model_dump() for item in _validation_issues(exc)]},
        ) from exc


def _owned_workflow(session: Session, workflow_id: str, owner_id: str) -> WorkflowModel:
    workflow = session.get(WorkflowModel, workflow_id)
    if workflow is None or workflow.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


def _latest_revision(session: Session, workflow_id: str) -> Optional[WorkflowRevision]:
    return session.execute(
        select(WorkflowRevision)
        .where(WorkflowRevision.workflow_id == workflow_id)
        .order_by(WorkflowRevision.revision.desc())
        .limit(1)
    ).scalar_one_or_none()


def _revision_by_number(session: Session, workflow_id: str, revision: int) -> Optional[WorkflowRevision]:
    return session.execute(
        select(WorkflowRevision)
        .where(WorkflowRevision.workflow_id == workflow_id, WorkflowRevision.revision == revision)
        .limit(1)
    ).scalar_one_or_none()


def _revision_summary(revision: Optional[WorkflowRevision]) -> Optional[RevisionSummary]:
    if revision is None:
        return None
    return RevisionSummary(
        revision_id=revision.id,
        revision=int(revision.revision),
        status=str(revision.status),
        canonical_hash=str(revision.canonical_hash),
        created_at=_iso(revision.created_at),
        activated_at=_iso(revision.activated_at),
    )


def _workflow_summary(session: Session, workflow: WorkflowModel) -> WorkflowSummary:
    latest = _latest_revision(session, workflow.id)
    active = session.execute(
        select(WorkflowRevision)
        .where(WorkflowRevision.workflow_id == workflow.id, WorkflowRevision.status == "active")
        .order_by(WorkflowRevision.activated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return WorkflowSummary(
        workflow_id=workflow.id,
        name=str(workflow.name),
        idempotency_key=workflow.idempotency_key,
        archived=workflow.archived_at is not None,
        archived_at=_iso(workflow.archived_at),
        created_at=_iso(workflow.created_at),
        updated_at=_iso(workflow.updated_at),
        latest_revision=_revision_summary(latest),
        active_revision=_revision_summary(active),
    )


def _subscription_config(alert: AlertSpec) -> Dict[str, Any]:
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


def _workflow_subscription_ids(session: Session, workflow_id: str) -> List[str]:
    return list(
        session.execute(
            select(AlertSubscription.id)
            .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
            .where(WorkflowRevision.workflow_id == workflow_id)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# validate / preview (read-only, never touch the database)
# ---------------------------------------------------------------------------


def _preview_observation(raw: Any, index: int) -> tuple[Optional[str], Optional[Observation], Optional[str]]:
    """Parse one client-supplied preview sample without inventing event time."""
    if not isinstance(raw, dict):
        return None, None, f"observations[{index}]: not a mapping"
    instrument_key = raw.get("instrument_key") or raw.get("instrument")
    if not isinstance(instrument_key, str) or not instrument_key.strip():
        return None, None, f"observations[{index}]: missing instrument_key"
    raw_ts = raw.get("ts")
    if not isinstance(raw_ts, (str, datetime)):
        return instrument_key, None, f"observations[{index}]: missing or invalid ts"
    try:
        ts = raw_ts if isinstance(raw_ts, datetime) else datetime.fromisoformat(
            raw_ts.replace("Z", "+00:00").replace("z", "+00:00")
        )
    except (TypeError, ValueError):
        return instrument_key, None, f"observations[{index}]: malformed ts"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    values: dict[str, Optional[float]] = {}
    for field in ("ltp", "open", "high", "low", "close", "volume"):
        value = raw.get(field)
        if value is None:
            values[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return instrument_key, None, f"observations[{index}]: invalid {field}"
        values[field] = float(value)
    epoch_id = raw.get("epoch_id", "preview")
    if not isinstance(epoch_id, str) or not epoch_id:
        return instrument_key, None, f"observations[{index}]: invalid epoch_id"
    return instrument_key, Observation(
        ts=ts.astimezone(timezone.utc),
        epoch_id=epoch_id,
        final=bool(raw.get("final", False)),
        **values,
    ), None


def _preview_evaluate(doc: WorkflowDocument, rows: List[Dict[str, Any]]) -> dict:
    """Run pure predicate/trigger evaluation over supplied recent samples."""
    grouped: dict[str, list[Observation]] = {}
    instrument_keys = {instrument.key() for instrument in doc.instruments}
    unknown: list[str] = []
    for index, raw in enumerate(rows):
        instrument_key, observation, reason = _preview_observation(raw, index)
        if reason:
            unknown.append(reason)
        if instrument_key and observation is not None:
            if instrument_key not in instrument_keys:
                unknown.append(f"observations[{index}]: instrument_not_in_workflow")
            grouped.setdefault(instrument_key, []).append(observation)
    for observations in grouped.values():
        observations.sort(key=lambda item: item.ts)

    would_fire: list[dict] = []
    stages = {stage.id: stage for stage in doc.stages}
    evaluated = sum(
        len(observations)
        for instrument_key, observations in grouped.items()
        if instrument_key in instrument_keys
    )
    warmup_bars = sum(
        1
        for instrument_key, observations in grouped.items()
        if instrument_key in instrument_keys
        for observation in observations
        if observation.final
    )
    for alert in doc.alerts:
        stage = stages.get(alert.source)
        if stage is None:
            continue
        for instrument in doc.instruments:
            instrument_key = instrument.key()
            state: dict = {}
            for observation in grouped.get(instrument_key, []):
                if stage.clock == "candle_close" and not observation.final:
                    unknown.append(f"{alert.id}:{instrument_key}:non_final_candle")
                    continue
                predicate = evaluate_stage(stage, observation, state)
                first_observation = not state.get("initialized")
                decision = decide(
                    alert,
                    fired=predicate.fired,
                    state=predicate.state,
                    now=observation.ts,
                    session_id=observation.ts.date().isoformat(),
                    already_true=predicate.matched if first_observation else None,
                    matched=predicate.matched,
                )
                state = decision.new_state
                if predicate.matched is None:
                    unknown.append(f"{alert.id}:{instrument_key}:missing_or_unsupported_data")
                if decision.emit:
                    would_fire.append({
                        "alert_id": alert.id,
                        "instrument_key": instrument_key,
                        "fired_at": observation.ts.isoformat(),
                        "evidence": predicate.evidence,
                    })
    return {
        "evaluation": "dry_run" if grouped else "dry_run_no_data",
        "warmup_bars": warmup_bars,
        "evaluated_observations": evaluated,
        "would_fire": would_fire,
        "unknown_reasons": sorted(set(unknown)),
    }


def capabilities_payload() -> dict:
    """Build the discovery payload from the SAME registry the compiler uses.

    Kept as a pure function (no request, no auth) so a test can prove that
    discovery and validation cannot drift: every advertised capability comes
    from ``backend.workflows.registry``, which is also what
    ``compile_document`` validates against.
    """
    from backend.workflows import registry as wf_registry

    features = {
        name: {
            "params": {
                param: {"min": bounds[0], "max": bounds[1]}
                for param, bounds in spec["params"].items()
            },
            "defaults": dict(spec.get("defaults", {})),
            "inputs": list(spec["inputs"]),
            "outputs": list(spec.get("outputs", ["value"])),
        }
        for name, spec in sorted(wf_registry.FEATURE_FUNCTIONS.items())
    }
    pairs = {
        name: {
            "formula": spec["formula"],
            "fields": list(spec["fields"]),
            "requires": list(spec["requires"]),
            "optional": list(spec.get("optional", ())),
            "unknown_reasons": list(spec["unknown_reasons"]),
        }
        for name, spec in sorted(wf_registry.PAIR_COMPUTATIONS.items())
    }
    return {
        "operators": {name: spec.get("kind") for name, spec in sorted(wf_registry.OPERATORS.items())},
        "fields": sorted(wf_registry.FIELDS),
        "fundamentals_fields": sorted(wf_registry.FUNDAMENTALS_FIELDS),
        "fundamentals_source": {
            "table": "public.fundamentals_features",
            "description": (
                "Latest stored snapshot per bare symbol (Screener.in "
                "nightly sync, NSE-listing coverage); NSE:SYMBOL keys "
                "resolve, other exchanges are unavailable. Missing rows "
                "leave fields unknown, never false. Refreshed by the "
                "nightly fundamentals scheduler; on-demand via "
                "POST /algo-workers/worker/fundamentals/sync."
            ),
            "freshness_keys": [
                "fundamentals.acquired_at",
                "fundamentals.as_of_date",
                "fundamentals.statement_scope",
            ],
            "columns": dict(sorted(wf_registry.FUNDAMENTALS_COLUMN_MAP.items())),
        },
        "clocks": {
            name: spec
            for name, spec in sorted(wf_registry.CLOCK_METADATA.items())
        },
        "clock_aliases": dict(sorted(wf_registry.CLOCK_ALIASES.items())),
        "triggers": sorted(wf_registry.TRIGGERS),
        "timeframes": sorted(wf_registry.TIMEFRAMES),
        "sessions": sorted(wf_registry.SESSIONS),
        "features": features,
        "arithmetic": sorted(wf_registry.ARITHMETIC_OPS),
        "limits": {
            "max_stages": 64,
            "max_alerts": 256,
            "max_instruments": 1000,
            "max_feature_stages": wf_registry.MAX_FEATURE_STAGES,
            "max_conditions_per_group": wf_registry.MAX_CONDITIONS_PER_GROUP,
            "max_arithmetic_depth": wf_registry.MAX_ARITHMETIC_DEPTH,
            "max_input_chain_depth": wf_registry.MAX_INPUT_CHAIN_DEPTH,
            # Phase 4 F10 — derived from the registry, never hard-coded here.
            "max_consecutive_bars": wf_registry.MAX_CONSECUTIVE_BARS,
            "max_sequence_within_bars": wf_registry.MAX_SEQUENCE_WITHIN_BARS,
            "max_breadth_instruments": wf_registry.MAX_BREADTH_INSTRUMENTS,
            "max_breadth_window_s": wf_registry.MAX_BREADTH_WINDOW_S,
            "max_per_session": wf_registry.MAX_PER_SESSION,
        },
        # Phase 4 F10 surface (registry-derived, so it cannot drift).
        "stage_types": sorted(wf_registry.STAGE_TYPES),
        "pairs": pairs,
        "pair_lookback_bounds": list(wf_registry.PAIR_LOOKBACK_BOUNDS),
        "pair_max_skew_bars": wf_registry.PAIR_MAX_SKEW_BARS,
        "breadth_modes": {
            name: {"implemented": bool(spec.get("implemented"))}
            for name, spec in sorted(wf_registry.BREADTH_MODES.items())
        },
        "breadth_semantics": (
            "windowed distinct-symbol participation: at least K different "
            "instruments triggered during the last W. This is NOT simultaneous "
            "breadth — that mode is reserved and not implemented."
        ),
        "hysteresis": {
            "operators": sorted(wf_registry.HYSTERESIS_OPS),
            "threshold": "constant only (right: {value: X}); dynamic release operands are not implemented",
        },
        "session_cap_resets": sorted(wf_registry.SESSION_CAP_RESETS),
        "session_cap_note": (
            "the per-session cap is scoped to (workflow, alert), counts LOGICAL "
            "notifications (one per signal event, never per channel delivery), "
            "and resets when the resolved session id changes; MCX/currency "
            "sessions are feed-driven, so their boundary is the IST date"
        ),
        "universe_ref_kinds": ["universe", "index", "watchlist"],
        "screener": {
            "attachment_triggers": ["entry", "exit", "top_n", "rank_delta"],
            "attachment_hysteresis": {
                "top_n": "enter at rank <= entry_rank, exit only when rank > exit_rank",
                "entry_exit": "exit_after consecutive absent complete runs (default 1)",
            },
            "schedule_calendars": ["nse_equity"],
            "schedule_note": (
                "only nse_equity is calendar-backed; MCX/currency "
                "eligibility is feed-driven and provides no session "
                "calendar for scheduled scans"
            ),
            "stored_data_fields": ["change_pct", "turnover"],
            "run_statuses": ["running", "complete", "partial", "failed"],
            "tie_break": "instrument identity (EXCHANGE:SYMBOL) ascending",
        },
        "universe_source_kinds": ["explicit", "index", "portfolio", "screener"],
    }


async def workflow_capabilities(request: Request):
    """Phase 2 discovery (+ Phase 4 F10): only EXECUTABLE capabilities.

    Compiler validation and this discovery endpoint read the same registry,
    so a capability listed here is exactly one the worker can evaluate.
    """
    token, _ = await _authorize(request, "workflows:read")
    _ = token
    return {"ok": True, "capabilities": capabilities_payload()}


async def validate_workflow(request: Request, payload: WorkflowValidateRequest):
    """Parse + compile a document and report issues. 200 even when invalid."""
    token, _ = await _authorize(request, "workflows:read")
    _ = token
    if (payload.yaml_text is None) == (payload.document is None):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "issues": [issue("request", "bad_request", "provide exactly one of yaml_text or document").model_dump()]},
        )
    try:
        if payload.yaml_text is not None:
            doc = parse_workflow_yaml(payload.yaml_text)
        else:
            doc = parse_workflow_dict(payload.document)
    except WorkflowParseError as exc:
        return IssueEnvelope(ok=False, issues=_parse_issues(exc))
    try:
        compile_document(doc)
    except WorkflowValidationError as exc:
        return IssueEnvelope(ok=False, issues=_validation_issues(exc))
    return IssueEnvelope(ok=True, issues=[])


async def preview_workflow(request: Request, payload: WorkflowValidateRequest):
    """Compile + optional pure dry-run over supplied recent samples.

    Preview never reads or writes alert state, sends notifications, or marks a
    workflow active. The caller supplies timestamped samples so preview stays
    deterministic and does not silently use a different market-data source.
    """
    token, _ = await _authorize(request, "workflows:read")
    _ = token
    if (payload.yaml_text is None) == (payload.document is None):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "issues": [issue("request", "bad_request", "provide exactly one of yaml_text or document").model_dump()]},
        )
    try:
        if payload.yaml_text is not None:
            doc = parse_workflow_yaml(payload.yaml_text)
        else:
            doc = parse_workflow_dict(payload.document)
    except WorkflowParseError as exc:
        return PreviewResponse(ok=False, issues=_parse_issues(exc))
    try:
        compile_document(doc)
    except WorkflowValidationError as exc:
        return PreviewResponse(
            ok=False,
            issues=_validation_issues(exc),
            instruments=[instrument.key() for instrument in doc.instruments],
            stages=[stage.id for stage in doc.stages],
            alerts=[alert.id for alert in doc.alerts],
        )
    report = _preview_evaluate(doc, payload.observations)
    return PreviewResponse(
        ok=True,
        issues=[],
        instruments=[instrument.key() for instrument in doc.instruments],
        stages=[stage.id for stage in doc.stages],
        alerts=[alert.id for alert in doc.alerts],
        **report,
        note=(
            "preview only: compiled and evaluated in memory; nothing is persisted "
            "(no workflows, signal events or deliveries)"
        ),
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_workflow(
    request: Request,
    payload: WorkflowCreateRequest,
    workflow_repo: SqlAlchemyWorkflowRepository = Depends(_workflow_repository),
    session_factory: Any = Depends(_alerts_db),
):
    token, owner_id = await _authorize(request, "workflows:write")
    _ = token
    doc = _load_document(payload.yaml_text, payload.document)
    compiled = _compile_or_422(doc)
    name = (payload.name or "").strip() or doc.name

    existing_id: Optional[str] = None
    if payload.idempotency_key:
        session = session_factory()
        try:
            row = session.execute(
                select(WorkflowModel).where(WorkflowModel.idempotency_key == payload.idempotency_key)
            ).scalar_one_or_none()
            existing_id = row.id if row is not None else None
        finally:
            session.close()

    try:
        workflow, revision = workflow_repo.create_workflow(
            owner_id,
            name,
            compiled.document.to_document_dict(),
            compiled.canonical_hash,
            payload.idempotency_key,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DomainConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return WorkflowMutationResponse(
        workflow_id=workflow.id,
        name=str(workflow.name),
        idempotency_key=workflow.idempotency_key,
        created=existing_id is None,
        revision=int(revision.revision),
        revision_id=revision.id,
        revision_status=str(revision.status),
        canonical_hash=str(revision.canonical_hash),
    )


async def import_workflow(
    request: Request,
    payload: WorkflowCreateRequest,
    workflow_repo: SqlAlchemyWorkflowRepository = Depends(_workflow_repository),
    session_factory: Any = Depends(_alerts_db),
):
    """POST /import: same semantics as create, YAML text only."""
    if not payload.yaml_text:
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "issues": [issue("request", "bad_request", "import requires yaml_text").model_dump()]},
        )
    return await create_workflow(request, payload, workflow_repo, session_factory)


async def list_workflows(
    request: Request,
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "workflows:read")
    session = session_factory()
    try:
        workflows = list(
            session.execute(
                select(WorkflowModel)
                .where(WorkflowModel.owner_id == owner_id)
                .order_by(WorkflowModel.created_at.desc(), WorkflowModel.id.desc())
            ).scalars().all()
        )
        return WorkflowListResponse(
            workflows=[_workflow_summary(session, workflow) for workflow in workflows]
        )
    finally:
        session.close()


async def get_workflow(
    request: Request,
    workflow_id: str,
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "workflows:read")
    session = session_factory()
    try:
        workflow = _owned_workflow(session, workflow_id, owner_id)
        return _workflow_summary(session, workflow)
    finally:
        session.close()


async def patch_workflow(
    request: Request,
    workflow_id: str,
    payload: WorkflowPatchRequest,
    workflow_repo: SqlAlchemyWorkflowRepository = Depends(_workflow_repository),
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "workflows:write")
    session = session_factory()
    try:
        _owned_workflow(session, workflow_id, owner_id)
        latest = _latest_revision(session, workflow_id)
        if latest is None or int(latest.revision) != payload.expected_revision:
            stored = int(latest.revision) if latest is not None else 0
            raise HTTPException(
                status_code=409,
                detail={
                    "rejection_reason": "REVISION_CONFLICT",
                    "expected_revision": payload.expected_revision,
                    "current_revision": stored,
                },
            )
        latest_revision = int(latest.revision)
        latest_id = latest.id
        latest_status = str(latest.status)
        latest_hash = str(latest.canonical_hash)
    finally:
        session.close()

    doc = _load_document(payload.yaml_text, payload.document)
    compiled = _compile_or_422(doc)
    if compiled.canonical_hash == latest_hash:
        return WorkflowMutationResponse(
            workflow_id=workflow_id,
            changed=False,
            revision=latest_revision,
            revision_id=latest_id,
            revision_status=latest_status,
            canonical_hash=latest_hash,
        )
    try:
        # The expected_revision compare happens INSIDE the repo's insert
        # transaction (atomic compare-and-insert; RevisionConflict on loss).
        revision = workflow_repo.add_draft_revision(
            workflow_id,
            compiled.document.to_document_dict(),
            compiled.canonical_hash,
            expected_revision=payload.expected_revision,
        )
    except RevisionConflict as exc:
        current = _current_revision_number(session_factory, workflow_id)
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "REVISION_CONFLICT",
                "expected_revision": payload.expected_revision,
                "current_revision": current,
            },
        ) from exc
    except DomainConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WorkflowMutationResponse(
        workflow_id=workflow_id,
        changed=True,
        revision=int(revision.revision),
        revision_id=revision.id,
        revision_status=str(revision.status),
        canonical_hash=str(revision.canonical_hash),
    )


def _current_revision_number(session_factory: Any, workflow_id: str) -> int:
    session = session_factory()
    try:
        latest = _latest_revision(session, workflow_id)
        return int(latest.revision) if latest is not None else 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# lifecycle: activate / pause / resume / archive
# ---------------------------------------------------------------------------


async def activate_workflow(
    request: Request,
    workflow_id: str,
    payload: Optional[WorkflowActivateRequest] = None,
    workflow_repo: SqlAlchemyWorkflowRepository = Depends(_workflow_repository),
    session_factory: Any = Depends(_alerts_db),
):
    """Activate a revision.

    Body ``{"revision": N}`` (optional) activates that explicit revision —
    the rollback path, which also re-activates archived revisions and
    un-archives an archived workflow. Without a body the latest revision is
    activated, exactly as before.
    """
    _, owner_id = await _authorize(request, "workflows:activate")
    explicit_revision = payload.revision if payload is not None else None
    session = session_factory()
    try:
        _owned_workflow(session, workflow_id, owner_id)
        if explicit_revision is not None:
            target = _revision_by_number(session, workflow_id, explicit_revision)
            if target is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Revision {explicit_revision} not found",
                )
        else:
            target = _latest_revision(session, workflow_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Workflow has no revisions")
        # Re-validate the stored revision; an invalid revision never activates.
        try:
            doc = parse_workflow_dict(target.document)
            compile_document(doc)
        except WorkflowParseError as exc:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "issues": [_parse_issues(exc)[0].model_dump()]},
            ) from exc
        except WorkflowValidationError as exc:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "issues": [item.model_dump() for item in _validation_issues(exc)]},
            ) from exc
        target_id = target.id
    finally:
        session.close()

    try:
        # Accepts draft or archived revisions (rollback); also un-archives
        # the workflow itself when it was archived.
        revision = workflow_repo.activate_revision(workflow_id, target_id)
    except DomainConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Materialize one subscription per (alert, instrument) for the revision.
    created = 0
    session = session_factory()
    try:
        existing = set(
            session.execute(
                select(AlertSubscription.alert_id, AlertSubscription.instrument_key)
                .where(AlertSubscription.revision_id == revision.id)
            ).all()
        )
        for alert in doc.alerts:
            for instrument in doc.instruments:
                key = (alert.id, instrument.key())
                if key in existing:
                    continue
                session.add(
                    AlertSubscription(
                        revision_id=revision.id,
                        alert_id=alert.id,
                        stage_id=alert.source,
                        instrument_symbol=instrument.symbol,
                        instrument_exchange=instrument.exchange,
                        instrument_key=instrument.key(),
                        trigger=alert.trigger,
                        config=_subscription_config(alert),
                    )
                )
                created += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return WorkflowMutationResponse(
        workflow_id=workflow_id,
        revision=int(revision.revision),
        revision_id=revision.id,
        revision_status=str(revision.status),
        canonical_hash=str(revision.canonical_hash),
        subscriptions_created=created,
    )


def _set_subscription_state(session_factory: Any, revision: WorkflowRevision, state: str) -> int:
    session = session_factory()
    try:
        result = session.execute(
            update(AlertSubscription)
            .where(
                AlertSubscription.revision_id == revision.id,
                AlertSubscription.state.notin_(("expired", "completed")),
            )
            .values(state=state)
        )
        session.commit()
        return int(result.rowcount or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def pause_workflow(
    request: Request,
    workflow_id: str,
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "workflows:write")
    session = session_factory()
    try:
        _owned_workflow(session, workflow_id, owner_id)
        revision = session.execute(
            select(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id, WorkflowRevision.status == "active")
            .order_by(WorkflowRevision.activated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()
    if revision is None:
        raise HTTPException(status_code=409, detail="Workflow has no active revision")
    updated = _set_subscription_state(session_factory, revision, "paused")
    return WorkflowMutationResponse(workflow_id=workflow_id, revision=int(revision.revision), state="paused", updated=updated)


async def resume_workflow(
    request: Request,
    workflow_id: str,
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "workflows:write")
    session = session_factory()
    try:
        _owned_workflow(session, workflow_id, owner_id)
        revision = session.execute(
            select(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id, WorkflowRevision.status == "active")
            .order_by(WorkflowRevision.activated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()
    if revision is None:
        raise HTTPException(status_code=409, detail="Workflow has no active revision")
    updated = _set_subscription_state(session_factory, revision, "active")
    return WorkflowMutationResponse(workflow_id=workflow_id, revision=int(revision.revision), state="active", updated=updated)


async def archive_workflow(
    request: Request,
    workflow_id: str,
    session_factory: Any = Depends(_alerts_db),
):
    """Archive the workflow and every one of its revisions."""
    _, owner_id = await _authorize(request, "workflows:write")
    session = session_factory()
    try:
        workflow = _owned_workflow(session, workflow_id, owner_id)
        workflow.archived_at = datetime.now(timezone.utc)
        revisions_archived = int(
            session.execute(
                update(WorkflowRevision)
                .where(
                    WorkflowRevision.workflow_id == workflow_id,
                    WorkflowRevision.status != "archived",
                )
                .values(status="archived")
            ).rowcount or 0
        )
        session.commit()
        return WorkflowMutationResponse(
            workflow_id=workflow_id,
            archived=True,
            archived_at=_iso(workflow.archived_at),
            revisions_archived=revisions_archived,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# events / health / export
# ---------------------------------------------------------------------------


async def list_workflow_events(
    request: Request,
    workflow_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    workflow_repo: SqlAlchemyWorkflowRepository = Depends(_workflow_repository),
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "workflows:read")
    session = session_factory()
    try:
        _owned_workflow(session, workflow_id, owner_id)
        subscription_alert_ids = dict(
            session.execute(
                select(AlertSubscription.id, AlertSubscription.alert_id)
                .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
                .where(WorkflowRevision.workflow_id == workflow_id)
            ).all()
        )
    finally:
        session.close()

    subscription_ids = list(subscription_alert_ids.keys())
    total = 0
    if subscription_ids:
        session = session_factory()
        try:
            total = int(
                session.execute(
                    select(func.count())
                    .select_from(SignalEvent)
                    .where(SignalEvent.subscription_id.in_(subscription_ids))
                ).scalar()
                or 0
            )
        finally:
            session.close()

    events = workflow_repo.list_events(subscription_ids, limit=limit, offset=offset)
    return EventPage(
        workflow_id=workflow_id,
        limit=limit,
        offset=offset,
        total=total,
        events=[
            SignalEventItem(
                id=event.id,
                subscription_id=event.subscription_id,
                alert_id=subscription_alert_ids.get(event.subscription_id),
                occurrence_key=event.occurrence_key,
                fired_at=_iso(event.fired_at),
                evidence=dict(event.evidence or {}),
            )
            for event in events
        ],
    )


async def get_workflow_health(
    request: Request,
    workflow_id: str,
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "workflows:read")
    session = session_factory()
    try:
        _owned_workflow(session, workflow_id, owner_id)
        active = session.execute(
            select(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id, WorkflowRevision.status == "active")
            .order_by(WorkflowRevision.activated_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        subscriptions: List[SubscriptionHealth] = []
        subscription_rows: List[AlertSubscription] = []
        if active is not None:
            subscription_rows = list(
                session.execute(
                    select(AlertSubscription)
                    .where(AlertSubscription.revision_id == active.id)
                    .order_by(AlertSubscription.created_at.asc(), AlertSubscription.id.asc())
                ).scalars().all()
            )

        # Per-subscription freshness: max(evaluation_checkpoints.updated_at).
        all_subscription_ids = _workflow_subscription_ids(session, workflow_id)
        last_evaluated: Dict[str, str] = {}
        if all_subscription_ids:
            checkpoint_rows = session.execute(
                select(
                    EvaluationCheckpoint.subscription_id,
                    func.max(EvaluationCheckpoint.updated_at),
                )
                .where(EvaluationCheckpoint.subscription_id.in_(all_subscription_ids))
                .group_by(EvaluationCheckpoint.subscription_id)
            ).all()
            last_evaluated = {
                str(subscription_id): _iso(value)
                for subscription_id, value in checkpoint_rows
                if value is not None
            }
        subscriptions = [
            SubscriptionHealth(
                alert_id=row.alert_id,
                instrument_key=row.instrument_key,
                state=str(row.state),
                last_evaluated_at=last_evaluated.get(row.id),
            )
            for row in subscription_rows
        ]

        last_event_at: Optional[str] = None
        delivery_counts: Dict[str, int] = {}
        if all_subscription_ids:
            last_event_at = _iso(
                session.execute(
                    select(func.max(SignalEvent.fired_at)).where(
                        SignalEvent.subscription_id.in_(all_subscription_ids)
                    )
                ).scalar()
            )
            counts = session.execute(
                select(Delivery.status, func.count())
                .join(SignalEvent, Delivery.event_id == SignalEvent.id)
                .where(SignalEvent.subscription_id.in_(all_subscription_ids))
                .group_by(Delivery.status)
            ).all()
            delivery_counts = {str(status): int(count) for status, count in counts}

        return HealthResponse(
            workflow_id=workflow_id,
            active_revision=int(active.revision) if active is not None else None,
            subscriptions=subscriptions,
            last_event_at=last_event_at,
            delivery_counts=delivery_counts,
            stale_after_seconds=STALE_AFTER_SECONDS,
        )
    finally:
        session.close()


async def export_workflow(
    request: Request,
    workflow_id: str,
    revision: Optional[int] = Query(None, ge=1),
    session_factory: Any = Depends(_alerts_db),
):
    """Export the document of the active (default) or requested revision."""
    _, owner_id = await _authorize(request, "workflows:read")
    session = session_factory()
    try:
        _owned_workflow(session, workflow_id, owner_id)
        if revision is not None:
            target = _revision_by_number(session, workflow_id, revision)
            if target is None:
                raise HTTPException(status_code=404, detail=f"Revision {revision} not found")
        else:
            target = session.execute(
                select(WorkflowRevision)
                .where(WorkflowRevision.workflow_id == workflow_id, WorkflowRevision.status == "active")
                .order_by(WorkflowRevision.activated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if target is None:
                raise HTTPException(status_code=404, detail="Workflow has no active revision to export")
        return WorkflowExportResponse(
            workflow_id=workflow_id,
            revision=int(target.revision),
            canonical_hash=str(target.canonical_hash),
            document=dict(target.document or {}),
        )
    finally:
        session.close()


# channel serialization shared with the notifications router


def serialize_channel(channel: Any) -> ChannelResponse:
    return ChannelResponse(
        channel_id=channel.id,
        name=str(channel.name),
        provider=str(channel.provider),
        destination=dict(channel.destination or {}),
        secret_env=channel.secret_env,
        enabled=bool(channel.enabled),
        created_at=_iso(channel.created_at),
    )


router.add_api_route("/capabilities", workflow_capabilities, methods=["GET"])
router.add_api_route("/validate", validate_workflow, methods=["POST"], response_model=IssueEnvelope)
router.add_api_route("/preview", preview_workflow, methods=["POST"], response_model=PreviewResponse)
router.add_api_route("/import", import_workflow, methods=["POST"], response_model=WorkflowMutationResponse)
router.add_api_route("", create_workflow, methods=["POST"], response_model=WorkflowMutationResponse)
router.add_api_route("", list_workflows, methods=["GET"], response_model=WorkflowListResponse)
router.add_api_route("/{workflow_id}", get_workflow, methods=["GET"], response_model=WorkflowSummary)
router.add_api_route("/{workflow_id}", patch_workflow, methods=["PATCH"], response_model=WorkflowMutationResponse)
router.add_api_route("/{workflow_id}/activate", activate_workflow, methods=["POST"], response_model=WorkflowMutationResponse)
router.add_api_route("/{workflow_id}/pause", pause_workflow, methods=["POST"], response_model=WorkflowMutationResponse)
router.add_api_route("/{workflow_id}/resume", resume_workflow, methods=["POST"], response_model=WorkflowMutationResponse)
router.add_api_route("/{workflow_id}/archive", archive_workflow, methods=["POST"], response_model=WorkflowMutationResponse)
router.add_api_route("/{workflow_id}/events", list_workflow_events, methods=["GET"], response_model=EventPage)
router.add_api_route("/{workflow_id}/health", get_workflow_health, methods=["GET"], response_model=HealthResponse)
router.add_api_route("/{workflow_id}/export", export_workflow, methods=["GET"], response_model=WorkflowExportResponse)
