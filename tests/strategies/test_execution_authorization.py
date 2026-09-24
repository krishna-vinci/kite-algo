"""Owner-issued standing authorisation: mode, grants, policy evidence (Phase 2).

These are service-level tests over a real SQLite schema created from the ORM, so
the new tables, the check constraints and the partial unique index are exercised
rather than mocked. PostgreSQL adds the trigger-level guarantees (identity
immutability, no resurrection, a real row lock); those live in
``tests/integration/test_hosted_execution_authorization_postgres.py``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from sqlalchemy import create_engine, inspect, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.strategies.admission import AdmissionService  # noqa: E402
from backend.strategies.execution_authorization import (  # noqa: E402
    AuthorizationInputError,
    AuthorizationKeyConflict,
    AuthorizationModeError,
    AuthorizationPolicyIncomplete,
    ExecutionAuthorizationService,
    GrantNotActive,
    GrantNotFound,
)
from backend.strategies.models import (  # noqa: E402
    HostedExecutionAudit,
    HostedExecutionGrant,
)
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402

OWNER = "app:admin"
ACCOUNT = "kite:paper"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        engine.dispose()


@pytest.fixture()
def world(factory):
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name="governed",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=ACCOUNT,
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="print('v1')",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 1},
        created_by=OWNER,
    )
    version2 = repo.create_version(
        strategy_id=strategy.id,
        source="print('v2')",
        source_sha256="b" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 1},
        created_by=OWNER,
    )
    service = ExecutionAuthorizationService(factory)
    return repo, strategy, version, version2, service


def _record_policy(factory, strategy_id, allocation=100000.0, **extra):
    return AdmissionService(session_factory=factory).upsert_policy(
        strategy_id=str(strategy_id),
        account_id=ACCOUNT,
        updated_by=OWNER,
        allocation_inr=allocation,
        **extra,
    )


def _audit_events(factory, strategy_id):
    with factory() as session:
        return [
            str(row.event)
            for row in session.execute(
                select(HostedExecutionAudit)
                .where(HostedExecutionAudit.strategy_id == str(strategy_id))
                .order_by(HostedExecutionAudit.audit_id)
            )
            .scalars()
            .all()
        ]


def test_new_tables_and_columns_exist_in_created_schema(factory):
    engine = factory().bind
    tables = set(inspect(engine).get_table_names())
    assert {
        "hosted_execution_grants",
        "hosted_execution_requests",
        "hosted_execution_audit",
    } <= tables
    assert "authorization_mode" in {
        column["name"] for column in inspect(engine).get_columns("hosted_strategies")
    }
    assert {"actor_kind", "authorization_evidence"} <= {
        column["name"] for column in inspect(engine).get_columns("strategy_approvals")
    }


def test_default_authorization_mode_is_approval_based(world):
    repo, strategy, _v1, _v2, service = world
    with repo.session_factory() as session:
        value = session.execute(
            text("SELECT authorization_mode FROM hosted_strategies WHERE id = :id"),
            {"id": strategy.id},
        ).scalar_one()
    assert value == "approval_based"
    assert service.authorization_mode(strategy.id) == "approval_based"


def test_autonomous_selection_alone_grants_nothing(world):
    _repo, strategy, _v1, _v2, service = world
    _record_policy(service.session_factory, strategy.id)
    result = service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    assert result["authorization_mode"] == "autonomous"
    status = service.status(OWNER, strategy.id)
    assert status["active_grant"] is None
    assert status["grant_usable"] is False
    assert "GRANT_REQUIRED" in status["blocking_reasons"]


def test_unknown_mode_is_refused_and_owner_scope_is_enforced(world):
    _repo, strategy, _v1, _v2, service = world
    with pytest.raises(AuthorizationInputError):
        service.set_mode(OWNER, strategy.id, "auto", actor=OWNER)
    with pytest.raises(GrantNotFound):
        service.set_mode("app:someone-else", strategy.id, "autonomous", actor="app:someone-else")
    with pytest.raises(GrantNotFound):
        service.status("app:someone-else", strategy.id)


def test_policy_hash_is_stable_and_moves_with_either_half(world):
    repo, strategy, _v1, _v2, service = world
    _record_policy(repo.session_factory, strategy.id, allocation=50000.0)
    first = service.policy_basis(OWNER, strategy.id)

    # A metadata change must not move the policy hash.
    repo.update_strategy(OWNER, strategy.id, description="notes")
    assert service.policy_basis(OWNER, strategy.id)["policy_hash"] == first["policy_hash"]

    # The admission half moves it.
    _record_policy(repo.session_factory, strategy.id, allocation=75000.0)
    tightened = service.policy_basis(OWNER, strategy.id)
    assert tightened["policy_hash"] != first["policy_hash"]

    # A policy-free strategy has no concrete basis at all.
    assert first["concrete"] is True

    # The mandatory run-protection half moves it too: a tightening is not a
    # hidden subset comparison - ANY change invalidates the grant.
    with repo.session_factory() as session:
        session.execute(
            text("UPDATE hosted_strategies SET stale_exit_policy = 'none' WHERE id = :id"),
            {"id": strategy.id},
        )
        session.commit()
    assert service.policy_basis(OWNER, strategy.id)["policy_hash"] != tightened["policy_hash"]


def test_grant_before_autonomous_mode_is_refused(world):
    repo, strategy, version, _v2, service = world
    _record_policy(repo.session_factory, strategy.id)
    with pytest.raises(AuthorizationModeError):
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-0001",
            version_id=version.id,
            execution_environment="paper",
        )


def test_grant_without_a_concrete_policy_is_refused(world):
    _repo, strategy, version, _v2, service = world
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    with pytest.raises(AuthorizationPolicyIncomplete):
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-0002",
            version_id=version.id,
            execution_environment="paper",
        )
    # An allocation of 0 is a real (and unspendable) limit, not a mandate.
    _record_policy(service.session_factory, strategy.id, allocation=0.0)
    with pytest.raises(AuthorizationPolicyIncomplete):
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-0003",
            version_id=version.id,
            execution_environment="paper",
        )


def test_grant_requires_an_owned_version(world):
    repo, strategy, version, _v2, service = world
    _record_policy(repo.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    with pytest.raises(AuthorizationInputError):
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-0004",
            version_id="ver_does_not_exist",
            execution_environment="paper",
        )
    with pytest.raises(GrantNotFound):
        service.issue_grant(
            "app:someone-else",
            strategy.id,
            actor="app:someone-else",
            idempotency_key="grant-key-0005",
            version_id=version.id,
            execution_environment="paper",
        )


def test_identical_repeat_is_idempotent_and_changed_content_conflicts(world):
    repo, strategy, version, version2, service = world
    _record_policy(repo.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    first = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-1000",
        version_id=version.id,
        execution_environment="paper",
    )
    assert first["idempotent"] is False

    replay = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-1000",
        version_id=version.id,
        execution_environment="paper",
    )
    assert replay["idempotent"] is True
    assert replay["grant"]["grant_id"] == first["grant"]["grant_id"]

    with pytest.raises(AuthorizationKeyConflict):
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-1000",
            version_id=version2.id,
            execution_environment="paper",
        )
    assert _audit_events(repo.session_factory, strategy.id) == ["mode_changed", "granted"]


def test_new_grant_supersedes_the_previous_active_grant(world):
    repo, strategy, version, _v2, service = world
    _record_policy(repo.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    first = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-2000",
        version_id=version.id,
        execution_environment="paper",
    )
    _record_policy(
        repo.session_factory, strategy.id, allocation=200000.0, daily_loss_budget_inr=5000.0
    )
    second = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-2001",
        version_id=version.id,
        execution_environment="paper",
    )
    old = service.get_grant(first["grant"]["grant_id"])
    assert old["status"] == "superseded"
    assert old["superseded_by"] == second["grant"]["grant_id"]
    assert old["supersession_reason"] == "replaced_by_new_grant"
    assert _audit_events(repo.session_factory, strategy.id) == [
        "mode_changed",
        "granted",
        "superseded",
        "granted",
    ]

    # ONE active grant per (strategy, account, environment): the partial unique
    # index is what makes that a database fact.
    with repo.session_factory() as session:
        active = (
            session.execute(
                select(HostedExecutionGrant).where(
                    HostedExecutionGrant.strategy_id == strategy.id,
                    HostedExecutionGrant.status == "active",
                )
            )
            .scalars()
            .all()
        )
    assert len(active) == 1


def test_replay_of_a_revoked_key_never_resurrects_the_grant(world):
    repo, strategy, version, _v2, service = world
    _record_policy(repo.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    issued = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-3000",
        version_id=version.id,
        execution_environment="paper",
    )
    grant_id = issued["grant"]["grant_id"]
    service.revoke_grant(OWNER, strategy.id, actor=OWNER, reason="operator stop", grant_id=grant_id)

    replay = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-3000",
        version_id=version.id,
        execution_environment="paper",
    )
    assert replay["idempotent"] is True
    assert replay["grant"]["grant_id"] == grant_id
    assert replay["grant"]["status"] == "revoked"
    assert service.active_grant(strategy.id) is None

    with pytest.raises(GrantNotActive):
        service.revoke_grant(OWNER, strategy.id, actor=OWNER, grant_id=grant_id)


def test_changing_mode_back_to_approval_based_supersedes_the_grant(world):
    repo, strategy, version, _v2, service = world
    _record_policy(repo.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    issued = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-4000",
        version_id=version.id,
        execution_environment="paper",
    )
    service.set_mode(OWNER, strategy.id, "approval_based", actor=OWNER, reason="back to review")
    assert service.get_grant(issued["grant"]["grant_id"])["status"] == "superseded"
    status = service.status(OWNER, strategy.id)
    assert status["authorization_mode"] == "approval_based"
    assert status["active_grant"] is None
    assert "AUTHORIZATION_MODE_NOT_AUTONOMOUS" in status["blocking_reasons"]


# ---------------------------------------------------------------------------
# execution-time evaluation
# ---------------------------------------------------------------------------


def _autonomous_grant(world, *, key, version):
    repo, strategy, _v1, _v2, service = world
    _record_policy(repo.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    return service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key=key,
        version_id=version.id,
        execution_environment="paper",
        now=NOW,
    )


def _evaluate(world, service, grant, **overrides):
    _repo, strategy, version, _v2, _svc = world
    payload = {
        "strategy_id": strategy.id,
        "mode": "autonomous",
        "account_id": ACCOUNT,
        "environment": "paper",
        "version_id": version.id,
        "source_sha256": version.source_sha256,
        # The caller always passes the CURRENT policy hash, exactly as the
        # request service does at dispatch-claim time; the grant's recorded hash
        # is what it is compared against.
        "policy_hash": service.policy_basis(OWNER, strategy.id)["policy_hash"],
        "grant_id": grant["grant"]["grant_id"],
        "now": NOW,
    }
    payload.update(overrides)
    return service.evaluate(**payload)


def test_evaluate_authorizes_only_the_exact_bound_work(world):
    repo, strategy, version, version2, service = world
    grant = _autonomous_grant(world, key="grant-key-5000", version=version)
    good = _evaluate(world, service, grant)
    assert good["authorized"] is True
    assert good["grant"]["grant_id"] == grant["grant"]["grant_id"]

    assert (
        _evaluate(world, service, grant, mode="approval_based")["refusal_code"]
        == "AUTHORIZATION_MODE_NOT_AUTONOMOUS"
    )
    # changed code version, then a changed source hash
    assert (
        _evaluate(world, service, grant, version_id=version2.id)["refusal_code"]
        == "GRANT_VERSION_MISMATCH"
    )
    assert (
        _evaluate(world, service, grant, source_sha256="c" * 64)["refusal_code"]
        == "GRANT_SOURCE_CHANGED"
    )
    # changed policy - a tightening included
    _record_policy(repo.session_factory, strategy.id, allocation=12345.0)
    assert _evaluate(world, service, grant)["refusal_code"] == "GRANT_POLICY_CHANGED"


def test_evaluate_refuses_other_accounts_and_revoked_grants(world):
    _repo, strategy, version, _v2, service = world
    grant = _autonomous_grant(world, key="grant-key-6000", version=version)
    assert (
        _evaluate(world, service, grant, account_id="kite:other")["refusal_code"]
        == "GRANT_ACCOUNT_MISMATCH"
    )
    assert (
        _evaluate(world, service, grant, environment="live")["refusal_code"]
        == "GRANT_ACCOUNT_MISMATCH"
    )

    service.revoke_grant(OWNER, strategy.id, actor=OWNER, grant_id=grant["grant"]["grant_id"])
    assert _evaluate(world, service, grant)["refusal_code"] == "GRANT_REVOKED"
    # A revocation is evaluated at the moment of use, not only at issue.
    assert (
        _evaluate(world, service, grant, now=NOW + timedelta(days=30))["refusal_code"]
        == "GRANT_REVOKED"
    )


def test_expired_grant_is_refused(world):
    _repo, strategy, version, _v2, service = world
    _record_policy(service.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    issued = service.issue_grant(
        OWNER,
        strategy.id,
        actor=OWNER,
        idempotency_key="grant-key-7000",
        version_id=version.id,
        execution_environment="paper",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    assert _evaluate(world, service, issued, now=NOW + timedelta(minutes=30))["authorized"] is True
    later = _evaluate(world, service, issued, now=NOW + timedelta(hours=2))
    assert later["authorized"] is False
    assert later["refusal_code"] == "GRANT_EXPIRED"

    # An expiry in the past is refused at issue time rather than stored.
    with pytest.raises(AuthorizationInputError):
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-7001",
            version_id=version.id,
            execution_environment="paper",
            expires_at=NOW - timedelta(minutes=1),
            now=NOW,
        )


def test_environment_vocabulary_is_enforced(world):
    _repo, strategy, version, _v2, service = world
    _record_policy(service.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    with pytest.raises(AuthorizationInputError):
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-8000",
            version_id=version.id,
            execution_environment="prod",
        )


def test_live_grant_is_refused_while_hosted_live_is_disabled(world, monkeypatch):
    _repo, strategy, version, _v2, service = world
    _record_policy(service.session_factory, strategy.id)
    service.set_mode(OWNER, strategy.id, "autonomous", actor=OWNER)
    monkeypatch.delenv("HOSTED_LIVE_ENABLED", raising=False)
    with pytest.raises(Exception) as info:
        service.issue_grant(
            OWNER,
            strategy.id,
            actor=OWNER,
            idempotency_key="grant-key-9000",
            version_id=version.id,
            execution_environment="live",
        )
    assert "LIVE_DISABLED" in str(info.value.as_detail())
