"""App-authenticated operator API for the alerts platform (Phase 6 6A.1).

The platform itself lives under ``/api/worker/*``, which is exempt from
app-cookie middleware and authenticates with a WORKER TOKEN. A browser has no
worker token and must not be given one (it would mean shipping a long-lived
credential into the page), so the operator UI needs a cookie-authenticated
surface. This router is that surface.

Design constraints, all deliberate:

- **Cookie auth only.** ``require_app_user`` on every route; no worker token is
  accepted here, and no route is mounted under an ``auth_exempt_path`` prefix,
  so the global middleware also gates it.
- **Server-side scope authorization.** The owner is decided by
  :func:`require_operator_scope` from a server allowlist; the client's ``scope``
  is a selection, never an authority.
- **No duplicated logic.** Serializers and payload builders are imported from
  the worker routers (``serialize_channel``, ``capabilities_payload``, …) and
  the same services are called, so the operator view cannot drift from the SDK
  view of the same data. Worker routes are untouched.
- **Nothing that sends by accident.** A test-send requires an explicit request;
  preview routes never persist.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from backend.api.routers.worker_workflows import (
    _alerts_db,
    _parse_issues,
    _preview_evaluate,
    _validation_issues,
    _warning_issues,
    serialize_channel,
    workflow_capabilities,
)
from backend.api.routers.worker_notifications import build_test_destination
from backend.api.schemas.workflows import (
    ChannelCreateRequest,
    IssueEnvelope,
    PreviewResponse,
    WorkflowCreateRequest,
    WorkflowPatchRequest,
    WorkflowValidateRequest,
    issue,
)
from backend.api.services.alerts_operator import (
    authorize_scope,
    list_scope_options,
    require_operator_scope,
)
from backend.api.services.csrf import enforce_same_origin
from backend.workflows.compiler import (
    WorkflowValidationError,
    collect_warnings,
    compile_document,
)
from backend.workflows.parser import (
    WorkflowParseError,
    document_to_yaml,
    parse_workflow_dict,
    parse_workflow_yaml,
)
from backend.workflows.repository import (
    DomainConflict,
    IdempotencyConflict,
    RevisionConflict,
    SqlAlchemyWorkflowRepository,
    Workflow as WorkflowModel,
    WorkflowRevision,
)

router = APIRouter(prefix="/alerts", tags=["Alerts (operator)"])

__all__ = ["router"]

#: Client-facing staleness contract, matching the worker health endpoint.
STALE_AFTER_SECONDS = 300


def _workflow_repo(request: Request, session_factory: Any = Depends(_alerts_db)):
    repository = getattr(request.app.state, "workflow_repository", None)
    if repository is not None:
        return repository
    return SqlAlchemyWorkflowRepository(session_factory)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------


@router.get("/scopes")
async def list_scopes(
    request: Request,
    session_factory: Any = Depends(_alerts_db),
):
    """Authorized alert scopes for this operator, and which hold data.

    Returns ONLY authorized scopes. The UI uses this to populate its scope
    picker so an operator whose alerts live under a token scope (``kite:paper-a``
    from the SDK, say) can select it instead of seeing an empty page and
    assuming nothing is configured — without the browser being able to reach any
    scope the server has not authorized.
    """
    from backend.app.auth import require_app_user

    user = require_app_user(request)
    options = list_scope_options(user, session_factory)
    return {
        "ok": True,
        "scopes": [
            {
                "scope": option.scope,
                "is_default": option.is_default,
                "has_data": option.has_data,
            }
            for option in options
        ],
        "note": (
            "Scopes come from ALERTS_OPERATOR_SCOPES on the server; the browser "
            "selects among them and can never request another. An empty list of "
            "workflows under an authorized scope means no alerts exist for it."
        ),
    }


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


@router.get("/capabilities")
async def operator_capabilities(
    request: Request,
    scope: str = Depends(require_operator_scope),
):
    """Everything the UI may offer, straight from the platform registry.

    Delegates to the worker route's builder so the operator UI and the SDK see
    the same declared limits, operators, timeframes and unsupported-feature
    flags — the UI must never hard-code what the backend actually supports.
    """
    _ = scope
    return await workflow_capabilities(request)


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------


def _enrich_workflow(
    session: Any, workflow: WorkflowModel, session_factory: Any
) -> Dict[str, Any]:
    """List-row view: lifecycle, definition summary, freshness, warnings.

    Warnings are computed from the STORED document so the list can flag a
    workflow whose rule can never emit (e.g. level-only with a transition
    trigger) without the operator opening it.
    """
    from backend.workflows.repository import AlertSubscription

    revisions = session.execute(
        select(WorkflowRevision)
        .where(WorkflowRevision.workflow_id == workflow.id)
        .order_by(WorkflowRevision.revision.desc())
    ).scalars().all()
    latest = revisions[0] if revisions else None
    active = next((r for r in revisions if r.status == "active"), None)

    summary: Dict[str, Any] = {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "archived": workflow.archived_at is not None,
        "archived_at": _iso(workflow.archived_at),
        "created_at": _iso(workflow.created_at),
        "updated_at": _iso(workflow.updated_at),
        "latest_revision": _revision_summary(latest),
        "active_revision": _revision_summary(active),
        "kind": "screener" if _is_screener(latest) else "alert",
        "instruments": [],
        "instrument_summary": None,
        "has_universe": False,
        "alerts": [],
        "channels": [],
        "last_evaluated_at": None,
        "last_tick_age_s": None,
        "stale": None,
        "warnings": [],
        "subscription_count": 0,
    }
    if latest is None:
        return summary

    try:
        document = parse_workflow_dict(latest.document)
    except (WorkflowParseError, ValueError, TypeError):
        summary["warnings"] = [
            {
                "where": "document",
                "code": "unparsable",
                "message": "the stored revision no longer parses; it will not activate",
                "severity": "error",
            }
        ]
        return summary

    keys = [instrument.key() for instrument in document.instruments]
    summary["instruments"] = keys
    summary["session"] = document.session
    summary["has_universe"] = document.universe is not None
    summary["instrument_summary"] = _instrument_summary(keys, document)
    summary["alerts"] = [
        {"id": alert.id, "source": alert.source, "trigger": alert.trigger}
        for alert in document.alerts
    ]
    summary["channels"] = sorted(
        {name for alert in document.alerts for name in (alert.channels or ())}
    )
    try:
        summary["warnings"] = [
            {
                "where": warning.where,
                "code": warning.code,
                "message": warning.message,
                "severity": warning.severity,
            }
            for warning in collect_warnings(document)
        ]
    except Exception:
        summary["warnings"] = []

    if active is not None:
        rows = session.execute(
            select(
                func.count(AlertSubscription.id),
                func.max(AlertSubscription.created_at),
            ).where(AlertSubscription.revision_id == active.id)
        ).one()
        summary["subscription_count"] = int(rows[0] or 0)
    return summary


def _revision_summary(revision: Optional[WorkflowRevision]) -> Optional[Dict[str, Any]]:
    if revision is None:
        return None
    return {
        "revision_id": revision.id,
        "revision": int(revision.revision),
        "status": revision.status,
        "canonical_hash": revision.canonical_hash,
        "created_at": _iso(revision.created_at),
        "activated_at": _iso(revision.activated_at),
    }


def _is_screener(revision: Optional[WorkflowRevision]) -> bool:
    if revision is None:
        return False
    document = revision.document if isinstance(revision.document, dict) else {}
    return bool(document.get("screener"))


def _instrument_summary(keys: List[str], document: Any) -> str:
    """A short, honest description of what this workflow covers."""
    if document.universe is not None:
        refs = [
            f"{ref.kind}:{ref.name}" for ref in list(document.universe.refs or ())
        ]
        base = f"universe ({', '.join(refs)})" if refs else "universe"
        if keys:
            return f"{base} + {len(keys)} instrument(s)"
        return base
    if not keys:
        return "no instruments"
    if len(keys) <= 3:
        return ", ".join(keys)
    return f"{', '.join(keys[:3])} +{len(keys) - 3} more"


@router.get("/workflows")
async def list_workflows(
    request: Request,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    include_archived: bool = Query(False),
):
    """Enriched workflow list for the operator UI."""
    _ = request
    with session_factory() as session:
        statement = select(WorkflowModel).where(WorkflowModel.owner_id == scope)
        if not include_archived:
            statement = statement.where(WorkflowModel.archived_at.is_(None))
        workflows = session.execute(
            statement.order_by(WorkflowModel.created_at.desc())
        ).scalars().all()
        rows = [
            _enrich_workflow(session, workflow, session_factory)
            for workflow in workflows
        ]
    return {"ok": True, "scope": scope, "workflows": rows}


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    include_yaml: bool = Query(True),
):
    """One workflow with its readable definition and optional YAML."""
    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
        payload = _enrich_workflow(session, workflow, session_factory)
        active_id = (payload.get("active_revision") or {}).get("revision_id")
        latest_id = (payload.get("latest_revision") or {}).get("revision_id")
        revision = session.get(WorkflowRevision, active_id or latest_id)
        payload["document"] = revision.document if revision is not None else None
        payload["revision_in_force"] = (
            _revision_summary(revision) if revision is not None else None
        )
        if include_yaml and revision is not None:
            try:
                document = parse_workflow_dict(revision.document)
                payload["yaml"] = document_to_yaml(document)
            except (WorkflowParseError, ValueError, TypeError) as exc:
                payload["yaml"] = None
                payload["yaml_error"] = f"{type(exc).__name__}: {exc}"
    return {"ok": True, "scope": scope, **payload}


@router.get("/workflows/{workflow_id}/export")
async def export_workflow(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    revision: Optional[int] = Query(None, ge=1),
):
    """The canonical document (JSON) and its readable YAML."""
    with session_factory() as session:
        workflow, stored = _owned_revision(session, workflow_id, scope, revision)
        _ = workflow
        document = parse_workflow_dict(stored.document)
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "revision": int(stored.revision),
            "canonical_hash": stored.canonical_hash,
            "document": stored.document,
            "yaml": document_to_yaml(document),
        }


def _owned_revision(
    session: Any, workflow_id: str, scope: str, revision: Optional[int]
) -> tuple:
    workflow = session.get(WorkflowModel, workflow_id)
    if workflow is None or workflow.owner_id != scope:
        raise HTTPException(status_code=404, detail="Workflow not found")
    statement = select(WorkflowRevision).where(
        WorkflowRevision.workflow_id == workflow_id
    )
    if revision is not None:
        statement = statement.where(WorkflowRevision.revision == revision)
    else:
        statement = statement.order_by(WorkflowRevision.revision.desc())
    stored = session.execute(statement.limit(1)).scalar_one_or_none()
    if stored is None:
        raise HTTPException(
            status_code=404, detail=f"Revision {revision} not found" if revision else "No revisions"
        )
    return workflow, stored


# ---------------------------------------------------------------------------
# validation / preview (pure)
# ---------------------------------------------------------------------------


def _document_from_payload(payload: Any):
    if (payload.yaml_text is None) == (payload.document is None):
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "issues": [
                    issue(
                        "request", "bad_request",
                        "provide exactly one of yaml_text or document",
                    ).model_dump()
                ],
            },
        )
    if payload.yaml_text is not None:
        return parse_workflow_yaml(payload.yaml_text)
    return parse_workflow_dict(payload.document)


@router.post("/workflows/validate")
async def operator_validate(
    request: Request,
    payload: WorkflowValidateRequest,
    scope: str = Depends(require_operator_scope),
):
    """Validate a document; warnings are reported with ``ok: true``.

    Same contract as the worker route, so the structured editor and the SDK see
    one truth — including the advisory level-vs-crossing warning.
    """
    _ = scope
    enforce_same_origin(request)
    try:
        document = _document_from_payload(payload)
    except WorkflowParseError as exc:
        return IssueEnvelope(ok=False, issues=_parse_issues(exc)).model_dump()
    except HTTPException as exc:
        raise exc
    try:
        compile_document(document)
    except WorkflowValidationError as exc:
        return IssueEnvelope(ok=False, issues=_validation_issues(exc)).model_dump()
    return IssueEnvelope(ok=True, issues=_warning_issues(document)).model_dump()


@router.post("/workflows/preview")
async def operator_preview(
    request: Request,
    payload: WorkflowValidateRequest,
    scope: str = Depends(require_operator_scope),
):
    """Pure dry-run. Persists nothing, activates nothing, sends nothing."""
    _ = scope
    enforce_same_origin(request)
    try:
        document = _document_from_payload(payload)
    except WorkflowParseError as exc:
        return PreviewResponse(ok=False, issues=_parse_issues(exc)).model_dump()
    try:
        compile_document(document)
    except WorkflowValidationError as exc:
        return PreviewResponse(
            ok=False,
            issues=_validation_issues(exc),
            instruments=[instrument.key() for instrument in document.instruments],
            stages=[stage.id for stage in document.stages],
            alerts=[alert.id for alert in document.alerts],
        ).model_dump()
    report = _preview_evaluate(document, payload.observations)
    return PreviewResponse(
        ok=True,
        issues=_warning_issues(document),
        instruments=[instrument.key() for instrument in document.instruments],
        stages=[stage.id for stage in document.stages],
        alerts=[alert.id for alert in document.alerts],
        **report,
        note=(
            "preview only: compiled and evaluated in memory over the supplied "
            "samples; nothing is persisted, no notification is sent, and no "
            "evaluation state changes. A preview cannot guarantee a future "
            "market event."
        ),
    ).model_dump()


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


@router.post("/workflows")
async def create_workflow(
    request: Request,
    payload: WorkflowCreateRequest,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Create a workflow + first revision for the authorized scope."""
    enforce_same_origin(request)
    document = _document_from_payload(payload)
    try:
        compiled = compile_document(document)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "issues": [item.model_dump() for item in _validation_issues(exc)],
            },
        ) from exc
    repository = SqlAlchemyWorkflowRepository(session_factory)
    name = str(payload.name or document.name).strip()
    try:
        workflow, revision = repository.create_workflow(
            scope, name, compiled.document.to_document_dict(),
            compiled.canonical_hash,
            idempotency_key=payload.idempotency_key,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "workflow_id": workflow.id,
        "name": workflow.name,
        "created": True,
        "revision": int(revision.revision),
        "revision_id": revision.id,
        "revision_status": revision.status,
        "canonical_hash": revision.canonical_hash,
    }


@router.patch("/workflows/{workflow_id}")
async def patch_workflow(
    request: Request,
    workflow_id: str,
    payload: WorkflowPatchRequest,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Add a draft revision under optimistic concurrency (409 on conflict)."""
    enforce_same_origin(request)
    document = _document_from_payload(payload)
    try:
        compiled = compile_document(document)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "issues": [item.model_dump() for item in _validation_issues(exc)],
            },
        ) from exc
    repository = SqlAlchemyWorkflowRepository(session_factory)
    try:
        revision = repository.add_draft_revision(
            workflow_id,
            compiled.document.to_document_dict(),
            compiled.canonical_hash,
            expected_revision=payload.expected_revision,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "rejection_reason": "REVISION_CONFLICT",
                "message": str(exc),
            },
        ) from exc
    except DomainConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="Workflow not found") from None
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "changed": True,
        "revision": int(revision.revision),
        "revision_id": revision.id,
        "revision_status": revision.status,
        "canonical_hash": revision.canonical_hash,
    }


@router.post("/workflows/{workflow_id}/archive")
async def archive_workflow(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Archive the workflow and every one of its revisions.

    Same effect as the worker route: events and deliveries are RETAINED so an
    archived workflow stays inspectable (spec E-30).
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    enforce_same_origin(request)
    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
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
        archived_at = _iso(workflow.archived_at)
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "archived": True,
        "archived_at": archived_at,
        "revisions_archived": revisions_archived,
    }


@router.post("/workflows/{workflow_id}/pause")
async def pause_workflow(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    return _set_state(
        request, workflow_id, scope, session_factory, "paused"
    )


@router.post("/workflows/{workflow_id}/resume")
async def resume_workflow(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    return _set_state(request, workflow_id, scope, session_factory, "active")


def _set_state(request: Request, workflow_id: str, scope: str, session_factory: Any, state: str):
    from sqlalchemy import update

    from backend.workflows.repository import AlertSubscription

    enforce_same_origin(request)
    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
        active = session.execute(
            select(WorkflowRevision).where(
                WorkflowRevision.workflow_id == workflow_id,
                WorkflowRevision.status == "active",
            )
        ).scalar_one_or_none()
        if active is None:
            raise HTTPException(status_code=409, detail="Workflow has no active revision")
        result = session.execute(
            update(AlertSubscription)
            .where(
                AlertSubscription.revision_id == active.id,
                AlertSubscription.state.notin_(("expired", "completed")),
            )
            .values(state=state)
        )
        session.commit()
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "revision": int(active.revision),
        "state": state,
        "updated": int(result.rowcount or 0),
    }


# ---------------------------------------------------------------------------
# health, events, deliveries
# ---------------------------------------------------------------------------


@router.get("/workflows/{workflow_id}/events")
async def workflow_events(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated signal events (retained for archived workflows too)."""
    from backend.workflows.repository import SignalEvent

    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
        total = session.execute(
            select(func.count(SignalEvent.id)).where(
                SignalEvent.workflow_id == workflow_id
            )
        ).scalar_one()
        rows = session.execute(
            select(SignalEvent)
            .where(SignalEvent.workflow_id == workflow_id)
            .order_by(SignalEvent.fired_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "limit": limit,
        "offset": offset,
        "total": int(total or 0),
        "events": [
            {
                "event_id": row.id,
                "subscription_id": row.subscription_id,
                "occurrence_key": row.occurrence_key,
                "fired_at": _iso(row.fired_at),
                "evidence": dict(row.evidence or {}),
            }
            for row in rows
        ],
    }


@router.get("/workflows/{workflow_id}/deliveries")
async def workflow_deliveries(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
):
    """Delivery history with per-attempt provider outcomes.

    This is the data that had NO endpoint before Phase 6: 'did my alert actually
    go out, and what did the provider say'. Provider acceptance is distinct from
    confirmed human receipt, and the response says so.
    """
    from backend.notifications.repository import (
        ChannelReference,
        Delivery,
        DeliveryAttempt,
    )
    from backend.workflows.repository import SignalEvent

    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
        statement = (
            select(Delivery, SignalEvent, ChannelReference)
            .join(SignalEvent, Delivery.event_id == SignalEvent.id)
            .outerjoin(ChannelReference, Delivery.channel_id == ChannelReference.id)
            .where(SignalEvent.workflow_id == workflow_id)
        )
        if status:
            statement = statement.where(Delivery.status == status)
        rows = session.execute(
            statement.order_by(Delivery.created_at.desc()).limit(limit).offset(offset)
        ).all()
        delivery_ids = [row[0].id for row in rows]
        attempts: Dict[str, List[Any]] = {}
        if delivery_ids:
            attempt_rows = session.execute(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id.in_(delivery_ids))
                .order_by(DeliveryAttempt.attempt_no.asc())
            ).scalars().all()
            for attempt in attempt_rows:
                attempts.setdefault(attempt.delivery_id, []).append(attempt)

    return {
        "ok": True,
        "workflow_id": workflow_id,
        "limit": limit,
        "offset": offset,
        "deliveries": [
            {
                "delivery_id": delivery.id,
                "event_id": delivery.event_id,
                "channel_id": delivery.channel_id,
                "channel_name": getattr(channel, "name", None),
                "provider": getattr(channel, "provider", None),
                "status": delivery.status,
                "attempts": int(delivery.attempts or 0),
                "next_attempt_at": _iso(delivery.next_attempt_at),
                "delivered_at": _iso(delivery.delivered_at),
                "last_error": delivery.last_error,
                "created_at": _iso(delivery.created_at),
                "fired_at": _iso(event.fired_at),
                "attempt_log": [
                    {
                        "attempt_no": attempt.attempt_no,
                        "outcome": attempt.outcome,
                        "detail": attempt.detail,
                        # Captured by 20260912_000017; null for history recorded
                        # before it, which is an honest gap rather than a guess.
                        "provider_id": attempt.provider_id,
                        "created_at": _iso(attempt.created_at),
                    }
                    for attempt in attempts.get(delivery.id, [])
                ],
            }
            for delivery, event, channel in rows
        ],
        "note": (
            "A 'delivered' status means the PROVIDER ACCEPTED the message; it is "
            "not confirmation that a human read it. Provider acceptance and "
            "human receipt are different things and are reported separately."
        ),
    }


@router.get("/deliveries/{delivery_id}/attempts")
async def delivery_attempts(
    request: Request,
    delivery_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """The full attempt log for one delivery, owner-scoped."""
    from backend.notifications.repository import Delivery, DeliveryAttempt
    from backend.workflows.repository import SignalEvent

    with session_factory() as session:
        row = session.execute(
            select(Delivery, SignalEvent)
            .join(SignalEvent, Delivery.event_id == SignalEvent.id)
            .where(Delivery.id == delivery_id)
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Delivery not found")
        delivery, event = row
        workflow = session.get(WorkflowModel, event.workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Delivery not found")
        attempts = session.execute(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery_id)
            .order_by(DeliveryAttempt.attempt_no.asc())
        ).scalars().all()
    return {
        "ok": True,
        "delivery_id": delivery_id,
        "status": delivery.status,
        "attempts": [
            {
                "attempt_no": attempt.attempt_no,
                "outcome": attempt.outcome,
                "detail": attempt.detail,
                "provider_id": attempt.provider_id,
                "created_at": _iso(attempt.created_at),
            }
            for attempt in attempts
        ],
    }


@router.post("/workflows/{workflow_id}/activate")
async def activate_workflow(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    revision: Optional[int] = Query(None, ge=1),
):
    """Activate a revision (latest by default; an explicit one is the rollback path).

    Re-validates the STORED document first, exactly as the worker route does, so
    an invalid stored revision never activates. Materialization delegates to
    ``EvaluationService.ensure_subscriptions`` — the single catalog-backed path —
    rather than a second copy of the loop.
    """
    from backend.workflows.service import EvaluationService

    enforce_same_origin(request)
    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
        statement = select(WorkflowRevision).where(
            WorkflowRevision.workflow_id == workflow_id
        )
        if revision is not None:
            statement = statement.where(WorkflowRevision.revision == revision)
        else:
            statement = statement.order_by(WorkflowRevision.revision.desc())
        target = session.execute(statement.limit(1)).scalar_one_or_none()
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"Revision {revision} not found" if revision else "Workflow has no revisions",
            )
        try:
            compile_document(parse_workflow_dict(target.document))
        except (WorkflowParseError, WorkflowValidationError) as exc:
            issues = (
                _parse_issues(exc) if isinstance(exc, WorkflowParseError)
                else _validation_issues(exc)
            )
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "issues": [item.model_dump() for item in issues]},
            ) from exc
        target_id = target.id

    repository = SqlAlchemyWorkflowRepository(session_factory)
    try:
        activated = repository.activate_revision(workflow_id, target_id)
    except DomainConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    created = EvaluationService(repository, session_factory).ensure_subscriptions(
        activated
    )
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "revision": int(activated.revision),
        "revision_id": activated.id,
        "revision_status": activated.status,
        "canonical_hash": activated.canonical_hash,
        "subscriptions_created": created,
        "note": (
            "A fresh activation is silent by design: an alert whose condition is "
            "already true initializes and does not notify unless the alert sets "
            "notify_if_already_true (spec E-9)."
        ),
    }


# ---------------------------------------------------------------------------
# instrument search
# ---------------------------------------------------------------------------


@router.get("/instruments/search")
async def search_instruments(
    request: Request,
    scope: str = Depends(require_operator_scope),
    q: str = Query(..., min_length=1, description="symbol or company-name fragment"),
    exchange: Optional[str] = Query(None),
    segment: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """Catalog-backed instrument search for the authoring UI.

    Deliberately NOT the legacy app ``/api/instruments/fuzzy-search``: that reads
    the old ``kite_instruments``/``kite_indices`` tables and returns
    ``tradingsymbol`` and ``exchange`` separately, without ``public_key``,
    ``expiry`` or ``lifecycle_status``. Workflows are keyed by the catalog's
    ``EXCHANGE:SYMBOL`` identity, so a search that cannot return it would make
    the UI guess at the one thing that must be exact.
    """
    _ = scope
    from backend.broker_api.instruments.catalog import (
        CatalogUnavailableError,
        InstrumentCatalog,
    )

    try:
        catalog = InstrumentCatalog()
        descriptors = catalog.search(
            q, exchange=exchange, segment=segment, limit=limit
        )
    except CatalogUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"instrument catalog unavailable: {exc}",
        ) from exc
    return {
        "ok": True,
        "query": q,
        "results": [
            {
                "public_key": descriptor.public_key,
                "symbol": descriptor.tradingsymbol,
                "exchange": descriptor.exchange,
                "segment": descriptor.segment,
                "name": descriptor.name,
                "instrument_type": descriptor.instrument_type,
                "expiry": descriptor.expiry.isoformat() if descriptor.expiry else None,
                "strike": descriptor.strike,
                "option_type": descriptor.option_type,
                "underlying": descriptor.underlying,
                "lot_size": descriptor.lot_size,
                "lifecycle_status": descriptor.lifecycle_status,
            }
            for descriptor in descriptors
        ],
        "note": (
            "public_key is the qualified EXCHANGE:SYMBOL identity workflows use; "
            "broker instrument tokens are deliberately not exposed, because the "
            "operator never needs one and they change when the catalog moves."
        ),
    }


# ---------------------------------------------------------------------------
# notification channels
# ---------------------------------------------------------------------------


def _notification_repo_dep(request: Request, session_factory: Any = Depends(_alerts_db)):
    from backend.notifications.repository import SqlAlchemyNotificationRepository

    repository = getattr(request.app.state, "notification_repository", None)
    if repository is not None:
        return repository
    return SqlAlchemyNotificationRepository(session_factory)


@router.get("/channels")
async def list_channels(
    request: Request,
    scope: str = Depends(require_operator_scope),
    notification_repo: Any = Depends(_notification_repo_dep),
):
    _ = request
    channels = notification_repo.list_channels(scope)
    return {
        "ok": True,
        "channels": [serialize_channel(channel).model_dump() for channel in channels],
        "note": (
            "secret_env names a server-side environment variable resolved at send "
            "time; the secret VALUE is never stored in or returned by this API."
        ),
    }


@router.post("/channels")
async def upsert_channel(
    request: Request,
    payload: ChannelCreateRequest,
    scope: str = Depends(require_operator_scope),
    notification_repo: Any = Depends(_notification_repo_dep),
):
    enforce_same_origin(request)
    name = str(payload.name or "").strip()
    provider = str(payload.provider or "").strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="channel name is required")
    if provider not in ("telegram", "ntfy"):
        raise HTTPException(
            status_code=422,
            detail=f"unsupported provider {provider!r}; supported: telegram, ntfy",
        )
    channel = notification_repo.upsert_channel(
        scope,
        name=name,
        provider=provider,
        destination=dict(payload.destination or {}),
        secret_env=payload.secret_env,
        enabled=bool(payload.enabled),
    )
    return {"ok": True, "channel": serialize_channel(channel).model_dump()}


@router.post("/channels/{channel_id}/test")
async def test_channel(
    request: Request,
    channel_id: str,
    scope: str = Depends(require_operator_scope),
    notification_repo: Any = Depends(_notification_repo_dep),
):
    """Send a REAL test message — an explicit, deliberate operator action.

    Never called from any read path or automatically: this exists so an operator
    can confirm a destination works, and it is the only route here that contacts
    a provider.
    """
    import os

    from backend.notifications.adapters import get_adapter

    enforce_same_origin(request)
    channel = notification_repo.get_channel(channel_id)
    if channel is None or str(getattr(channel, "owner_id", "")) != scope:
        raise HTTPException(status_code=404, detail="Channel not found")
    destination, secret_env = build_test_destination(channel)
    if secret_env and os.environ.get(secret_env) is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_env_secret",
                "secret_env": secret_env,
                "message": (
                    f"environment variable {secret_env} is not set on the server, "
                    "so this channel cannot send"
                ),
            },
        )
    adapter = get_adapter(channel.provider)
    body = None
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw.get("message")
    except Exception:
        body = None
    outcome = await adapter.send(
        destination,
        f"[Test] {channel.name}",
        body or f"[Test] channel '{channel.name}' is wired up correctly.",
    )
    return {
        "ok": True,
        "status": getattr(outcome, "status", "unknown"),
        "provider_id": getattr(outcome, "provider_id", None),
        "detail": getattr(outcome, "detail", ""),
    }
