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

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from backend.api.routers.worker_workflows import (
    _alerts_db,
    _parse_issues,
    _preview_evaluate,
    _validation_issues,
    _warning_issues,
    capabilities_payload,
    serialize_channel,
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

    Uses the worker route's *builder* (``capabilities_payload``) rather than
    calling the worker handler: that handler authorizes a worker bearer token,
    which a browser session cannot present — delegating to it made the route
    401 for an authenticated operator, which broke the entire authoring form
    (it renders from these capabilities).
    """
    _ = (request, scope)
    return {"ok": True, "capabilities": capabilities_payload()}


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------


def _lifecycle_state(session: Any, workflow: Any, payload: Dict[str, Any]) -> str:
    """The operator-facing lifecycle: archived | draft | paused | active.

    Derived from the revision status AND the subscription states, because a
    paused workflow keeps an active revision (that is what makes Resume cheap).
    """
    if workflow.archived_at is not None:
        return "archived"
    active = payload.get("active_revision") or {}
    if active.get("revision_id") is None:
        return "draft"
    from backend.workflows.repository import AlertSubscription

    states = [
        str(state)
        for (state,) in session.execute(
            select(AlertSubscription.state).where(
                AlertSubscription.revision_id == active.get("revision_id")
            )
        ).all()
    ]
    live = [state for state in states if state not in ("expired", "completed")]
    if live and all(state == "paused" for state in live):
        return "paused"
    return "active"


def _rule_summary(document: Any) -> Optional[Dict[str, Any]]:
    """The first stage's simple comparison, or None when it is not one.

    Deliberately narrow: a sequence, a group or an indicator operand is not
    described here, because a wrong one-line rule is worse than no rule.
    """
    stages = getattr(document, "stages", None) or []
    if not stages:
        return None
    stage = stages[0]
    # A sequence replaces the stage's own conditions; groups/any/not mean the
    # rule is not a single comparison.
    if getattr(stage, "sequence", None) is not None:
        return None
    conditions = getattr(stage, "conditions", None) or ()
    if len(conditions) != 1:
        return None
    condition = conditions[0]
    left = getattr(condition, "left", None)
    right = getattr(condition, "right", None)
    if str(getattr(left, "kind", "")) != "field" or str(getattr(right, "kind", "")) != "value":
        return None
    if getattr(left, "offset", None) is not None or getattr(right, "params", None):
        return None
    value = getattr(right, "value", None)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return {
        "field": str(getattr(left, "name", "") or ""),
        "operator": str(getattr(condition, "op", "") or ""),
        "value": numeric,
        "clock": str(getattr(stage, "clock", "") or ""),
        "timeframe": str(getattr(stage, "timeframe", "") or ""),
    }


def _enrich_workflow(
    session: Any, workflow: WorkflowModel, session_factory: Any
) -> Dict[str, Any]:
    """List-row view: lifecycle, definition summary, freshness, warnings.

    Warnings are computed from the STORED document so the list can flag a
    workflow whose rule can never emit (e.g. level-only with a transition
    trigger) without the operator opening it.
    """
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
        "warnings": [],
        "subscription_count": 0,
        # Freshness is ALWAYS a populated object, never null: a null here was
        # indistinguishable from "not computed", which is the same failure mode
        # as reporting an unknown state as healthy. The detail endpoint refines
        # this per subscription with tick-level precision.
        "freshness": _empty_freshness(),
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
    # A compact rule for the list row, so the operator reads "crosses above
    # 125,000" instead of opening every alert. Canonical pieces only: the UI owns
    # the wording, and anything that is not a simple field-vs-constant comparison
    # reports None rather than a half-description.
    summary["rule"] = _rule_summary(document)
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

    return summary


def _empty_freshness() -> Dict[str, Any]:
    return {
        "last_evaluated_at": None,
        "evaluation_age_s": None,
        "subscription_count": 0,
        "stale_subscriptions": 0,
        "stale": None,
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }


def _freshness_for_workflows(
    session: Any, workflow_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Evaluation recency per workflow, in ONE query for the whole page.

    Derived from ``evaluation_checkpoints.updated_at``, which advances on EVERY
    accepted observation (the service persists a checkpoint even for suppressed
    evaluations). So for an LTP subscription it doubles as "when did we last
    receive a usable tick", and for a candle subscription as "when did the last
    bar complete" — which is why one query can answer the list page's question
    without reading per-subscription state.

    A workflow with no subscriptions yet reports ``stale: None`` and a zero
    count, not ``False``: nothing has been evaluated, so "not stale" would be a
    claim about data that does not exist.
    """
    if not workflow_ids:
        return {}
    from backend.workflows.repository import (
        AlertSubscription,
        EvaluationCheckpoint,
        WorkflowRevision,
    )

    rows = session.execute(
        select(
            WorkflowRevision.workflow_id,
            func.count(AlertSubscription.id),
            func.max(EvaluationCheckpoint.updated_at),
        )
        .join(AlertSubscription, AlertSubscription.revision_id == WorkflowRevision.id)
        .outerjoin(
            EvaluationCheckpoint,
            EvaluationCheckpoint.subscription_id == AlertSubscription.id,
        )
        .where(WorkflowRevision.workflow_id.in_(workflow_ids))
        .group_by(WorkflowRevision.workflow_id)
    ).all()

    now = datetime.now(timezone.utc)
    result: Dict[str, Dict[str, Any]] = {}
    for workflow_id, count, latest in rows:
        total = int(count or 0)
        entry = _empty_freshness()
        entry["subscription_count"] = total
        if latest is not None:
            moment = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
            age = max(0.0, (now - moment).total_seconds())
            entry["last_evaluated_at"] = _iso(latest)
            entry["evaluation_age_s"] = round(age, 3)
            if age > STALE_AFTER_SECONDS:
                # Every subscription is at least this old, because `latest` is
                # the newest checkpoint across the workflow.
                entry["stale_subscriptions"] = total
                entry["stale"] = True
            else:
                entry["stale"] = False
        result[str(workflow_id)] = entry
    return result


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


def _lifecycle_states_for_workflows(
    session: Any, workflow_ids: List[str]
) -> Dict[str, str]:
    """Effective lifecycle per workflow, in one query for the whole page.

    Pause/Resume act on SUBSCRIPTIONS while the revision stays active, so the
    revision status alone would tell the list "active" for a workflow the
    operator explicitly paused — and the row would then offer the wrong action.
    """
    if not workflow_ids:
        return {}
    from backend.workflows.repository import AlertSubscription, WorkflowRevision

    rows = session.execute(
        select(
            WorkflowRevision.workflow_id,
            WorkflowRevision.id,
            AlertSubscription.state,
        )
        .outerjoin(AlertSubscription, AlertSubscription.revision_id == WorkflowRevision.id)
        .where(WorkflowRevision.workflow_id.in_(workflow_ids))
        .where(WorkflowRevision.status == "active")
    ).all()
    states: Dict[str, List[str]] = {}
    for workflow_id, _revision_id, state in rows:
        if state is None:
            continue
        states.setdefault(str(workflow_id), []).append(str(state))
    return {
        workflow_id: (
            "paused"
            if values and all(value == "paused" for value in values)
            else "active"
        )
        for workflow_id, values in states.items()
    }


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
        freshness = _freshness_for_workflows(
            session, [workflow.id for workflow in workflows]
        )
        lifecycles = _lifecycle_states_for_workflows(
            session, [workflow.id for workflow in workflows]
        )
    for row in rows:
        row["freshness"] = freshness.get(row["workflow_id"], _empty_freshness())
        # Archived and never-activated rows keep their own state; the batch only
        # distinguishes paused from active for rows that have an active revision.
        if row.get("archived"):
            row["lifecycle_state"] = "archived"
        elif row.get("active_revision") is None:
            row["lifecycle_state"] = "draft"
        else:
            row["lifecycle_state"] = lifecycles.get(row["workflow_id"], "active")
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
        # The detail view answers "is it armed, when was it last evaluated", so it
        # cannot report the empty defaults the list placeholders start from —
        # otherwise an alert with a live subscription reads as never evaluated.
        payload["freshness"] = _freshness_for_workflows(session, [workflow.id]).get(
            workflow.id, _empty_freshness()
        )
        payload["subscription_count"] = payload["freshness"].get("subscription_count", 0)
        active_id = (payload.get("active_revision") or {}).get("revision_id")
        latest_id = (payload.get("latest_revision") or {}).get("revision_id")
        revision = session.get(WorkflowRevision, active_id or latest_id)
        payload["document"] = revision.document if revision is not None else None
        payload["revision_in_force"] = (
            _revision_summary(revision) if revision is not None else None
        )
        # Pause/Resume act on the SUBSCRIPTIONS while the revision stays active,
        # so `active_revision.status` alone told the operator "ACTIVE" for a
        # workflow that was explicitly paused. Report the effective state, and
        # keep the raw revision status alongside it.
        payload["lifecycle_state"] = _lifecycle_state(session, workflow, payload)
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
        # Commit expires the instance on a default session, and the session is
        # closed on exit: read the value while it is still attached.
        revision_number = int(active.revision)
        updated = int(result.rowcount or 0)
        session.commit()
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "revision": revision_number,
        "state": state,
        "updated": updated,
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
# health
# ---------------------------------------------------------------------------


@router.get("/health")
async def platform_health(
    request: Request,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Worker-runtime health: the part the API cannot derive from the database.

    Reports ``available: false`` with a reason rather than zeros when the
    worker's health file cannot be read, so "nothing is quarantined" is never
    confused with "quarantine state is unknown".
    """
    from backend.api.services.alerts_runtime_health import runtime_health_view

    _ = scope
    return {"ok": True, "runtime": runtime_health_view()}


@router.get("/workflows/{workflow_id}/health")
async def workflow_health(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Per-workflow health: durable facts merged with runtime facts.

    The durable half (tick age, stale reason, continuity invalidation,
    last evaluation, delivery counters) comes from the database, so it works
    across the API/worker process split and survives a worker restart. The
    runtime half (quarantine, failure counts) is merged from the worker's
    published health when reachable and otherwise reported as unknown.

    Lifecycle and data-freshness are reported as SEPARATE fields on purpose: a
    workflow can be ``active`` (lifecycle) while ``stale`` (no fresh data), and
    collapsing them into one status is how a stale alert gets mistaken for a
    working one.
    """
    from backend.api.services.alerts_runtime_health import (
        derive_subscription_freshness,
        runtime_health_view,
    )
    from backend.workflows.advanced_repository import suppression_summary
    from backend.workflows.repository import AlertSubscription, EvaluationCheckpoint

    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
        active = session.execute(
            select(WorkflowRevision)
            .where(
                WorkflowRevision.workflow_id == workflow_id,
                WorkflowRevision.status == "active",
            )
            .order_by(WorkflowRevision.activated_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        subscriptions = []
        if active is not None:
            subscriptions = list(
                session.execute(
                    select(AlertSubscription)
                    .where(AlertSubscription.revision_id == active.id)
                    .order_by(AlertSubscription.created_at.asc(), AlertSubscription.id.asc())
                ).scalars().all()
            )

        all_ids = _workflow_subscription_ids(session, workflow_id)
        last_evaluated: Dict[str, Any] = {}
        states: Dict[str, Any] = {}
        if all_ids:
            # Newest checkpoint per subscription. This cannot be an aggregate:
            # ``state`` is JSONB and PostgreSQL has no max(jsonb), so the
            # previous ``func.max(state)`` passed on SQLite and 500'd here for
            # every workflow that has subscriptions.
            for subscription_id, updated, state in session.execute(
                select(
                    EvaluationCheckpoint.subscription_id,
                    EvaluationCheckpoint.updated_at,
                    EvaluationCheckpoint.state,
                )
                .where(EvaluationCheckpoint.subscription_id.in_(all_ids))
                .order_by(
                    EvaluationCheckpoint.subscription_id.asc(),
                    EvaluationCheckpoint.updated_at.desc(),
                )
            ).all():
                key = str(subscription_id)
                if key in last_evaluated:
                    continue
                last_evaluated[key] = updated
                states[key] = state

        document = None
        if active is not None:
            try:
                document = parse_workflow_dict(active.document)
            except (WorkflowParseError, ValueError, TypeError):
                document = None
        clock_by_stage = (
            {stage.id: stage.clock for stage in document.stages} if document else {}
        )
        alert_stage = (
            {alert.id: alert.source for alert in document.alerts} if document else {}
        )
        # Durable, per-workflow suppression counts. Unlike the runtime-only
        # counters these are PERSISTED in the same transaction that skipped the
        # event, so they survive a worker restart and need no health-file mount —
        # the API reads them from the database directly.
        suppressions = suppression_summary(session, workflow_id=workflow_id)

    now = datetime.now(timezone.utc)
    runtime = runtime_health_view()
    quarantined = runtime.get("quarantined") or {}
    failures = runtime.get("subscription_failures") or {}

    rows = []
    for sub in subscriptions:
        freshness = derive_subscription_freshness(
            state=states.get(sub.id),
            clock=clock_by_stage.get(alert_stage.get(sub.alert_id, "")),
            now=now,
            stale_after_s=STALE_AFTER_SECONDS,
        )
        evaluated_at = last_evaluated.get(sub.id)
        evaluated_age = None
        if evaluated_at is not None:
            moment = evaluated_at if evaluated_at.tzinfo else evaluated_at.replace(tzinfo=timezone.utc)
            evaluated_age = round(max(0.0, (now - moment).total_seconds()), 3)
        rows.append(
            {
                "subscription_id": sub.id,
                "alert_id": sub.alert_id,
                "instrument_key": sub.instrument_key,
                "state": str(sub.state),
                "last_evaluated_at": _iso(evaluated_at),
                "evaluation_age_s": evaluated_age,
                **freshness,
                "quarantined_until": quarantined.get(sub.id),
                "failures": (failures.get(sub.id) or {}).get("failures", 0),
                "last_error": (failures.get(sub.id) or {}).get("last_error"),
                "last_failure_at": (failures.get(sub.id) or {}).get("last_failure_at"),
            }
        )

    return {
        "ok": True,
        "workflow_id": workflow_id,
        "lifecycle": {
            "active": workflow.archived_at is None and active is not None,
            "archived": workflow.archived_at is not None,
            "archived_at": _iso(workflow.archived_at),
            "active_revision": _revision_summary(active),
        },
        "subscriptions": rows,
        "runtime": runtime,
        "suppressions": suppressions,
        "note": (
            "'suppressions' are DURABLE per-reason counts (for example "
            "'session_cap'), persisted when the notification was skipped, so "
            "they are available without the worker health file; the counters "
            "inside 'runtime' are in-memory worker facts and are UNKNOWN — not "
            "zero — when that file is unreadable. "
            "'lifecycle.active' is whether this workflow is switched on; the "
            "per-subscription 'stale'/'stale_reason' fields are whether fresh "
            "data is arriving. They are different questions — an active "
            "workflow can be receiving nothing, and the tick age is derived "
            "from a stored receipt timestamp so it grows without any tick."
        ),
    }


def _workflow_subscription_ids(session: Any, workflow_id: str) -> List[str]:
    from backend.workflows.repository import AlertSubscription

    return list(
        session.execute(
            select(AlertSubscription.id)
            .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
            .where(WorkflowRevision.workflow_id == workflow_id)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# worker tokens (scope-aligned)
# ---------------------------------------------------------------------------

#: The alerts-platform actions a token minted from this surface may carry.
#: Deliberately excludes every execution action (`intents:submit`,
#: `risk:update`, `runs:*`, `gtt:*`): the alerts feature must not become a way
#: to obtain an order-placing credential, and a preset that could would be a
#: privilege escalation dressed as a convenience.
ALERTS_TOKEN_ACTIONS = frozenset(
    {
        "workflows:read",
        "workflows:write",
        "workflows:activate",
        "notifications:test",
        "signals:read",
        "signals:admin",
    }
)

#: Presets mirroring §7's least-privilege list. The server owns this mapping so
#: the UI cannot invent a broader preset than the backend will honour.
TOKEN_PRESETS: Dict[str, List[str]] = {
    "alerts_authoring": ["workflows:read", "workflows:write", "workflows:activate"],
    "alerts_read_only": ["workflows:read"],
    "notifications": ["workflows:read", "notifications:test"],
    "external_producers": ["signals:read", "signals:admin"],
}

#: Modes a token minted here may carry. `live` is excluded because alerts never
#: place orders, so an alerts token has no reason to be live-capable — and
#: excluding it means a leaked alerts credential cannot reach a broker account.
ALERTS_TOKEN_MODES = ("paper", "dry_run")


@router.get("/tokens/presets")
async def token_presets(
    request: Request,
    scope: str = Depends(require_operator_scope),
):
    """The presets and their exact action sets, from the server.

    Returned by the API rather than duplicated in the client so a preset can
    never drift from what the backend will actually grant.
    """
    from backend.api.services.alerts_operator import default_scope
    from backend.app.auth import require_app_user

    _ = scope
    user = require_app_user(request)
    return {
        "ok": True,
        "presets": [
            {
                "id": preset_id,
                "actions": actions,
                "description": _PRESET_DESCRIPTIONS.get(preset_id, ""),
            }
            for preset_id, actions in TOKEN_PRESETS.items()
        ],
        "all_actions": sorted(ALERTS_TOKEN_ACTIONS),
        "modes": list(ALERTS_TOKEN_MODES),
        "account_scope": default_scope(user),
        "note": (
            "the account scope is fixed server-side to the authorized scope and "
            "is not accepted from the client; a token's scope IS the alerts "
            "owner_id, so a token minted for another scope would read a "
            "different owner's alerts. No execution action is offered here."
        ),
    }


_PRESET_DESCRIPTIONS = {
    "alerts_authoring": "create, edit and switch alerts on or off",
    "alerts_read_only": "read alerts, events and delivery history",
    "notifications": "read alerts and send a channel test message",
    "external_producers": "register producers and issue their credentials",
}


@router.get("/tokens")
async def list_operator_tokens(
    request: Request,
    scope: str = Depends(require_operator_scope),
):
    """Worker tokens with their scope, and whether it matches this operator's."""
    from backend.api.services.alerts_operator import allowed_scopes
    from backend.app.auth import require_app_user
    from backend.api.routers.worker_shared import _repo

    _ = scope
    user = require_app_user(request)
    tokens = await _repo(request).list_tokens()
    authorized = set(allowed_scopes(user))
    return {
        "ok": True,
        "tokens": [
            {
                "token_id": token.get("token_id"),
                "label": token.get("name"),
                "account_scope": token.get("account_scope"),
                "allowed_actions": list(token.get("allowed_actions") or []),
                "allowed_modes": list(token.get("allowed_modes") or []),
                "status": token.get("status"),
                "created_at": token.get("created_at"),
                "last_used_at": token.get("last_used_at"),
                "expires_at": token.get("expires_at"),
                # Surfaced as a warning rather than hidden: a token pointing at
                # another scope silently sees an empty alerts view, which reads
                # as "nothing is configured" instead of "wrong scope".
                "scope_matches_operator": token.get("account_scope") in authorized,
            }
            for token in tokens
        ],
        "authorized_scopes": sorted(authorized),
    }


@router.post("/tokens")
async def create_operator_token(
    request: Request,
    payload: Dict[str, Any],
    scope: str = Depends(require_operator_scope),
):
    """Mint a worker token for the AUTHORIZED scope. One-time reveal.

    ``account_scope`` is taken from the authorization result, never the body: a
    body-supplied scope is ignored outright rather than validated, so there is
    no path on which a client value reaches the token row.
    """
    from backend.api.routers.worker_shared import _repo
    from backend.api.schemas.worker import WorkerTokenCreateRequest

    enforce_same_origin(request)

    preset = payload.get("preset")
    if preset is not None:
        if preset not in TOKEN_PRESETS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown preset {preset!r}; known: {', '.join(sorted(TOKEN_PRESETS))}",
            )
        actions = list(TOKEN_PRESETS[preset])
    else:
        actions = list(payload.get("allowed_actions") or [])
        if not actions:
            raise HTTPException(
                status_code=422,
                detail="provide either a 'preset' or an explicit 'allowed_actions' list",
            )
    unsupported = sorted(set(actions) - ALERTS_TOKEN_ACTIONS)
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_actions",
                "unsupported": unsupported,
                "message": (
                    "this surface mints alerts tokens only; execution actions "
                    "are not offered here"
                ),
            },
        )

    modes = [str(mode).strip().lower() for mode in (payload.get("allowed_modes") or ["paper"])]
    unsupported_modes = sorted(set(modes) - set(ALERTS_TOKEN_MODES))
    if unsupported_modes:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_modes",
                "unsupported": unsupported_modes,
                "message": (
                    "alerts tokens may only carry paper or dry_run: alerts never "
                    "place orders, so a live-capable alerts token would be "
                    "capability the feature does not need"
                ),
            },
        )

    label = str(payload.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=422, detail="a label is required so the token is identifiable later")

    create_payload = WorkerTokenCreateRequest(
        name=label,
        account_scope=scope,
        allowed_actions=sorted(set(actions)),
        allowed_modes=modes,
    )
    raw_token = f"kwa_{secrets.token_urlsafe(32)}"
    token_id = f"worker_{uuid.uuid4().hex[:16]}"
    record = await _repo(request).create_token(
        create_payload, raw_token=raw_token, token_id=token_id
    )
    return {
        "ok": True,
        "token": raw_token,
        "token_id": record.get("token_id"),
        "account_scope": record.get("account_scope"),
        "allowed_actions": list(record.get("allowed_actions") or []),
        "allowed_modes": list(record.get("allowed_modes") or []),
        "reveal_once": True,
        "note": (
            "this is the only time the token value is returned; it is not "
            "stored in readable form and cannot be shown again. Its scope is "
            "the authorized alerts scope, so it sees exactly the workflows this "
            "operator sees."
        ),
    }


@router.post("/tokens/{token_id}/revoke")
async def revoke_operator_token(
    request: Request,
    token_id: str,
    scope: str = Depends(require_operator_scope),
):
    from backend.api.routers.worker_shared import _repo

    enforce_same_origin(request)
    record = await _repo(request).revoke_token(token_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Worker token not found")
    return {"ok": True, "token": record}


# ---------------------------------------------------------------------------
# yaml (revision addressed)
# ---------------------------------------------------------------------------


@router.get("/workflows/{workflow_id}/revisions")
async def list_workflow_revisions(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    limit: int = Query(50, ge=1, le=500),
):
    """Revision history, newest first — what a rollback can roll back TO.

    ``POST /activate?revision=N`` accepts a revision number, but until this
    route existed nothing told the operator WHICH revisions existed: the detail
    payload carries only the latest and the active one, so a rollback would have
    meant guessing a number. Read-only and owner-scoped, like every other route
    here.
    """
    _ = request
    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Workflow not found")
        revisions = session.execute(
            select(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id)
            .order_by(WorkflowRevision.revision.desc())
            .limit(limit)
        ).scalars().all()
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "limit": limit,
        "revisions": [_revision_summary(revision) for revision in revisions],
    }


@router.get("/workflows/{workflow_id}/yaml")
async def workflow_yaml(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    revision: Optional[int] = Query(None, ge=1),
):
    """The readable YAML for one revision (latest when unspecified).

    ``revision`` addresses history explicitly, which is what a rollback or a
    diff view needs — the detail route can only render the definition in force.
    """
    with session_factory() as session:
        _workflow, stored = _owned_revision(session, workflow_id, scope, revision)
        document = parse_workflow_dict(stored.document)
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "revision": int(stored.revision),
            "revision_id": stored.id,
            "revision_status": stored.status,
            "canonical_hash": stored.canonical_hash,
            "yaml": document_to_yaml(document),
            "round_trip": (
                "this YAML parses back to the same canonical hash shown above; "
                "the structured editor, the canvas and this text are views of "
                "one definition"
            ),
        }


# ---------------------------------------------------------------------------
# canvas layout
# ---------------------------------------------------------------------------


def _layout_repo(request: Request, session_factory: Any = Depends(_alerts_db)):
    """Injectable so tests can swap it, mirroring the other repositories."""
    repository = getattr(request.app.state, "canvas_layout_repository", None)
    if repository is not None:
        return repository
    from backend.workflows.canvas_layout import CanvasLayoutRepository

    return CanvasLayoutRepository(session_factory)


def _owned_workflow_or_404(session: Any, workflow_id: str, scope: str) -> Any:
    workflow = session.get(WorkflowModel, workflow_id)
    if workflow is None or workflow.owner_id != scope:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


@router.get("/workflows/{workflow_id}/layout")
async def get_canvas_layout(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    repo: Any = Depends(_layout_repo),
):
    """Saved canvas positions for this workflow, plus the node-id contract.

    Positions are returned independent of the document, and the contract is
    included so the canvas never has to hard-code the namespace rules — the
    server that validates writes is the same one that documents them.
    """
    with session_factory() as session:
        _owned_workflow_or_404(session, workflow_id, scope)
    entries = repo.list_layout(scope, workflow_id)
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "nodes": [
            {
                "node_id": entry.node_id,
                "x": entry.x,
                "y": entry.y,
                "collapsed": entry.collapsed,
                "updated_at": entry.updated_at,
            }
            for entry in entries
        ],
        "contract": _layout_contract(),
    }


def _layout_contract() -> Dict[str, Any]:
    from backend.workflows.canvas_layout import (
        CANVAS_NAMESPACES,
        MAX_ABS_COORDINATE,
        MAX_NODE_ID_LENGTH,
    )

    return {
        "namespaces": list(CANVAS_NAMESPACES),
        "node_id_format": "<namespace>:<id>",
        "max_abs_coordinate": MAX_ABS_COORDINATE,
        "max_node_id_length": MAX_NODE_ID_LENGTH,
        "note": (
            "Layout is stored separately from the definition and can never "
            "change a canonical hash: saving positions creates no revision. "
            "Node ids are namespaced because stage ids, alert ids and channel "
            "names are separate id spaces that can legally collide, so a bare "
            "id would let two different nodes share one saved position."
        ),
    }


@router.put("/workflows/{workflow_id}/layout")
async def put_canvas_layout(
    request: Request,
    workflow_id: str,
    payload: Dict[str, Any],
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    repo: Any = Depends(_layout_repo),
):
    """Merge node positions. Cosmetic: no revision, no hash change.

    Deliberately NOT a PATCH on the workflow document. A layout-only change must
    not create a draft revision, so it does not participate in
    ``expected_revision`` conflict handling at all — two operators moving nodes
    is not a definition conflict.
    """
    from backend.workflows.canvas_layout import CanvasNodeIdError

    enforce_same_origin(request)
    with session_factory() as session:
        _owned_workflow_or_404(session, workflow_id, scope)

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "error": "no_nodes",
                "message": "provide a non-empty 'nodes' list of {node_id, x, y[, collapsed]}",
            },
        )
    try:
        entries = repo.upsert_layout(scope, workflow_id, raw_nodes)
    except CanvasNodeIdError as exc:
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "error": "invalid_node", "message": str(exc)},
        ) from exc
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "saved": len(raw_nodes),
        "nodes": [
            {
                "node_id": entry.node_id,
                "x": entry.x,
                "y": entry.y,
                "collapsed": entry.collapsed,
                "updated_at": entry.updated_at,
            }
            for entry in entries
        ],
        "note": (
            "positions saved; the workflow definition was not modified, so no "
            "revision was created and the canonical hash is unchanged"
        ),
    }


@router.post("/workflows/{workflow_id}/layout/delete")
async def delete_canvas_layout(
    request: Request,
    workflow_id: str,
    payload: Dict[str, Any],
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    repo: Any = Depends(_layout_repo),
):
    """Forget positions for nodes that left the document (explicit delete).

    POST rather than DELETE-with-body so the operation is a JSON mutation like
    every other one here and goes through the same origin assertion.
    """
    from backend.workflows.canvas_layout import CanvasNodeIdError

    enforce_same_origin(request)
    with session_factory() as session:
        _owned_workflow_or_404(session, workflow_id, scope)
    node_ids = payload.get("node_ids")
    if not isinstance(node_ids, list) or not node_ids:
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "error": "no_nodes", "message": "provide 'node_ids'"},
        )
    try:
        removed = repo.delete_layout(scope, workflow_id, node_ids)
    except CanvasNodeIdError as exc:
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "error": "invalid_node", "message": str(exc)},
        ) from exc
    return {"ok": True, "workflow_id": workflow_id, "removed": removed}


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
