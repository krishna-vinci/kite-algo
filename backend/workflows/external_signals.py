"""Registered external signal producers (Phase 4 F10).

Typed, expiring, idempotent values submitted by authorized producers and
consumed by workflow conditions as ``external.<producer>.<field>``.

Design commitments that shape this module:

- **No code execution.** A producer's payload is typed scalars only. There are
  no expressions, imports, callables or executable payloads anywhere in the
  ingestion path.
- **Durable acceptance precedes the response.** A value is committed before the
  endpoint returns 2xx, so nothing can silently disappear between "accepted"
  and storage.
- **Sampled, never pushed.** Accepted values do not trigger evaluation; the
  consuming stage reads them at its own ``candle_close`` clock. A value can
  therefore be accepted and expire between two evaluations — disclosed, not
  implied away.
- **Lookup is deterministic and replay-safe.** The evaluation cutoff is the
  observation's event time (never wall clock), and expiry is evaluated at that
  same cutoff, so re-evaluating a bar yields the same answer regardless of when
  the replay runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.workflows.repository import Base
from backend.workflows.universes import GUID

__all__ = [
    "ExternalSignalProducer",
    "ExternalSignalProducerCredential",
    "ExternalSignalValue",
    "ExternalSignalError",
    "ExternalSignalIdempotencyConflict",
    "ExternalSignalCapacityExceeded",
    "ExternalSignalLimits",
    "DEFAULT_LIMITS",
    "normalize_value_schema",
    "validate_payload",
    "canonical_content_hash",
    "register_producer",
    "get_producer",
    "list_producers",
    "revoke_producer",
    "issue_credential",
    "list_credentials",
    "revoke_credential",
    "resolve_producer_credential",
    "ingest_value",
    "lookup_value",
    "producer_health",
    "purge_expired_values",
]

# Payload bounds — enforced before anything is persisted.
MAX_PAYLOAD_BYTES = 8 * 1024
MAX_FIELDS = 32
MAX_STRING_LENGTH = 512
MAX_PRODUCER_NAME = 128
# Retention: values are unreadable once expired, so they are deletable after a
# grace period; idempotency keys age out with their row (documented: a replay
# older than retention is treated as new work).
DEFAULT_RETENTION_S = 7 * 24 * 3600
IDEMPOTENCY_RETENTION_S = 30 * 24 * 3600
# Capacity: rejected, never resolved by evicting a still-valid value.
MAX_ROWS_PER_PRODUCER = 100_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class ExternalSignalError(Exception):
    """Base class for typed external-signal failures."""


class ExternalSignalIdempotencyConflict(ExternalSignalError):
    """Same idempotency key, different content — never silently overwritten."""


class ExternalSignalCapacityExceeded(ExternalSignalError):
    """The producer is at its row cap — reject rather than evict live values."""


@dataclass(frozen=True)
class ExternalSignalLimits:
    max_payload_bytes: int = MAX_PAYLOAD_BYTES
    max_fields: int = MAX_FIELDS
    max_string_length: int = MAX_STRING_LENGTH
    max_rows_per_producer: int = MAX_ROWS_PER_PRODUCER
    retention_s: int = DEFAULT_RETENTION_S
    idempotency_retention_s: int = IDEMPOTENCY_RETENTION_S


DEFAULT_LIMITS = ExternalSignalLimits()


class ExternalSignalProducer(Base):
    """Owner-scoped producer identity and its declared value schema."""

    __tablename__ = "external_signal_producers"

    id = Column(GUID, primary_key=True, default=_uuid)
    owner_id = Column(String(255), nullable=False)
    name = Column(String(MAX_PRODUCER_NAME), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    value_schema = Column(JSON, nullable=False, default=dict)
    default_ttl_s = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class ExternalSignalProducerCredential(Base):
    """A producer credential: the HASH is stored, the secret is shown once."""

    __tablename__ = "external_signal_producer_credentials"

    id = Column(GUID, primary_key=True, default=_uuid)
    producer_id = Column(GUID, nullable=False)
    token_id = Column(String(64), nullable=False)
    token_hash = Column(String(128), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class ExternalSignalValue(Base):
    """One accepted (or late-rejected) external observation."""

    __tablename__ = "external_signal_values"

    id = Column(GUID, primary_key=True, default=_uuid)
    producer_id = Column(GUID, nullable=False)
    owner_id = Column(String(255), nullable=False)
    instrument_key = Column(String(128), nullable=True)
    event_time = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # 'accepted' is usable until expiry; 'late' is stored but permanently
    # unusable, and is counted so the operator can see why a signal vanished.
    status = Column(String(16), nullable=False)
    value = Column(JSON, nullable=False, default=dict)
    content_hash = Column(String(64), nullable=False)
    idempotency_key = Column(String(160), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# schema and payload validation
# ---------------------------------------------------------------------------


def normalize_value_schema(raw: Any) -> Dict[str, Any]:
    """Validate and normalize a producer's declared value schema.

    Shape: ``{"fields": {"<name>": "number"|"string"|"boolean"}}``. Only these
    three scalar types exist — there is deliberately no expression, script or
    nested-object type, so a payload can never carry executable content.
    """
    if raw is None:
        return {"fields": {}}
    if not isinstance(raw, dict):
        raise ExternalSignalError("value_schema must be a mapping")
    fields = raw.get("fields", raw)
    if not isinstance(fields, dict):
        raise ExternalSignalError("value_schema.fields must be a mapping")
    if len(fields) > MAX_FIELDS:
        raise ExternalSignalError(
            f"value_schema declares {len(fields)} fields; the maximum is {MAX_FIELDS}"
        )
    normalized: Dict[str, str] = {}
    for name, kind in fields.items():
        if not isinstance(name, str) or not name or len(name) > 64:
            raise ExternalSignalError(f"invalid field name {name!r}")
        if kind not in ("number", "string", "boolean"):
            raise ExternalSignalError(
                f"field '{name}' has unsupported type {kind!r} "
                "(supported: number, string, boolean)"
            )
        normalized[name] = kind
    return {"fields": normalized}


def validate_payload(payload: Any, schema: Dict[str, Any], limits=DEFAULT_LIMITS) -> Dict[str, Any]:
    """Validate a submitted payload against the producer's schema.

    Enforces the size, field-count and value bounds and rejects anything that
    is not a finite scalar. Returns the payload unchanged on success.
    """
    if not isinstance(payload, dict):
        raise ExternalSignalError("value must be a mapping of field -> scalar")
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > limits.max_payload_bytes:
        raise ExternalSignalError(
            f"value exceeds the {limits.max_payload_bytes} byte payload limit"
        )
    if len(payload) > limits.max_fields:
        raise ExternalSignalError(
            f"value declares {len(payload)} fields; the maximum is {limits.max_fields}"
        )
    declared = (schema or {}).get("fields") or {}
    for name, value in payload.items():
        if not isinstance(name, str) or not name or len(name) > 64:
            raise ExternalSignalError(f"invalid field name {name!r}")
        if isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise ExternalSignalError(f"field '{name}' must be a finite number")
            kind = "number"
        elif isinstance(value, str):
            if len(value) > limits.max_string_length:
                raise ExternalSignalError(
                    f"field '{name}' exceeds the {limits.max_string_length} character limit"
                )
            kind = "string"
        else:
            raise ExternalSignalError(
                f"field '{name}' must be a number, string or boolean "
                f"(got {type(value).__name__})"
            )
        expected = declared.get(name)
        if expected is not None and expected != kind:
            raise ExternalSignalError(
                f"field '{name}' is declared {expected!r} but was sent as {kind}"
            )
    missing = [name for name in declared if name not in payload]
    if missing:
        raise ExternalSignalError(
            "value is missing declared field(s): " + ", ".join(sorted(missing))
        )
    return payload


def canonical_content_hash(payload: Dict[str, Any]) -> str:
    """Stable hash of a payload, used to detect a conflicting duplicate."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# producer administration
# ---------------------------------------------------------------------------


def register_producer(
    session: Session,
    *,
    owner_id: str,
    name: str,
    value_schema: Any,
    default_ttl_s: int,
    now: Optional[datetime] = None,
) -> ExternalSignalProducer:
    """Create (or return) an owner-scoped producer registration."""
    timestamp = now or _utcnow()
    clean = (name or "").strip()
    if not clean or len(clean) > MAX_PRODUCER_NAME:
        raise ExternalSignalError(
            f"producer name must be 1..{MAX_PRODUCER_NAME} characters"
        )
    if not isinstance(default_ttl_s, int) or isinstance(default_ttl_s, bool) or default_ttl_s <= 0:
        raise ExternalSignalError("default_ttl_s must be a positive integer")
    schema = normalize_value_schema(value_schema)
    producer = get_producer(session, owner_id=owner_id, name=clean)
    if producer is not None:
        producer.value_schema = schema
        producer.default_ttl_s = int(default_ttl_s)
        producer.enabled = True
        producer.revoked_at = None
        producer.updated_at = timestamp
        session.flush()
        return producer
    producer = ExternalSignalProducer(
        id=_uuid(),
        owner_id=owner_id,
        name=clean,
        enabled=True,
        value_schema=schema,
        default_ttl_s=int(default_ttl_s),
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(producer)
    session.flush()
    return producer


def get_producer(
    session: Session, *, owner_id: str, name: str
) -> Optional[ExternalSignalProducer]:
    return session.execute(
        select(ExternalSignalProducer).where(
            ExternalSignalProducer.owner_id == owner_id,
            ExternalSignalProducer.name == name,
        )
    ).scalar_one_or_none()


def get_producer_by_id(session: Session, producer_id: str) -> Optional[ExternalSignalProducer]:
    return session.execute(
        select(ExternalSignalProducer).where(ExternalSignalProducer.id == producer_id)
    ).scalar_one_or_none()


def list_producers(session: Session, *, owner_id: str) -> List[ExternalSignalProducer]:
    return list(
        session.execute(
            select(ExternalSignalProducer)
            .where(ExternalSignalProducer.owner_id == owner_id)
            .order_by(ExternalSignalProducer.name.asc())
        ).scalars().all()
    )


def revoke_producer(
    session: Session, *, owner_id: str, name: str, now: Optional[datetime] = None
) -> Optional[ExternalSignalProducer]:
    """Disable a producer and every one of its credentials."""
    timestamp = now or _utcnow()
    producer = get_producer(session, owner_id=owner_id, name=name)
    if producer is None:
        return None
    producer.enabled = False
    producer.revoked_at = timestamp
    producer.updated_at = timestamp
    for credential in session.execute(
        select(ExternalSignalProducerCredential).where(
            ExternalSignalProducerCredential.producer_id == producer.id,
            ExternalSignalProducerCredential.status == "active",
        )
    ).scalars().all():
        credential.status = "revoked"
        credential.revoked_at = timestamp
    session.flush()
    return producer


def issue_credential(
    session: Session, *, producer: ExternalSignalProducer, now: Optional[datetime] = None
) -> Tuple[ExternalSignalProducerCredential, str]:
    """Mint a credential. Returns ``(row, secret)``; the secret is shown ONCE.

    Only the hash is persisted, so it cannot be retrieved later, echoed in
    another response, or written to a log. Losing it means issuing a new
    credential and revoking the old one.
    """
    from backend.shared.serialization import _hash_token

    timestamp = now or _utcnow()
    secret = "kas_" + uuid.uuid4().hex + uuid.uuid4().hex[:16]
    credential = ExternalSignalProducerCredential(
        id=_uuid(),
        producer_id=producer.id,
        token_id="producer_" + uuid.uuid4().hex[:16],
        token_hash=_hash_token(secret),
        status="active",
        created_at=timestamp,
    )
    session.add(credential)
    session.flush()
    return credential, secret


def list_credentials(
    session: Session, *, producer_id: str
) -> List[ExternalSignalProducerCredential]:
    """Non-secret credential metadata for later management.

    Returns only the token id and lifecycle fields needed to revoke a credential.
    The secret is stored as a hash and is never recoverable, so it is not (and
    cannot be) part of this payload.
    """
    return list(
        session.execute(
            select(ExternalSignalProducerCredential)
            .where(ExternalSignalProducerCredential.producer_id == producer_id)
            .order_by(ExternalSignalProducerCredential.created_at.desc())
        )
        .scalars()
        .all()
    )


def revoke_credential(
    session: Session, *, producer_id: str, token_id: str, now: Optional[datetime] = None
) -> bool:
    timestamp = now or _utcnow()
    credential = session.execute(
        select(ExternalSignalProducerCredential).where(
            ExternalSignalProducerCredential.producer_id == producer_id,
            ExternalSignalProducerCredential.token_id == token_id,
        )
    ).scalar_one_or_none()
    if credential is None:
        return False
    credential.status = "revoked"
    credential.revoked_at = timestamp
    session.flush()
    return True


def resolve_producer_credential(
    session: Session, *, raw_token: str, now: Optional[datetime] = None
) -> Optional[ExternalSignalProducer]:
    """Resolve a producer credential to its (enabled) producer.

    Fails closed for revoked credentials, revoked/disabled producers and
    unknown tokens: a producer that has been revoked contributes nothing, in
    any version, regardless of what values are still stored.
    """
    from backend.shared.serialization import _hash_token

    if not raw_token:
        return None
    credential = session.execute(
        select(ExternalSignalProducerCredential).where(
            ExternalSignalProducerCredential.token_hash == _hash_token(raw_token)
        )
    ).scalar_one_or_none()
    if credential is None or credential.status != "active":
        return None
    producer = get_producer_by_id(session, credential.producer_id)
    if producer is None or not producer.enabled or producer.revoked_at is not None:
        return None
    credential.last_used_at = now or _utcnow()
    session.flush()
    return producer


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------


def ingest_value(
    session: Session,
    *,
    producer: ExternalSignalProducer,
    payload: Any,
    event_time: datetime,
    instrument_key: Optional[str],
    expires_at: Optional[datetime],
    idempotency_key: Optional[str],
    max_future_skew_s: int,
    max_lateness_s: int,
    limits=DEFAULT_LIMITS,
    now: Optional[datetime] = None,
) -> Tuple[ExternalSignalValue, bool]:
    """Validate, deduplicate and durably store one external value.

    Returns ``(row, deduplicated)``. The caller commits BEFORE responding, so a
    2xx means the value is stored. Rules:

    - a value stamped further in the future than ``max_future_skew_s`` is
      rejected outright (it cannot be trusted to be an observation of the past);
    - a value later than ``max_lateness_s`` is stored with ``status='late'``:
      recorded for the audit trail, permanently unusable, never a signal;
    - an expiry already in the past is rejected;
    - a repeated idempotency key with IDENTICAL content returns the original row
      (no second row, no re-processing); with DIFFERENT content it raises
      rather than silently overwriting.
    """
    timestamp = now or _utcnow()
    schema = producer.value_schema or {"fields": {}}
    clean = validate_payload(payload, schema, limits)
    content_hash = canonical_content_hash(clean)
    event_time = _as_utc(event_time)

    skew = (event_time - timestamp).total_seconds()
    if skew > max_future_skew_s:
        raise ExternalSignalError(
            f"event_time is {int(skew)}s in the future; the maximum allowed skew "
            f"is {max_future_skew_s}s"
        )
    lateness = (timestamp - event_time).total_seconds()
    lateness = max(0.0, lateness)
    if lateness > max_lateness_s:
        status = "late"
    else:
        status = "accepted"

    resolved_expiry = _as_utc(expires_at)
    if resolved_expiry is None:
        # TTL runs from RECEIPT, not from the (possibly old) event time: a
        # value's usable lifetime starts when we accepted it, so a legitimately
        # late-but-within-bound observation is not born already expired.
        resolved_expiry = timestamp + timedelta(seconds=int(producer.default_ttl_s))
    if resolved_expiry <= timestamp:
        raise ExternalSignalError("expires_at is already in the past")

    if idempotency_key is not None and idempotency_key.strip():
        key = idempotency_key.strip()
        if len(key) > 160:
            raise ExternalSignalError("idempotency_key must be at most 160 characters")
        existing = session.execute(
            select(ExternalSignalValue).where(
                ExternalSignalValue.producer_id == producer.id,
                ExternalSignalValue.idempotency_key == key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.content_hash == content_hash:
                return existing, True
            raise ExternalSignalIdempotencyConflict(
                "idempotency_key was already used with different content "
                f"(original value {existing.id} at {existing.event_time.isoformat()})"
            )
    else:
        key = None

    rows = session.execute(
        select(func.count()).select_from(ExternalSignalValue).where(
            ExternalSignalValue.producer_id == producer.id
        )
    ).scalar()
    if int(rows or 0) >= limits.max_rows_per_producer:
        # Reject rather than evict: a value inside its validity window is not
        # ours to delete, and dropping it would change evaluation results.
        raise ExternalSignalCapacityExceeded(
            f"producer '{producer.name}' holds {rows} values; the limit is "
            f"{limits.max_rows_per_producer}. Expired values are purged "
            "automatically; reduce the submission rate or the TTL."
        )

    value = ExternalSignalValue(
        id=_uuid(),
        producer_id=producer.id,
        owner_id=producer.owner_id,
        instrument_key=instrument_key,
        event_time=event_time,
        received_at=timestamp,
        expires_at=resolved_expiry,
        status=status,
        value=dict(clean),
        content_hash=content_hash,
        idempotency_key=key,
        created_at=timestamp,
    )
    try:
        # A savepoint, so losing the race rolls back only this insert and never
        # the caller's surrounding transaction.
        with session.begin_nested():
            session.add(value)
            session.flush()
    except IntegrityError:
        if key is None:
            raise
        winner = session.execute(
            select(ExternalSignalValue).where(
                ExternalSignalValue.producer_id == producer.id,
                ExternalSignalValue.idempotency_key == key,
            )
        ).scalar_one_or_none()
        if winner is not None and winner.content_hash == content_hash:
            # Identical content raced us: the winner IS this logical value.
            return winner, True
        raise ExternalSignalIdempotencyConflict(
            "idempotency_key was used concurrently with different content"
        )
    return value, False


def purge_expired_values(
    session: Session,
    *,
    producer_id: Optional[str] = None,
    retention_s: int = DEFAULT_RETENTION_S,
    now: Optional[datetime] = None,
) -> int:
    """Delete values that expired longer ago than the retention window.

    Only genuinely unusable rows are removed: an expired value can never be
    read again, so deletion cannot change an evaluation result.
    """
    timestamp = now or _utcnow()
    cutoff = timestamp - timedelta(seconds=int(retention_s))
    stmt = delete(ExternalSignalValue).where(ExternalSignalValue.expires_at < cutoff)
    if producer_id is not None:
        stmt = stmt.where(ExternalSignalValue.producer_id == producer_id)
    result = session.execute(stmt)
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# lookup (the consuming side)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalLookup:
    """The result of resolving one ``external.<producer>.<field>`` reference."""

    value: Optional[float]
    reason: Optional[str]
    value_id: Optional[str] = None
    event_time: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    acquired_at: Optional[datetime] = None
    usable: bool = False


def lookup_value(
    session: Session,
    *,
    owner_id: str,
    producer_name: str,
    field: str,
    cutoff: datetime,
    instrument_key: Optional[str] = None,
) -> ExternalLookup:
    """Resolve one external field as of an evaluation cutoff ``T``.

    Deterministic and replay-safe:

    1. the producer must exist, be enabled and not revoked — otherwise
       ``external_revoked`` regardless of what is stored;
    2. only values with ``event_time <= T`` are candidates, so a bar closing at
       10:05 can never consume a value stamped 10:10 (``external_future`` when
       that is the only candidate);
    3. the newest candidate by ``event_time`` wins, ties broken by
       ``received_at`` then ``id`` descending;
    4. usability is ``T <= expires_at`` — evaluated at ``T``, NOT at ``now()``,
       so replaying an old bar yields the value that was in force then rather
       than an answer that depends on when the replay ran;
    5. there is NO fallback to an older still-valid value: if the newest
       candidate is expired or late the field is unknown with that reason,
       because falling back would attribute a stale observation to a bar whose
       intended input had already lapsed.
    """
    cutoff = _as_utc(cutoff)
    producer = get_producer(session, owner_id=owner_id, name=producer_name)
    if producer is None:
        return ExternalLookup(value=None, reason="external_missing")
    if not producer.enabled or producer.revoked_at is not None:
        return ExternalLookup(value=None, reason="external_revoked")

    stmt = (
        select(ExternalSignalValue)
        .where(
            ExternalSignalValue.producer_id == producer.id,
            ExternalSignalValue.event_time <= cutoff,
        )
        .order_by(
            ExternalSignalValue.event_time.desc(),
            ExternalSignalValue.received_at.desc(),
            ExternalSignalValue.id.desc(),
        )
        .limit(1)
    )
    if instrument_key is not None:
        # Instrument-addressed values win; a producer-scoped value (NULL) is
        # an acceptable general fallback for the same field.
        stmt = stmt.where(
            or_(
                ExternalSignalValue.instrument_key == instrument_key,
                ExternalSignalValue.instrument_key.is_(None),
            )
        )
    candidate = session.execute(stmt).scalar_one_or_none()
    if candidate is None:
        any_future = session.execute(
            select(ExternalSignalValue.id)
            .where(
                ExternalSignalValue.producer_id == producer.id,
                ExternalSignalValue.event_time > cutoff,
            )
            .limit(1)
        ).scalar_one_or_none()
        reason = "external_future" if any_future is not None else "external_missing"
        return ExternalLookup(value=None, reason=reason)

    if candidate.status != "accepted":
        return ExternalLookup(
            value=None,
            reason="external_late",
            value_id=candidate.id,
            event_time=_as_utc(candidate.event_time),
            expires_at=_as_utc(candidate.expires_at),
            acquired_at=_as_utc(candidate.received_at),
        )
    if _as_utc(candidate.expires_at) < cutoff:
        return ExternalLookup(
            value=None,
            reason="external_expired",
            value_id=candidate.id,
            event_time=_as_utc(candidate.event_time),
            expires_at=_as_utc(candidate.expires_at),
            acquired_at=_as_utc(candidate.received_at),
        )
    raw = (candidate.value or {}).get(field)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        if isinstance(raw, str):
            return ExternalLookup(value=None, reason="external_non_numeric")
        return ExternalLookup(value=None, reason="external_missing_field")
    return ExternalLookup(
        value=float(raw),
        reason=None,
        value_id=candidate.id,
        event_time=_as_utc(candidate.event_time),
        expires_at=_as_utc(candidate.expires_at),
        acquired_at=_as_utc(candidate.received_at),
        usable=True,
    )


def producer_health(
    session: Session, *, owner_id: str, now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """Per-producer counters, so "why did this not fire" is answerable.

    Reports both clocks deliberately: ``expired_at_evaluation`` counts values
    past the current wall clock (what an operator can see now) while the
    consuming lookup uses the observation's event time.
    """
    timestamp = now or _utcnow()
    out: List[Dict[str, Any]] = []
    for producer in list_producers(session, owner_id=owner_id):
        rows = session.execute(
            select(ExternalSignalValue.status, func.count())
            .where(ExternalSignalValue.producer_id == producer.id)
            .group_by(ExternalSignalValue.status)
        ).all()
        by_status = {str(status): int(count) for status, count in rows}
        expired_now = session.execute(
            select(func.count()).select_from(ExternalSignalValue).where(
                ExternalSignalValue.producer_id == producer.id,
                ExternalSignalValue.expires_at < timestamp,
            )
        ).scalar()
        out.append({
            "name": producer.name,
            "enabled": bool(producer.enabled),
            "revoked": producer.revoked_at is not None,
            "default_ttl_s": int(producer.default_ttl_s),
            "schema_fields": sorted((producer.value_schema or {}).get("fields", {})),
            "accepted": by_status.get("accepted", 0),
            "late": by_status.get("late", 0),
            "expired_now": int(expired_now or 0),
            "last_received_at": _as_utc(
                session.execute(
                    select(func.max(ExternalSignalValue.received_at)).where(
                        ExternalSignalValue.producer_id == producer.id
                    )
                ).scalar()
            ),
        })
    return out
