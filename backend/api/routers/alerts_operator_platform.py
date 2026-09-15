"""App-authenticated operator routes for universes, screeners and producers.

Split from ``alerts_operator.py`` purely for size: same router prefix, same
authorization dependency, same CSRF rule, same owner scoping. Nothing here
re-implements platform logic — every handler calls the SAME service the worker
route calls, with the owner taken from the server-side authorization result.

Why not delegate to the worker route handlers directly: those call
``_authorize(request, action)``, which requires a WORKER TOKEN the browser must
never hold, and substituting a module-global authorization for the duration of a
request would leak across concurrent requests (the substitution would have to
span ``await`` points). Importing the shared serializers and calling the shared
services is the approach the plan asks for and the only one that is safe under
concurrency.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from backend.api.routers.worker_workflows import _alerts_db
from backend.api.routers.worker_universes import (
    _call,
    _revision_summary as _universe_revision_summary,
    _universe_service,
)
from backend.api.schemas.universes import (
    PreviewResponse,
    ResolveResponse,
    UniverseCreateRequest,
    UniverseDetailResponse,
    UniverseListResponse,
    UniverseMutationResponse,
    UniversePreviewRequest,
    UniverseRevisionItem,
    UniverseRevisionsResponse,
    UniverseSummary,
)
from backend.api.schemas.screeners import ScreenerPreviewRequest
from backend.api.services.alerts_operator import require_operator_scope
from backend.api.services.csrf import enforce_same_origin
from backend.api.routers.worker_screeners import preview_screener_for
from backend.api.routers.worker_signals import (
    ProducerCreateRequest,
    _producer_payload,
)

router = APIRouter(prefix="/alerts", tags=["Alerts (operator)"])

__all__ = ["router"]


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# universes
# ---------------------------------------------------------------------------


@router.get("/universes")
async def list_universes(
    request: Request,
    scope: str = Depends(require_operator_scope),
    service: Any = Depends(_universe_service),
):
    """Every universe owned by the authorized scope, with its latest revision.

    Read-only: resolution is a separate, explicit action, because resolving
    consults the live catalog and writes a revision.
    """
    _ = request
    universes = _call(service.list_universes, scope)
    items = []
    for universe in universes:
        latest = _call(service.latest_revision, scope, str(universe["name"]))
        items.append(
            UniverseSummary(
                universe_id=str(universe["universe_id"]),
                name=str(universe["name"]),
                kind=str(universe["kind"]),
                source_config=dict(universe.get("source_config") or {}),
                enabled=bool(universe.get("enabled", True)),
                created_at=universe.get("created_at"),
                updated_at=universe.get("updated_at"),
                latest_revision=_universe_revision_summary(latest),
            )
        )
    return UniverseListResponse(universes=items)


@router.post("/universes", status_code=201)
async def create_universe(
    request: Request,
    payload: UniverseCreateRequest,
    scope: str = Depends(require_operator_scope),
    service: Any = Depends(_universe_service),
):
    enforce_same_origin(request)
    created = _call(
        service.create_universe,
        scope,
        payload.name,
        payload.kind,
        dict(payload.source_config or {}),
    )
    return UniverseMutationResponse(
        universe_id=str(created["universe_id"]),
        name=str(created["name"]),
        kind=str(created["kind"]),
        source_config=dict(created.get("source_config") or {}),
        enabled=bool(created.get("enabled", True)),
        created_at=created.get("created_at"),
        updated_at=created.get("updated_at"),
    )


@router.post("/universes/preview")
async def preview_universe(
    request: Request,
    payload: UniversePreviewRequest,
    scope: str = Depends(require_operator_scope),
    service: Any = Depends(_universe_service),
):
    """Would-be membership WITHOUT persisting anything the operator can see.

    The service is the same one the worker route calls, so a preview cannot
    diverge from what resolution would actually produce. Any revision the
    service needs internally is committed in its own transaction and is not a
    subscription or an alert state change.
    """
    enforce_same_origin(request)
    preview = _call(
        service.preview_membership,
        scope,
        payload.kind,
        dict(payload.source_config or {}),
    )
    return PreviewResponse(
        kind=str(preview["kind"]),
        members=list(preview["members"]),
        rejected=list(preview["rejected"]),
        source_generation=preview.get("source_generation"),
        coverage=dict(preview["coverage"]),
    )


@router.get("/universes/{name}")
async def get_universe(
    request: Request,
    name: str,
    scope: str = Depends(require_operator_scope),
    service: Any = Depends(_universe_service),
):
    _ = request
    universe = _call(service.get_universe, scope, name)
    latest = _call(service.latest_revision, scope, name)
    return UniverseDetailResponse(
        universe_id=str(universe["universe_id"]),
        name=str(universe["name"]),
        kind=str(universe["kind"]),
        source_config=dict(universe.get("source_config") or {}),
        enabled=bool(universe.get("enabled", True)),
        created_at=universe.get("created_at"),
        updated_at=universe.get("updated_at"),
        latest_revision=_universe_revision_summary(latest),
        latest_members=list(latest["members"]) if latest else None,
        latest_coverage=dict(latest["coverage"]) if latest else None,
    )


@router.get("/universes/{name}/revisions")
async def list_universe_revisions(
    request: Request,
    name: str,
    scope: str = Depends(require_operator_scope),
    service: Any = Depends(_universe_service),
    limit: int = Query(50, ge=1, le=500),
):
    _ = request
    universe = _call(service.get_universe, scope, name)
    revisions = _call(service.list_revisions, scope, name, limit=limit)
    return UniverseRevisionsResponse(
        universe_id=str(universe["universe_id"]),
        name=str(universe["name"]),
        limit=limit,
        revisions=[UniverseRevisionItem(**revision) for revision in revisions],
    )


@router.post("/universes/{name}/resolve")
async def resolve_universe(
    request: Request,
    name: str,
    scope: str = Depends(require_operator_scope),
    service: Any = Depends(_universe_service),
):
    """Resolve membership and persist a revision — an explicit operator action."""
    enforce_same_origin(request)
    resolved = _call(service.resolve_membership, scope, name)
    return ResolveResponse(
        universe_id=str(resolved["universe_id"]),
        name=str(resolved["name"]),
        kind=str(resolved["kind"]),
        revision=int(resolved["revision"]),
        members=list(resolved["members"]),
        rejected=list(resolved["rejected"]),
        source_generation=resolved.get("source_generation"),
        coverage=dict(resolved["coverage"]),
    )


# ---------------------------------------------------------------------------
# screeners
# ---------------------------------------------------------------------------
#
# A screener IS a workflow with `kind: screener`, so the owner/scope rules are
# identical to the workflow routes. The shared helpers are imported from the
# worker screener router rather than copied, so the run payload the operator
# sees cannot drift from the one the SDK sees.


def _screener_helper(name: str):
    from backend.api.routers import worker_screeners

    return getattr(worker_screeners, name)


def _require_screener(session_factory: Any, workflow_id: str, scope: str) -> Any:
    """The workflow, asserting it is a screener owned by this scope.

    Two distinct 404-worthy facts collapse into "not found" deliberately: a
    foreign handler should not be able to tell a workflow it may not read from
    one that is not a screener.
    """
    from backend.workflows.repository import Workflow as WorkflowModel, WorkflowRevision

    with session_factory() as session:
        workflow = session.get(WorkflowModel, workflow_id)
        if workflow is None or workflow.owner_id != scope:
            raise HTTPException(status_code=404, detail="Screener not found")
        revision = session.execute(
            select(WorkflowRevision)
            .where(WorkflowRevision.workflow_id == workflow_id)
            .order_by(WorkflowRevision.revision.desc())
            .limit(1)
        ).scalar_one_or_none()
        if revision is None:
            raise HTTPException(status_code=404, detail="Screener not found")
        document = revision.document if isinstance(revision.document, dict) else {}
        if not document.get("screener"):
            raise HTTPException(
                status_code=409,
                detail="this workflow is not a screener",
            )
        return workflow, revision


@router.get("/screeners/{workflow_id}/runs")
async def list_screener_runs(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    _require_screener(session_factory, workflow_id, scope)
    repo = _screener_helper("_run_repo")(request)
    runs = repo.list_runs(scope, str(workflow_id), limit=limit, offset=offset)
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "runs": [_screener_helper("_run_out")(run).model_dump() for run in runs],
        "limit": limit,
        "offset": offset,
        "note": (
            "a partial run is labelled partial and is NOT a complete membership "
            "replacement: a downstream universe keeps the last complete "
            "revision until a complete run supersedes it"
        ),
    }


@router.get("/screener-runs/{run_id}")
async def get_screener_run(
    request: Request,
    run_id: str,
    scope: str = Depends(require_operator_scope),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """One run with its ranked members.

    ``exclusion_reason`` is carried through per member so the operator can see
    WHY a symbol did not qualify instead of inferring it from absence.
    """
    repo = _screener_helper("_run_repo")(request)
    run = repo.get_run(str(run_id))
    if run is None or str(run.owner_id) != scope:
        raise HTTPException(status_code=404, detail="screener run not found")
    members = repo.run_members(run.id)
    page = members[offset : offset + limit]
    return {
        "ok": True,
        "run": _screener_helper("_run_out")(run).model_dump(),
        "members": [
            {
                "instrument_key": member.instrument_key,
                "passed": bool(member.passed),
                "exclusion_reason": member.exclusion_reason,
                "rank": member.rank,
                "score": member.score,
                "values": dict(member.values or {}),
            }
            for member in page
        ],
        "member_count": len(members),
        "limit": limit,
        "offset": offset,
    }


def _screener_warm_context(request: Request, scope: str, workflow_id: str):
    """Shared setup for the warm/status endpoints: owned screener + members.

    Uses the LATEST revision of an owned screener, not the active one: warming
    and inspecting candle availability are statements about the definition, and
    an operator must be able to check a paused or archived screener's data. Only
    *running* it requires an active revision.
    """
    from backend.workflows.parser import parse_workflow_dict

    _workflow, revision = _require_screener(
        _screener_helper("_session_factory")(request), workflow_id, scope
    )
    scheduler = _screener_helper("_scheduler")(request)
    document = parse_workflow_dict(revision.document)
    resolved = scheduler.resolve_members(scope, document)
    members = sorted(str(key) for key in resolved.get("__members__", []) or [])
    return scheduler, document, members, resolved


@router.get("/screeners/{workflow_id}/data-status")
async def screener_data_status(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    idempotency_key: Optional[str] = Query(None),
):
    """Whether each member of this screener has the candle history it needs.

    Read-only: answers "is this universe warming, unavailable, stale or
    complete" without running the scan, so the operator can tell missing data
    apart from a genuine zero-match result.
    """
    scheduler, document, members, resolved = _screener_warm_context(
        request, scope, workflow_id
    )
    warmer = getattr(scheduler.pipeline, "warmer", None)
    if warmer is None:
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "warming_supported": False,
            "members": [],
            "note": "this deployment has no candle warmer configured",
        }
    status = warmer.data_status(
        members, session=str(getattr(document, "session", "") or "")
    )
    needed = [row for row in status if row["warming"]]
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "warming_supported": True,
        "resolution_ok": bool(resolved.get("__resolution_ok__", True)),
        "member_count": len(members),
        "required_bars": warmer.required_bars,
        "members_needing_candles": len(needed),
        "status": (
            "complete"
            if members and not needed
            else ("warming" if needed else "unavailable")
        ),
        "members": status,
        "note": (
            "bars counts FINAL daily candles only; a forming session is excluded "
            "until its exchange has closed"
        ),
    }


@router.post("/screeners/{workflow_id}/warm-candles")
async def warm_screener_candles(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
):
    """Bounded, idempotent candle acquisition for this screener's members.

    Only this universe's members are fetched, only the ones that still need
    history, and the whole call is bounded by member count and wall-clock time;
    anything left over is reported as skipped so a second call continues.
    """
    enforce_same_origin(request)
    scheduler, document, members, _resolved = _screener_warm_context(
        request, scope, workflow_id
    )
    warmer = getattr(scheduler.pipeline, "warmer", None)
    if warmer is None:
        raise HTTPException(
            status_code=503, detail="this deployment has no candle warmer configured"
        )
    outcome = await asyncio.to_thread(
        warmer.ensure_members, members, session=str(getattr(document, "session", "") or "")
    )
    return {
        "ok": True,
        "workflow_id": workflow_id,
        **outcome.to_coverage(),
    }


@router.post("/screeners/{workflow_id}/runs")
async def trigger_screener_run(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    idempotency_key: Optional[str] = Query(None),
):
    """Manual run. The same idempotency key returns the original run."""
    enforce_same_origin(request)
    _require_screener(session_factory, workflow_id, scope)
    scheduler = _screener_helper("_scheduler")(request)
    workflow, revision = _screener_helper("_owned_screener_revision")(
        request, scope, workflow_id
    )
    # Off the event loop: a run may warm bounded candle history (network I/O)
    # before evaluating, and blocking the API loop for that would stall every
    # other request.
    run = await asyncio.to_thread(
        scheduler.execute_manual, workflow, revision, idempotency_key=idempotency_key
    )
    if run is None:
        existing = scheduler.run_repo.get_run_by_occurrence(
            scope, f"{workflow.id}:manual:{idempotency_key}"
        )
        return {
            "ok": True,
            "run_id": str(existing.id) if existing is not None else None,
            "status": "already_finalized",
            "detail": "the run for this idempotency key already exists",
        }
    return {"ok": True, "run_id": str(run.id), "status": str(run.status)}


@router.get("/screeners/{workflow_id}/events")
async def list_screener_events(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Attachment events: entry/exit/top_n/rank_delta firings for this screener."""
    _require_screener(session_factory, workflow_id, scope)
    events = _screener_helper("list_workflow_events")(
        _screener_helper("_session_factory")(request),
        str(workflow_id),
        limit=limit,
        offset=offset,
    )
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "events": [
            {
                "event_id": str(event.id),
                "fired_at": _iso(event.fired_at),
                "evidence": dict(event.evidence or {}),
            }
            for event in events
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/screeners/{workflow_id}/attachments")
async def list_screener_attachments(
    request: Request,
    workflow_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    revision: Optional[int] = Query(None, ge=1),
):
    """Attachment BASELINES — previously computed and stored but never routed.

    This is what makes hysteresis inspectable instead of mysterious: for every
    attachment and instrument it shows ``present`` (is it in now), ``last_rank``,
    ``consecutive_absent`` (how close an exit trigger is to firing) and
    ``last_complete_run_id`` (which run the baseline came from).

    ``last_complete_run_id`` is the honesty field: the baseline advances only on
    a COMPLETE run, so a partial run cannot masquerade as the current
    membership. An operator asking "why has this not exited" gets the answer
    here — the baseline is from run X, and consecutive_absent is N.
    """
    from backend.workflows.repository import WorkflowRevision
    from backend.workflows.screener_repository import ScreenerRunRepository

    workflow, latest = _require_screener(session_factory, workflow_id, scope)
    if revision is not None:
        with session_factory() as session:
            target = session.execute(
                select(WorkflowRevision).where(
                    WorkflowRevision.workflow_id == workflow_id,
                    WorkflowRevision.revision == revision,
                )
            ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail=f"Revision {revision} not found")
    else:
        target = latest

    document = target.document if isinstance(target.document, dict) else {}
    # Attachments live inside the `screener` block; a document-root lookup would
    # silently return nothing and the page would look like "no attachments".
    screener_block = document.get("screener")
    attachments = (
        (screener_block or {}).get("attachments")
        if isinstance(screener_block, dict)
        else None
    ) or []
    repo = ScreenerRunRepository(session_factory)

    payload = []
    for attachment in attachments:
        attachment_id = str(attachment.get("id") or "")
        if not attachment_id:
            continue
        states = repo.attachment_states(
            scope, str(workflow_id), str(target.id), attachment_id
        )
        payload.append(
            {
                "attachment_id": attachment_id,
                "trigger": attachment.get("trigger"),
                "channels": list(attachment.get("channels") or []),
                # Hysteresis fields are flat on the attachment in the document
                # schema, so they are surfaced flat rather than re-nested into a
                # shape the document does not actually use.
                "hysteresis": {
                    "entry_rank": attachment.get("entry_rank"),
                    "exit_rank": attachment.get("exit_rank"),
                    "exit_after": attachment.get("exit_after"),
                    "top_n": attachment.get("top_n"),
                    "rank_delta": attachment.get("rank_delta"),
                    "initial_match": bool(attachment.get("initial_match", False)),
                },
                "membership_count": len(states),
                "members": [
                    {
                        "instrument_key": state.instrument_key,
                        "present": bool(state.present),
                        "last_rank": state.last_rank,
                        "consecutive_absent": int(state.consecutive_absent or 0),
                        "last_complete_run_id": (
                            str(state.last_complete_run_id)
                            if state.last_complete_run_id
                            else None
                        ),
                        "updated_at": _iso(state.updated_at),
                    }
                    for state in sorted(
                        states.values(), key=lambda item: item.instrument_key
                    )
                ],
            }
        )

    return {
        "ok": True,
        "workflow_id": workflow_id,
        "revision": int(target.revision),
        "revision_id": str(target.id),
        "attachments": payload,
        "note": (
            "the baseline advances only on a COMPLETE run, so a partial run "
            "never replaces membership; an attachment with no rows has never "
            "seen a complete run and will only initialize on the next one"
        ),
    }


@router.post("/screeners/preview")
async def preview_screener(
    request: Request,
    payload: ScreenerPreviewRequest,
    scope: str = Depends(require_operator_scope),
):
    """Pure screener dry-run. Persists nothing and sends nothing.

    Calls the SAME function the worker route calls, with the owner the server
    authorized — so the preview an operator sees is the preview the SDK would
    get, and the two cannot drift.
    """
    enforce_same_origin(request)
    return preview_screener_for(request, scope, payload)


# ---------------------------------------------------------------------------
# external signal producers
# ---------------------------------------------------------------------------
#
# Management only. Submitting a VALUE is deliberately absent: that needs a
# producer credential (a different credential type, minted below), and exposing
# it here would let a browser session write signal data that consuming rules
# then sample. `GET /signals/health?purge=true` is also deliberately absent — it
# MUTATES, and a read-shaped route that deletes rows is a trap.


def _signals_service():
    from backend.workflows import external_signals as signals

    return signals


@router.get("/signals/health")
async def signals_health(
    request: Request,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Per-producer counters plus the sampling/expiry limits, in plain terms.

    Never purges: the worker route accepts ``purge=true``, but a GET that
    deletes rows is not offered on the browser surface, where a prefetch or a
    retry could trigger it.
    """
    _ = request
    from datetime import datetime, timezone

    signals = _signals_service()
    from backend.api.routers.worker_signals import MAX_FUTURE_SKEW_S, MAX_LATENESS_S

    session = session_factory()
    try:
        return {
            "ok": True,
            "limits": {
                "max_payload_bytes": signals.MAX_PAYLOAD_BYTES,
                "max_fields": signals.MAX_FIELDS,
                "max_string_length": signals.MAX_STRING_LENGTH,
                "max_rows_per_producer": signals.MAX_ROWS_PER_PRODUCER,
                "retention_s": signals.DEFAULT_RETENTION_S,
                "max_future_skew_s": MAX_FUTURE_SKEW_S,
                "max_lateness_s": MAX_LATENESS_S,
            },
            "note": (
                "accepted values are SAMPLED by the consuming stage's candle "
                "clock and never trigger evaluation on their own, so a value "
                "can expire between evaluations. There is no fallback: a "
                "missing or expired value makes the condition UNKNOWN rather "
                "than false."
            ),
            "purge_available": False,
            "producers": signals.producer_health(
                session, owner_id=scope, now=datetime.now(timezone.utc)
            ),
        }
    finally:
        session.close()


@router.get("/signals/producers")
async def list_producers(
    request: Request,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    _ = request
    from backend.api.routers.worker_signals import _producer_payload

    signals = _signals_service()
    session = session_factory()
    try:
        producers = signals.list_producers(session, owner_id=scope)
        return {
            "ok": True,
            "producers": [_producer_payload(producer) for producer in producers],
            "note": (
                "a producer credential is NOT a worker token: it can only "
                "submit values for its own producer and cannot read alerts, "
                "runs or orders"
            ),
        }
    finally:
        session.close()


@router.post("/signals/producers")
async def create_producer(
    request: Request,
    payload: ProducerCreateRequest,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    enforce_same_origin(request)
    signals = _signals_service()
    session = session_factory()
    try:
        producer = signals.register_producer(
            session,
            owner_id=scope,
            name=payload.name,
            value_schema=payload.value_schema,
            default_ttl_s=payload.default_ttl_s,
        )
        session.commit()
        return {"ok": True, "producer": _producer_payload(producer)}
    except signals.ExternalSignalError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        session.close()


@router.get("/signals/producers/{name}")
async def get_producer(
    request: Request,
    name: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    _ = request
    from backend.api.routers.worker_signals import _producer_payload

    signals = _signals_service()
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=scope, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        return {"ok": True, "producer": _producer_payload(producer)}
    finally:
        session.close()


@router.get("/signals/producers/{name}/credentials")
async def list_producer_credentials(
    request: Request,
    name: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Credential metadata for a producer, so one can be revoked later by id.

    NON-SECRET ONLY: the secret is stored as a hash and cannot be retrieved, so
    this returns the token id and lifecycle fields and nothing a credential
    could be reconstructed from. Owner-scoped, like every other route here.
    """
    _ = request
    signals = _signals_service()
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=scope, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        credentials = signals.list_credentials(session, producer_id=str(producer.id))
        return {
            "ok": True,
            "producer": name,
            "credentials": [
                {
                    "token_id": credential.token_id,
                    "status": credential.status,
                    "created_at": _iso(credential.created_at),
                    "last_used_at": _iso(credential.last_used_at),
                    "revoked_at": _iso(credential.revoked_at),
                }
                for credential in credentials
            ],
            "note": (
                "metadata only — the secret is shown once, at issue time, and is "
                "not recoverable"
            ),
        }
    finally:
        session.close()


@router.post("/signals/producers/{name}/revoke")
async def revoke_producer(
    request: Request,
    name: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    enforce_same_origin(request)
    from backend.api.routers.worker_signals import _producer_payload

    signals = _signals_service()
    session = session_factory()
    try:
        producer = signals.revoke_producer(session, owner_id=scope, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        session.commit()
        return {
            "ok": True,
            "producer": _producer_payload(producer),
            "note": (
                "revoked: existing values are no longer usable by consuming "
                "rules, and a credential must be reissued before the producer "
                "can submit again"
            ),
        }
    finally:
        session.close()


@router.post("/signals/producers/{name}/credentials")
async def issue_producer_credential(
    request: Request,
    name: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    """Issue a producer credential. The secret is returned EXACTLY ONCE."""
    enforce_same_origin(request)
    signals = _signals_service()
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=scope, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        credential, secret = signals.issue_credential(session, producer=producer)
        session.commit()
        return {
            "ok": True,
            "token_id": credential.token_id,
            # The only response that ever carries the secret. It is stored as a
            # hash, so it cannot be retrieved again — issue a new credential and
            # revoke this one if it is lost.
            "secret": secret,
            "reveal_once": True,
            "note": (
                "store this now; it cannot be retrieved. This is a PRODUCER "
                "credential, not a worker token — it can only submit values for "
                "this producer"
            ),
        }
    finally:
        session.close()


@router.post("/signals/producers/{name}/credentials/{token_id}/revoke")
async def revoke_producer_credential(
    request: Request,
    name: str,
    token_id: str,
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
):
    enforce_same_origin(request)
    signals = _signals_service()
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=scope, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        revoked = signals.revoke_credential(
            session, producer_id=str(producer.id), token_id=token_id
        )
        if not revoked:
            raise HTTPException(status_code=404, detail="credential not found")
        session.commit()
        return {"ok": True, "token_id": token_id, "revoked": True}
    finally:
        session.close()


@router.get("/signals/values")
async def list_signal_values(
    request: Request,
    producer: str = Query(..., min_length=1),
    scope: str = Depends(require_operator_scope),
    session_factory: Any = Depends(_alerts_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Retained values for one producer, newest first.

    ``status`` distinguishes accepted from late and ``expires_at`` is returned,
    so the UI can show a value that is retained but no longer usable — the
    difference between "we received it" and "a rule could still use it".
    """
    _ = request
    from sqlalchemy import func
    from sqlalchemy import select as _select

    signals = _signals_service()
    session = session_factory()
    try:
        record = signals.get_producer(session, owner_id=scope, name=producer)
        if record is None:
            raise HTTPException(status_code=404, detail="producer not found")
        rows = session.execute(
            _select(signals.ExternalSignalValue)
            .where(signals.ExternalSignalValue.producer_id == record.id)
            .order_by(signals.ExternalSignalValue.event_time.desc())
            .offset(offset)
            .limit(limit)
        ).scalars().all()
        total = session.execute(
            _select(func.count())
            .select_from(signals.ExternalSignalValue)
            .where(signals.ExternalSignalValue.producer_id == record.id)
        ).scalar()
        return {
            "ok": True,
            "producer": record.name,
            "limit": limit,
            "offset": offset,
            "total": int(total or 0),
            "values": [
                {
                    "value_id": row.id,
                    "instrument_key": row.instrument_key,
                    "event_time": _iso(row.event_time),
                    "received_at": _iso(row.received_at),
                    "expires_at": _iso(row.expires_at),
                    "status": row.status,
                    "value": row.value,
                    "idempotency_key": row.idempotency_key,
                }
                for row in rows
            ],
        }
    finally:
        session.close()
