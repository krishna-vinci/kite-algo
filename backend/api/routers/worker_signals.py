"""Worker API for registered external signal producers (Phase 4 F10).

Two distinct authorizations, deliberately not interchangeable:

- **Managing producers** (register, list, revoke, issue credentials) requires a
  worker token holding ``signals:admin`` / ``signals:read``. Those actions are
  in the token allow-list but are NOT granted by default, so no existing token
  silently gains producer administration.
- **Submitting values** requires a **producer credential** issued for exactly
  one producer. A worker token cannot submit as a producer, and a producer
  credential cannot administer anything — least privilege in both directions.

The submission response is only returned after the value is durably committed,
so a 2xx means the value is stored rather than merely acknowledged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from backend.api.routers.worker_shared import require_worker_token
from backend.api.routers.worker_workflows import _alerts_db, _owner_id_for_token
from backend.workflows import external_signals as signals

router = APIRouter(prefix="/worker/signals", tags=["worker-signals"])

# Ingestion guards. Future-skew is a hard rejection (an observation cannot come
# from the future); lateness stores the value as permanently unusable rather
# than discarding it, so the audit trail explains why a signal went quiet.
MAX_FUTURE_SKEW_S = 300
MAX_LATENESS_S = 3600


async def _authorize(request: Request, action: str) -> tuple[Any, str]:
    from backend.api.routers.worker_shared import _require_action

    token = await require_worker_token(request)
    _require_action(token, action)
    return token, _owner_id_for_token(token)


def _resolve_producer(request: Request, session_factory: Any) -> dict:
    """Resolve a producer credential (never a worker token)."""
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Producer bearer credential required")
    raw = header.split(" ", 1)[1].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="Producer bearer credential required")
    session = session_factory()
    try:
        producer = signals.resolve_producer_credential(session, raw_token=raw)
        if producer is None:
            raise HTTPException(
                status_code=401, detail="Invalid or revoked producer credential"
            )
        session.commit()
        return {"id": producer.id, "name": producer.name}
    except HTTPException:
        session.rollback()
        raise
    finally:
        session.close()


class ProducerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=signals.MAX_PRODUCER_NAME)
    value_schema: Optional[dict] = None
    default_ttl_s: int = Field(default=3600, gt=0)


class ValueSubmitRequest(BaseModel):
    value: dict
    event_time: datetime
    instrument_key: Optional[str] = None
    expires_at: Optional[datetime] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=160)


def _producer_payload(producer) -> dict:
    """Producer view. NEVER includes a secret or a hash."""
    return {
        "name": producer.name,
        "enabled": bool(producer.enabled),
        "revoked": producer.revoked_at is not None,
        "value_schema": producer.value_schema or {"fields": {}},
        "default_ttl_s": int(producer.default_ttl_s),
        "created_at": producer.created_at.isoformat() if producer.created_at else None,
        "revoked_at": producer.revoked_at.isoformat() if producer.revoked_at else None,
    }


@router.get("/producers")
async def list_producers(request: Request, session_factory: Any = Depends(_alerts_db)):
    _, owner_id = await _authorize(request, "signals:read")
    session = session_factory()
    try:
        producers = signals.list_producers(session, owner_id=owner_id)
        return {
            "ok": True,
            "producers": [_producer_payload(producer) for producer in producers],
        }
    finally:
        session.close()


@router.post("/producers", status_code=201)
async def create_producer(
    request: Request,
    payload: ProducerCreateRequest,
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "signals:admin")
    session = session_factory()
    try:
        producer = signals.register_producer(
            session,
            owner_id=owner_id,
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


@router.get("/producers/{name}")
async def get_producer(
    request: Request, name: str, session_factory: Any = Depends(_alerts_db)
):
    _, owner_id = await _authorize(request, "signals:read")
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=owner_id, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        return {"ok": True, "producer": _producer_payload(producer)}
    finally:
        session.close()


@router.post("/producers/{name}/revoke")
async def revoke_producer(
    request: Request, name: str, session_factory: Any = Depends(_alerts_db)
):
    _, owner_id = await _authorize(request, "signals:admin")
    session = session_factory()
    try:
        producer = signals.revoke_producer(session, owner_id=owner_id, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        session.commit()
        return {"ok": True, "producer": _producer_payload(producer)}
    finally:
        session.close()


@router.post("/producers/{name}/credentials", status_code=201)
async def issue_credential(
    request: Request, name: str, session_factory: Any = Depends(_alerts_db)
):
    """Issue a credential. The secret is returned EXACTLY ONCE, here."""
    _, owner_id = await _authorize(request, "signals:admin")
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=owner_id, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        credential, secret = signals.issue_credential(session, producer=producer)
        session.commit()
        return {
            "ok": True,
            "token_id": credential.token_id,
            # The ONLY response that ever carries the secret. It is stored as a
            # hash, so it cannot be retrieved again — issue a new credential
            # and revoke this one if it is lost.
            "secret": secret,
            "note": "store this now; it cannot be retrieved",
        }
    finally:
        session.close()


@router.post("/producers/{name}/credentials/{token_id}/revoke")
async def revoke_credential(
    request: Request,
    name: str,
    token_id: str,
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "signals:admin")
    session = session_factory()
    try:
        producer = signals.get_producer(session, owner_id=owner_id, name=name)
        if producer is None:
            raise HTTPException(status_code=404, detail="producer not found")
        revoked = signals.revoke_credential(
            session, producer_id=producer.id, token_id=token_id
        )
        if not revoked:
            raise HTTPException(status_code=404, detail="credential not found")
        session.commit()
        return {"ok": True, "token_id": token_id, "status": "revoked"}
    finally:
        session.close()


@router.post("/values", status_code=201)
async def submit_value(
    request: Request,
    payload: ValueSubmitRequest,
    session_factory: Any = Depends(_alerts_db),
):
    """Submit a typed value as an authorized producer.

    The value is committed before this returns, so 2xx means stored.
    """
    producer_ref = _resolve_producer(request, session_factory)
    session = session_factory()
    try:
        producer = signals.get_producer_by_id(session, producer_ref["id"])
        if producer is None or not producer.enabled or producer.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Producer is disabled")
        row, deduplicated = signals.ingest_value(
            session,
            producer=producer,
            payload=payload.value,
            event_time=payload.event_time,
            instrument_key=payload.instrument_key,
            expires_at=payload.expires_at,
            idempotency_key=payload.idempotency_key,
            max_future_skew_s=MAX_FUTURE_SKEW_S,
            max_lateness_s=MAX_LATENESS_S,
        )
        session.commit()
        return {
            "ok": True,
            "value_id": row.id,
            "status": row.status,
            "deduplicated": deduplicated,
            "event_time": row.event_time.isoformat(),
            "expires_at": row.expires_at.isoformat(),
        }
    except signals.ExternalSignalIdempotencyConflict as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={"rejection_reason": "IDEMPOTENCY_CONFLICT", "message": str(exc)},
        ) from exc
    except signals.ExternalSignalCapacityExceeded as exc:
        session.rollback()
        raise HTTPException(
            status_code=429,
            detail={
                "rejection_reason": "PRODUCER_CAPACITY_EXCEEDED",
                "message": str(exc),
            },
        ) from exc
    except signals.ExternalSignalError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        session.close()


@router.get("/values")
async def list_values(
    request: Request,
    producer: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session_factory: Any = Depends(_alerts_db),
):
    _, owner_id = await _authorize(request, "signals:read")
    session = session_factory()
    try:
        record = signals.get_producer(session, owner_id=owner_id, name=producer)
        if record is None:
            raise HTTPException(status_code=404, detail="producer not found")
        rows = session.execute(
            select(signals.ExternalSignalValue)
            .where(signals.ExternalSignalValue.producer_id == record.id)
            .order_by(signals.ExternalSignalValue.event_time.desc())
            .offset(offset)
            .limit(limit)
        ).scalars().all()
        total = session.execute(
            select(func.count()).select_from(signals.ExternalSignalValue).where(
                signals.ExternalSignalValue.producer_id == record.id
            )
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
                    "event_time": row.event_time.isoformat(),
                    "received_at": row.received_at.isoformat(),
                    "expires_at": row.expires_at.isoformat(),
                    "status": row.status,
                    "value": row.value,
                    "idempotency_key": row.idempotency_key,
                }
                for row in rows
            ],
        }
    finally:
        session.close()


@router.get("/health")
async def signals_health(
    request: Request,
    purge: bool = Query(False, description="Also purge retained expired values"),
    session_factory: Any = Depends(_alerts_db),
):
    """Per-producer counters, including expired/late/unusable state."""
    _, owner_id = await _authorize(request, "signals:read")
    session = session_factory()
    try:
        if purge:
            signals.purge_expired_values(session, now=datetime.now(timezone.utc))
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
                "clock and never trigger evaluation; a value can therefore "
                "expire between evaluations"
            ),
            "producers": signals.producer_health(
                session, owner_id=owner_id, now=datetime.now(timezone.utc)
            ),
        }
    finally:
        session.close()
