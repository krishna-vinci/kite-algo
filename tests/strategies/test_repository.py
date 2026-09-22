"""State, fencing and immutability tests for the hosted-strategy store.

Runs on in-memory SQLite (serialised writers), sufficient for the state-machine
and CAS *logic*; genuine concurrent-race behaviour is pinned on PostgreSQL in
``tests/integration/test_hosted_strategy_foundation_postgres.py``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.strategies import models  # noqa: F401,E402 (table registration)
from backend.strategies import service  # noqa: E402
from backend.strategies.repository import (  # noqa: E402
    SqlAlchemyStrategyRepository,
    StrategyConflict,
    StrategyDisabled,
    StrategyFenceError,
    StrategyIdentityError,
    StrategyNotFound,
)
from backend.workflows.repository import Base  # noqa: E402

OWNER = "app:admin"
OTHER = "app:other"

SCHEMA = {
    "type": "object",
    "properties": {"lots": {"type": "integer", "minimum": 1}},
    "required": ["lots"],
}


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def repo(factory):
    return SqlAlchemyStrategyRepository(factory)


def _strategy(repo, owner=OWNER, name=None, **overrides):
    kwargs = dict(
        owner_id=owner,
        name=name or f"s-{uuid.uuid4().hex[:8]}",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope="kite:paper",
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    kwargs.update(overrides)
    return repo.create_strategy(**kwargs)


def _version(repo, strategy_id, schema=None):
    return repo.create_version(
        strategy_id=strategy_id,
        source="print('hi')\n",
        source_sha256="a" * 64,
        parameters_schema=SCHEMA if schema is None else schema,
        capabilities_snapshot={"schema_version": 1},
        created_by=OWNER,
    )


def _job(repo, strategy, version, **overrides):
    kwargs = dict(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={"lots": 1},
    )
    kwargs.update(overrides)
    return repo.create_job(**kwargs)


def _future(minutes=60):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _claim(repo, job, *, holder="sup-A", epoch=0, attempt=1, until=None):
    return repo.claim_job(
        job.id,
        lease_owner=holder,
        expected_lease_epoch=epoch,
        expected_attempt=attempt,
        lease_until=until or _future(),
    )


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_versions_are_numbered_and_immutable(repo):
    strategy = _strategy(repo)
    v1 = _version(repo, strategy.id)
    v2 = _version(repo, strategy.id)
    assert (v1.version, v2.version) == (1, 2)
    assert repo.get_version(strategy.id, 1).source == "print('hi')\n"
    assert [v.version for v in repo.list_versions(strategy.id)] == [1, 2]


def test_duplicate_strategy_name_conflicts(repo):
    _strategy(repo, name="dup")
    with pytest.raises(StrategyConflict):
        _strategy(repo, name="dup")


def test_duplicate_version_number_is_rejected_by_constraint(repo, factory):
    strategy = _strategy(repo)
    _version(repo, strategy.id)
    with pytest.raises(IntegrityError):
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO hosted_strategy_versions "
                    "(id, strategy_id, version, source, source_sha256, parameters_schema, "
                    "capabilities_snapshot, created_by) "
                    "VALUES ('dup', :sid, 1, 'x', 'y', '{}', '{}', 'app:admin')"
                ),
                {"sid": strategy.id},
            )


# ---------------------------------------------------------------------------
# owner scoping / update
# ---------------------------------------------------------------------------


def test_reads_are_owner_scoped(repo):
    strategy = _strategy(repo)
    assert repo.get_strategy(OWNER, strategy.id) is not None
    assert repo.get_strategy(OTHER, strategy.id) is None
    assert repo.list_strategies(OTHER) == []


def test_update_is_owner_scoped_and_rejects_bad_status(repo):
    strategy = _strategy(repo)
    assert repo.update_strategy(OTHER, strategy.id, status="disabled") is None
    updated = repo.update_strategy(OWNER, strategy.id, description="hello", status="disabled")
    assert updated.status == "disabled" and updated.description == "hello"
    with pytest.raises(service.StrategyValidationError):
        repo.update_strategy(OWNER, strategy.id, status="nonsense")


# ---------------------------------------------------------------------------
# job creation: identity, snapshots and the replacement block
# ---------------------------------------------------------------------------


def test_create_job_pins_snapshots_and_account_scope(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version, params={"lots": 3})
    assert job.account_scope == "kite:paper"
    assert job.max_duration_s == 21600 and job.progress_deadline_s == 600
    assert job.params_snapshot == {"lots": 3}
    assert job.capabilities_snapshot == {"schema_version": 1}
    assert job.policy_snapshot["max_duration_s"] == 21600


def test_create_job_rejects_invalid_params(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    with pytest.raises(service.StrategyValidationError):
        _job(repo, strategy, version, params={})  # 'lots' is required


def test_create_job_rejects_mismatched_version(repo):
    strategy_a = _strategy(repo)
    strategy_b = _strategy(repo)
    other_version = _version(repo, strategy_b.id)
    with pytest.raises(StrategyIdentityError):
        _job(repo, strategy_a, other_version)


def test_create_job_rejects_wrong_owner(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    with pytest.raises(StrategyNotFound):
        _job(repo, strategy, version, owner_id=OTHER)


def test_snapshots_are_immutable_after_defaults_and_version_change(repo, factory):
    strategy = _strategy(repo, max_duration_s=21600)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version, params={"lots": 2})

    # Change the strategy defaults and add a new version with a different schema.
    with factory() as session:
        session.execute(
            text(
                "UPDATE hosted_strategies SET max_duration_s = 999, "
                "default_account_scope = 'kite:changed' WHERE id = :id"
            ),
            {"id": strategy.id},
        )
        session.commit()
    _version(repo, strategy.id, schema={"type": "object"})

    reread = repo.get_job(OWNER, job.id)
    assert reread.max_duration_s == 21600
    assert reread.account_scope == "kite:paper"
    assert reread.params_snapshot == {"lots": 2}
    assert reread.policy_snapshot["max_duration_s"] == 21600


def test_create_and_claim_refused_when_strategy_disabled(repo, factory):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)

    # Create is refused while disabled, and writes nothing.
    repo.update_strategy(OWNER, strategy.id, status="disabled")
    with pytest.raises(StrategyDisabled):
        _job(repo, strategy, version)
    with factory() as session:
        assert session.execute(text("SELECT COUNT(*) FROM strategy_jobs")).scalar_one() == 0

    # Re-enable, create a queued job, disable again: claim is refused.
    repo.update_strategy(OWNER, strategy.id, status="active")
    job = _job(repo, strategy, version)
    repo.update_strategy(OWNER, strategy.id, status="disabled")
    with pytest.raises(StrategyDisabled):
        _claim(repo, job, epoch=0, attempt=1)
    assert repo.get_job(OWNER, job.id).status == "queued"

    # Re-enable: the queued job is claimable again.
    repo.update_strategy(OWNER, strategy.id, status="active")
    claimed = _claim(repo, job, epoch=0, attempt=1)
    assert claimed is not None and claimed.lease_epoch == 1


def test_create_job_validates_account_scope_for_requested_mode(repo, factory):
    # A live account is legal for a dry_run default strategy, but a paper job
    # against it must not persist.
    strategy = _strategy(repo, execution_mode="dry_run", account_scope="kite:live-account")
    version = _version(repo, strategy.id)
    with pytest.raises(service.StrategyValidationError):
        _job(repo, strategy, version, execution_mode="paper")
    with factory() as session:
        assert session.execute(text("SELECT COUNT(*) FROM strategy_jobs")).scalar_one() == 0
    # The matching mode is fine.
    assert _job(repo, strategy, version, execution_mode="dry_run").execution_mode == "dry_run"


def test_create_schedule_validates_account_scope_for_requested_mode(repo):
    strategy = _strategy(repo, execution_mode="dry_run", account_scope="kite:live-account")
    version = _version(repo, strategy.id)
    with pytest.raises(service.StrategyValidationError):
        repo.create_schedule(
            strategy_id=strategy.id,
            version_id=version.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            at_time="09:15",
        )


def test_second_active_job_is_blocked(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    _job(repo, strategy, version)
    with pytest.raises(StrategyFenceError):
        _job(repo, strategy, version, params={"lots": 1})


# ---------------------------------------------------------------------------
# lease / fencing (id + lease_owner + epoch + attempt)
# ---------------------------------------------------------------------------


def test_claim_is_a_cas_on_epoch_and_attempt(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)

    claimed = _claim(repo, job, epoch=0, attempt=1)
    assert claimed is not None and claimed.lease_epoch == 1 and claimed.status == "starting"

    # Stale epoch and wrong attempt both lose.
    assert _claim(repo, job, holder="sup-B", epoch=0, attempt=1) is None
    assert _claim(repo, job, holder="sup-B", epoch=1, attempt=2) is None
    # Current epoch/attempt but the job is no longer queued.
    assert _claim(repo, job, holder="sup-B", epoch=1, attempt=1) is None


def test_claim_rejects_blank_holder_and_bad_ttl(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    with pytest.raises(service.StrategyValidationError):
        _claim(repo, job, holder="  ")
    with pytest.raises(service.StrategyValidationError):
        _claim(repo, job, until=datetime.now(timezone.utc) - timedelta(minutes=1))
    with pytest.raises(service.StrategyValidationError):
        _claim(repo, job, until=datetime(2026, 1, 1))  # naive


def test_recovery_required_requires_exact_authority(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)

    # A stale holder (wrong epoch) or wrong attempt cannot mutate state.
    assert (
        repo.mark_recovery_required(
            job.id, lease_owner="sup-A", expected_lease_epoch=0, expected_attempt=1
        )
        is False
    )
    assert (
        repo.mark_recovery_required(
            job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=2
        )
        is False
    )
    assert (
        repo.mark_recovery_required(
            job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
        )
        is True
    )
    assert repo.get_job(OWNER, job.id).status == "recovery_required"


def test_create_job_blocked_until_reconciled(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)
    repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )

    # Enforcement lives in create_job, not only in the helper.
    with pytest.raises(StrategyFenceError):
        _job(repo, strategy, version, params={"lots": 1})

    assert (
        repo.reconcile_recovery(
            job.id, owner_id=OWNER, expected_lease_epoch=1, expected_attempt=2
        )
        is False
    )
    assert (
        repo.reconcile_recovery(
            job.id, owner_id=OWNER, expected_lease_epoch=1, expected_attempt=1
        )
        is True
    )
    new_job = _job(repo, strategy, version, params={"lots": 1}, attempt=2)
    assert new_job.attempt == 2


def test_expire_to_recovery_blocks_real_create_job(repo, factory):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)
    with factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET lease_until = :past WHERE id = :id"),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=1), "id": job.id},
        )
        session.commit()

    assert repo.expire_to_recovery(job.id, expected_attempt=2) is False
    assert repo.expire_to_recovery(job.id, expected_attempt=1) is True
    assert repo.get_job(OWNER, job.id).status == "recovery_required"
    with pytest.raises(StrategyFenceError):
        _job(repo, strategy, version, params={"lots": 1})


def test_recovery_transition_survives_a_later_failing_effect(repo, factory):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)
    assert repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    with pytest.raises(RuntimeError):
        with factory() as session:
            session.execute(
                text("UPDATE strategy_jobs SET status = 'running' WHERE id = :id"), {"id": job.id}
            )
            raise RuntimeError("effect failed")
    assert repo.get_job(OWNER, job.id).status == "recovery_required"


def test_cross_owner_cannot_reconcile(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)
    repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    assert (
        repo.reconcile_recovery(
            job.id, owner_id=OTHER, expected_lease_epoch=1, expected_attempt=1
        )
        is False
    )
    assert repo.has_unreconciled_recovery(OWNER, strategy.id) is True


# ---------------------------------------------------------------------------
# job_kind vs execution_mode + CHECK constraints
# ---------------------------------------------------------------------------


def test_job_kind_and_execution_mode_are_independent(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version, job_kind="continuous", execution_mode="dry_run")
    assert (job.job_kind, job.execution_mode) == ("continuous", "dry_run")


def test_live_execution_mode_is_rejected(repo, factory):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    with pytest.raises(IntegrityError):
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_jobs (id, strategy_id, version_id, owner_id, "
                    "account_scope, job_kind, execution_mode, params_snapshot, "
                    "capabilities_snapshot, policy_snapshot, max_duration_s, progress_deadline_s) "
                    "VALUES ('j2', :sid, :vid, 'app:admin', 'kite:paper', 'finite', 'live', "
                    "'{}', '{}', '{}', 600, 60)"
                ),
                {"sid": strategy.id, "vid": version.id},
            )


def test_weekly_schedule_requires_weekday(repo, factory):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    with pytest.raises(IntegrityError):
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO hosted_strategy_schedules (id, strategy_id, version_id, "
                    "owner_id, account_scope, execution_mode, job_kind, max_duration_s, "
                    "progress_deadline_s, schedule_kind, at_time, weekday) "
                    "VALUES ('s1', :sid, :vid, 'app:admin', 'kite:paper', 'paper', 'finite', "
                    "600, 60, 'weekly', '09:15', NULL)"
                ),
                {"sid": strategy.id, "vid": version.id},
            )


# ---------------------------------------------------------------------------
# schedules: identity + derived snapshots
# ---------------------------------------------------------------------------


def test_create_schedule_derives_and_validates(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    schedule = repo.create_schedule(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={"lots": 1},
        schedule_kind="weekly",
        at_time="09:15",
        weekday=2,
    )
    assert schedule.account_scope == "kite:paper"
    assert schedule.max_duration_s == 21600 and schedule.progress_deadline_s == 600
    assert schedule.params_snapshot == {"lots": 1}


def test_create_schedule_stores_a_monthly_day_of_month(repo):
    """The real creation path supports monthly, not just daily/weekly."""
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    schedule = repo.create_schedule(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={"lots": 1},
        schedule_kind="monthly",
        at_time="09:15",
        day_of_month=15,
    )
    assert schedule.schedule_kind == "monthly"
    assert schedule.day_of_month == 15
    assert schedule.calendar_dates is None
    # The scheduler reads exactly this row.
    from backend.strategies.scheduling import ScheduleScheduler

    stored = ScheduleScheduler(session_factory=repo.session_factory).enabled_schedules()
    assert [row["id"] for row in stored] == [schedule.id]
    assert stored[0]["day_of_month"] == 15
    assert stored[0]["version_id"] == version.id
    assert stored[0]["params_snapshot"] == {"lots": 1}


def test_create_schedule_stores_calendar_dates_normalized(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    schedule = repo.create_schedule(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode="paper",
        params={"lots": 1},
        schedule_kind="calendar",
        at_time="09:15",
        calendar_dates=["2026-11-01", "2026-10-10", "2026-10-10"],
    )
    assert schedule.schedule_kind == "calendar"
    # Sorted and de-duplicated, so an occurrence key is never ambiguous.
    assert list(schedule.calendar_dates) == ["2026-10-10", "2026-11-01"]
    assert schedule.day_of_month is None


def test_create_schedule_rejects_a_kind_without_its_required_field(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    with pytest.raises(service.StrategyValidationError):
        repo.create_schedule(
            strategy_id=strategy.id,
            version_id=version.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            schedule_kind="monthly",
            at_time="09:15",
        )
    with pytest.raises(service.StrategyValidationError):
        repo.create_schedule(
            strategy_id=strategy.id,
            version_id=version.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            schedule_kind="calendar",
            at_time="09:15",
            calendar_dates=[],
        )
    with pytest.raises(service.StrategyValidationError):
        repo.create_schedule(
            strategy_id=strategy.id,
            version_id=version.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            schedule_kind="calendar",
            at_time="09:15",
            calendar_dates=["not-a-date"],
        )


def test_create_schedule_rejects_wrong_owner_and_version(repo):
    strategy_a = _strategy(repo)
    strategy_b = _strategy(repo)
    version_a = _version(repo, strategy_a.id)
    version_b = _version(repo, strategy_b.id)
    with pytest.raises(StrategyNotFound):
        repo.create_schedule(
            strategy_id=strategy_a.id,
            version_id=version_a.id,
            owner_id=OTHER,
            job_kind="finite",
            execution_mode="paper",
            at_time="09:15",
        )
    with pytest.raises(StrategyIdentityError):
        repo.create_schedule(
            strategy_id=strategy_a.id,
            version_id=version_b.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            at_time="09:15",
        )


# ---------------------------------------------------------------------------
# authorized expired-lease recovery + retained attribution
# ---------------------------------------------------------------------------


def _expire_lease(factory, job):
    with factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET lease_until = :past WHERE id = :id"),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=1), "id": job.id},
        )
        session.commit()


def test_expire_to_recovery_authorized_requires_identity_and_expired_lease(repo, factory):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)

    # A still-live lease is not recoverable through the expired path.
    assert (
        repo.expire_to_recovery_authorized(
            job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
        )
        is False
    )

    _expire_lease(factory, job)
    # Wrong owner / epoch / attempt are all refused even when expired.
    assert (
        repo.expire_to_recovery_authorized(
            job.id, lease_owner="sup-B", expected_lease_epoch=1, expected_attempt=1
        )
        is False
    )
    assert (
        repo.expire_to_recovery_authorized(
            job.id, lease_owner="sup-A", expected_lease_epoch=0, expected_attempt=1
        )
        is False
    )
    assert (
        repo.expire_to_recovery_authorized(
            job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=2
        )
        is False
    )
    assert (
        repo.expire_to_recovery_authorized(
            job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
        )
        is True
    )
    persisted = repo.get_job(OWNER, job.id)
    assert persisted.status == "recovery_required"
    # Attribution retained so an authorized state read still works.
    assert persisted.lease_owner == "sup-A"
    assert persisted.lease_until is None


def test_mark_recovery_required_retains_lease_attribution(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)
    assert (
        repo.mark_recovery_required(
            job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
        )
        is True
    )
    persisted = repo.get_job(OWNER, job.id)
    assert persisted.lease_owner == "sup-A"
    assert persisted.lease_until is None


def test_record_progress_only_for_live_jobs(repo):
    strategy = _strategy(repo)
    version = _version(repo, strategy.id)
    job = _job(repo, strategy, version)
    _claim(repo, job, epoch=0, attempt=1)

    assert repo.get_job(OWNER, job.id).last_progress_at is None
    assert repo.record_progress(job.id) is True
    assert repo.get_job(OWNER, job.id).last_progress_at is not None

    repo.mark_recovery_required(
        job.id, lease_owner="sup-A", expected_lease_epoch=1, expected_attempt=1
    )
    # A fenced job accepts no progress.
    assert repo.record_progress(job.id) is False
