"""Registered external producers: ingestion, lookup, expiry and authorization.

Phase 4 F10 requires typed, expiring, idempotent values with explicit
least-privilege authorization. These tests pin the semantics that matter:
durable acceptance, deterministic replay-safe lookup, and the rule that no
absent/expired/late/revoked input can ever manufacture a signal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows import external_context, external_signals as signals
from backend.workflows.repository import Base

T0 = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)

SCHEMA = {"fields": {"score": "number", "label": "string"}}


@pytest.fixture()
def session_factory():
    """Fresh in-memory SQLite with every alerts-platform table created."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def producer(session):
    return signals.register_producer(
        session,
        owner_id="owner-1",
        name="quant",
        value_schema=SCHEMA,
        default_ttl_s=3600,
        now=T0,
    )


def _ingest(session, producer, payload, *, event_time=None, **kwargs):
    return signals.ingest_value(
        session,
        producer=producer,
        payload=payload,
        event_time=event_time or T0,
        instrument_key=kwargs.pop("instrument_key", None),
        expires_at=kwargs.pop("expires_at", None),
        idempotency_key=kwargs.pop("idempotency_key", None),
        max_future_skew_s=kwargs.pop("max_future_skew_s", 300),
        max_lateness_s=kwargs.pop("max_lateness_s", 3600),
        limits=kwargs.pop("limits", signals.DEFAULT_LIMITS),
        now=kwargs.pop("now", T0),
    )


# ---------------------------------------------------------------------------
# schema and payload validation
# ---------------------------------------------------------------------------


def test_value_schema_rejects_non_scalar_types():
    with pytest.raises(signals.ExternalSignalError):
        signals.normalize_value_schema({"fields": {"nested": "object"}})


def test_payload_must_match_the_declared_schema():
    schema = signals.normalize_value_schema(SCHEMA)
    with pytest.raises(signals.ExternalSignalError, match="missing declared field"):
        signals.validate_payload({"score": 1.0}, schema)
    with pytest.raises(signals.ExternalSignalError, match="declared 'number'"):
        signals.validate_payload({"score": "high", "label": "x"}, schema)


def test_payload_bounds_are_enforced():
    schema = signals.normalize_value_schema(SCHEMA)
    with pytest.raises(signals.ExternalSignalError, match="finite number"):
        signals.validate_payload({"score": float("nan"), "label": "x"}, schema)
    with pytest.raises(signals.ExternalSignalError, match="character limit"):
        signals.validate_payload(
            {"score": 1.0, "label": "x" * (signals.MAX_STRING_LENGTH + 1)}, schema
        )
    with pytest.raises(signals.ExternalSignalError, match="payload limit"):
        signals.validate_payload(
            {"score": 1.0, "label": "y" * signals.MAX_STRING_LENGTH},
            {"fields": {"score": "number", "label": "string"}},
            signals.ExternalSignalLimits(max_payload_bytes=32),
        )


def test_payload_rejects_executable_shapes():
    """No expressions, imports or callables — typed scalars only."""
    schema = signals.normalize_value_schema(SCHEMA)
    with pytest.raises(signals.ExternalSignalError):
        signals.validate_payload({"score": {"__import__": "os"}, "label": "x"}, schema)
    with pytest.raises(signals.ExternalSignalError):
        signals.validate_payload({"score": [1, 2], "label": "x"}, schema)
    with pytest.raises(signals.ExternalSignalError):
        signals.validate_payload({"score": "1+1", "label": "x"}, schema)


# ---------------------------------------------------------------------------
# ingestion: durability, lateness, idempotency, capacity
# ---------------------------------------------------------------------------


def test_accepted_value_is_durably_stored(session, producer):
    row, deduplicated = _ingest(session, producer, {"score": 42.5, "label": "ok"})
    session.commit()
    assert deduplicated is False
    assert row.status == "accepted"
    session.expire_all()
    stored = session.get(signals.ExternalSignalValue, row.id)
    assert stored is not None
    assert stored.value == {"score": 42.5, "label": "ok"}
    assert stored.expires_at > stored.received_at


def test_future_skew_beyond_the_bound_is_rejected(session, producer):
    with pytest.raises(signals.ExternalSignalError, match="in the future"):
        _ingest(
            session, producer, {"score": 1.0, "label": "x"},
            event_time=T0 + timedelta(seconds=301),
        )


def test_small_future_skew_is_tolerated(session, producer):
    row, _ = _ingest(
        session, producer, {"score": 1.0, "label": "x"},
        event_time=T0 + timedelta(seconds=100),
    )
    assert row.status == "accepted"


def test_late_value_is_stored_but_unusable(session, producer):
    row, _ = _ingest(
        session, producer, {"score": 1.0, "label": "x"},
        event_time=T0 - timedelta(seconds=7200),
    )
    session.commit()
    assert row.status == "late"
    # Stored for the audit trail, but never usable as a signal.
    lookup = signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="score",
        cutoff=T0,
    )
    assert lookup.value is None
    assert lookup.reason == "external_late"


def test_expiry_in_the_past_is_rejected(session, producer):
    with pytest.raises(signals.ExternalSignalError, match="already in the past"):
        _ingest(
            session, producer, {"score": 1.0, "label": "x"},
            expires_at=T0 - timedelta(seconds=1),
        )


def test_duplicate_idempotency_key_with_same_content_deduplicates(session, producer):
    first, deduplicated = _ingest(
        session, producer, {"score": 1.0, "label": "x"}, idempotency_key="k-1"
    )
    assert deduplicated is False
    again, deduplicated = _ingest(
        session, producer, {"score": 1.0, "label": "x"}, idempotency_key="k-1"
    )
    assert deduplicated is True
    assert again.id == first.id
    total = session.query(signals.ExternalSignalValue).count()
    assert total == 1


def test_duplicate_idempotency_key_with_different_content_conflicts(session, producer):
    _ingest(session, producer, {"score": 1.0, "label": "x"}, idempotency_key="k-2")
    with pytest.raises(signals.ExternalSignalIdempotencyConflict):
        _ingest(session, producer, {"score": 2.0, "label": "x"}, idempotency_key="k-2")
    # Nothing was overwritten or inserted.
    assert session.query(signals.ExternalSignalValue).count() == 1


def test_capacity_is_rejected_rather_than_evicting_live_values(session, producer):
    limits = signals.ExternalSignalLimits(max_rows_per_producer=2)
    _ingest(session, producer, {"score": 1.0, "label": "x"}, limits=limits)
    _ingest(session, producer, {"score": 2.0, "label": "x"}, limits=limits)
    with pytest.raises(signals.ExternalSignalCapacityExceeded):
        _ingest(session, producer, {"score": 3.0, "label": "x"}, limits=limits)
    assert session.query(signals.ExternalSignalValue).count() == 2


def test_purge_only_removes_long_expired_values(session, producer):
    _ingest(session, producer, {"score": 1.0, "label": "x"},
            expires_at=T0 + timedelta(seconds=300))
    _ingest(session, producer, {"score": 2.0, "label": "x"},
            expires_at=T0 + timedelta(seconds=30))
    # Inside the retention grace period for the shorter-lived value only.
    removed = signals.purge_expired_values(
        session, retention_s=60, now=T0 + timedelta(seconds=120)
    )
    assert removed == 1
    assert session.query(signals.ExternalSignalValue).count() == 1
    # The remaining value is only removable once it is also past retention.
    removed = signals.purge_expired_values(
        session, retention_s=60, now=T0 + timedelta(seconds=400)
    )
    assert removed == 1
    assert session.query(signals.ExternalSignalValue).count() == 0


# ---------------------------------------------------------------------------
# lookup semantics
# ---------------------------------------------------------------------------


def test_lookup_selects_the_newest_value_at_or_before_the_cutoff(session, producer):
    _ingest(session, producer, {"score": 1.0, "label": "old"},
            event_time=T0 - timedelta(minutes=30))
    _ingest(session, producer, {"score": 2.0, "label": "new"},
            event_time=T0 - timedelta(minutes=5))
    lookup = signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="score", cutoff=T0
    )
    assert lookup.value == 2.0


def test_lookup_excludes_future_values(session, producer):
    """A bar closing at 10:05 can never consume a value stamped 10:10."""
    _ingest(session, producer, {"score": 9.0, "label": "future"},
            event_time=T0 + timedelta(minutes=5))
    lookup = signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="score", cutoff=T0
    )
    assert lookup.value is None
    assert lookup.reason == "external_future"


def test_lookup_expiry_is_evaluated_at_the_cutoff_not_now(session, producer):
    """Replay determinism: the same bar yields the same answer whenever replayed."""
    _ingest(session, producer, {"score": 5.0, "label": "x"},
            event_time=T0, expires_at=T0 + timedelta(minutes=10))
    # Evaluated at a cutoff inside the validity window a long time later.
    lookup = signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="score",
        cutoff=T0 + timedelta(minutes=5),
    )
    assert lookup.value == 5.0
    # Past expiry at the cutoff: unknown.
    later = signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="score",
        cutoff=T0 + timedelta(minutes=30),
    )
    assert later.value is None
    assert later.reason == "external_expired"


def test_lookup_does_not_fall_back_to_an_older_valid_value(session, producer):
    """The newest candidate being expired must not silently use an older one."""
    _ingest(session, producer, {"score": 1.0, "label": "old"},
            event_time=T0 - timedelta(hours=2),
            expires_at=T0 + timedelta(hours=1))
    _ingest(session, producer, {"score": 2.0, "label": "new"},
            event_time=T0 - timedelta(minutes=1),
            expires_at=T0 + timedelta(minutes=1))
    lookup = signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="score",
        cutoff=T0 + timedelta(minutes=5),
    )
    assert lookup.value is None
    assert lookup.reason == "external_expired"


def test_revoked_producer_is_unusable_regardless_of_stored_values(session, producer):
    _ingest(session, producer, {"score": 7.0, "label": "x"})
    signals.revoke_producer(session, owner_id="owner-1", name="quant", now=T0)
    lookup = signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="score", cutoff=T0
    )
    assert lookup.value is None
    assert lookup.reason == "external_revoked"


def test_unknown_producer_and_missing_field_report_named_reasons(session, producer):
    _ingest(session, producer, {"score": 1.0, "label": "x"})
    assert signals.lookup_value(
        session, owner_id="owner-1", producer_name="nope", field="score", cutoff=T0
    ).reason == "external_missing"
    assert signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="absent", cutoff=T0
    ).reason == "external_missing_field"
    assert signals.lookup_value(
        session, owner_id="owner-1", producer_name="quant", field="label", cutoff=T0
    ).reason == "external_non_numeric"


def test_cross_owner_lookup_is_isolated(session, producer):
    _ingest(session, producer, {"score": 1.0, "label": "x"})
    assert signals.lookup_value(
        session, owner_id="owner-2", producer_name="quant", field="score", cutoff=T0
    ).reason == "external_missing"


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_credential_secret_is_returned_once_and_stored_hashed(session, producer):
    credential, secret = signals.issue_credential(session, producer=producer)
    session.commit()
    assert secret.startswith("kas_")
    # Only the hash is persisted — the secret itself must not be recoverable.
    assert credential.token_hash != secret
    assert secret not in (credential.token_hash, credential.token_id)


def test_credential_resolves_to_its_producer_only(session, producer):
    other = signals.register_producer(
        session, owner_id="owner-1", name="other", value_schema=SCHEMA,
        default_ttl_s=60, now=T0,
    )
    _, secret = signals.issue_credential(session, producer=producer)
    _, other_secret = signals.issue_credential(session, producer=other)
    assert signals.resolve_producer_credential(session, raw_token=secret).id == producer.id
    assert signals.resolve_producer_credential(
        session, raw_token=other_secret
    ).id == other.id
    assert signals.resolve_producer_credential(session, raw_token="kas_bogus") is None


def test_revoking_a_credential_stops_ingestion(session, producer):
    credential, secret = signals.issue_credential(session, producer=producer)
    assert signals.resolve_producer_credential(session, raw_token=secret) is not None
    signals.revoke_credential(
        session, producer_id=producer.id, token_id=credential.token_id
    )
    assert signals.resolve_producer_credential(session, raw_token=secret) is None


def test_revoking_a_producer_revokes_its_credentials(session, producer):
    _, secret = signals.issue_credential(session, producer=producer)
    signals.revoke_producer(session, owner_id="owner-1", name="quant")
    assert signals.resolve_producer_credential(session, raw_token=secret) is None


def test_rotation_keeps_the_old_credential_valid_until_revoked(session, producer):
    first, first_secret = signals.issue_credential(session, producer=producer)
    _, second_secret = signals.issue_credential(session, producer=producer)
    assert signals.resolve_producer_credential(session, raw_token=first_secret) is not None
    assert signals.resolve_producer_credential(session, raw_token=second_secret) is not None
    signals.revoke_credential(
        session, producer_id=producer.id, token_id=first.token_id
    )
    assert signals.resolve_producer_credential(session, raw_token=first_secret) is None
    assert signals.resolve_producer_credential(session, raw_token=second_secret) is not None


# ---------------------------------------------------------------------------
# the evaluation-side loader
# ---------------------------------------------------------------------------


def test_loader_supplies_values_and_provenance(session_factory, producer):
    session = session_factory()
    _ingest(session, producer, {"score": 3.5, "label": "x"})
    session.commit()
    session.close()
    loader = external_context.ExternalSignalLoader(session_factory)
    context = loader.context_for(
        "owner-1", ["external.quant.score"], cutoff=T0
    )
    assert context["external.quant.score"] == 3.5
    assert "external.quant.score.event_time" in context
    assert "external.quant.score.acquired_at" in context


def test_loader_reports_unknown_reason_without_a_value(session_factory, producer):
    loader = external_context.ExternalSignalLoader(session_factory)
    context = loader.context_for("owner-1", ["external.quant.score"], cutoff=T0)
    assert "external.quant.score" not in context
    assert context["external.quant.score.unknown_reason"] == "external_missing"
    health = loader.health()
    assert health["unknown_reasons"]["external_missing"] == 1


def test_loader_caches_per_cutoff_not_across_cutoffs(session_factory, producer):
    session = session_factory()
    # Both values are in the past relative to the ingest clock, so neither is
    # rejected as a future observation.
    _ingest(session, producer, {"score": 1.0, "label": "x"},
            event_time=T0 - timedelta(minutes=30))
    _ingest(session, producer, {"score": 2.0, "label": "x"},
            event_time=T0 - timedelta(minutes=20))
    session.commit()
    session.close()
    loader = external_context.ExternalSignalLoader(session_factory)
    early = loader.context_for(
        "owner-1", ["external.quant.score"], cutoff=T0 - timedelta(minutes=25)
    )
    later = loader.context_for("owner-1", ["external.quant.score"], cutoff=T0)
    assert early["external.quant.score"] == 1.0
    assert later["external.quant.score"] == 2.0


def test_external_reference_parsing():
    assert external_context.external_reference("external.quant.score") == ("quant", "score")
    assert external_context.external_reference("external.a.b.c") is None
    assert external_context.external_reference("fundamentals.pe_ratio") is None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_producer_health_reports_state_not_secrets(session, producer):
    _ingest(session, producer, {"score": 1.0, "label": "x"})
    _ingest(session, producer, {"score": 2.0, "label": "x"},
            event_time=T0 - timedelta(hours=2))
    signals.issue_credential(session, producer=producer)
    health = signals.producer_health(session, owner_id="owner-1", now=T0)
    assert len(health) == 1
    entry = health[0]
    assert entry["name"] == "quant"
    assert entry["accepted"] == 1
    assert entry["late"] == 1
    assert entry["schema_fields"] == ["label", "score"]
    assert "token_hash" not in entry and "secret" not in entry
