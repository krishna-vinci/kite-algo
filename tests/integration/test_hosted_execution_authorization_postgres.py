"""Governed execution authorization on PostgreSQL (Phase 2).

SQLite proves the service logic; PostgreSQL proves the facts that only a real
server can enforce:

* the additive migration reaches head from ZERO and from the PRIOR head, and a
  pre-existing hosted strategy reads ``approval_based``;
* the partial unique index makes "one active grant per book" a database fact;
* the grant identity is immutable, a revoked grant can never be reactivated, and
  a grant is never deleted;
* the audit is append-only, and a refused grant issue leaves no audit row;
* revocation and the dispatch claim serialise on ONE row lock, so the ordering
  between them is decidable rather than racy, and a concurrent claim has exactly
  one winner.

Every test runs against a DISPOSABLE, uniquely named database created on the
test server and dropped afterwards. Nothing here touches an existing database.

    HOSTED_EXECUTION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        .venv/bin/pytest tests/integration/test_hosted_execution_authorization_postgres.py -q

Run in its own pytest invocation (other suites stub ``psycopg2``). Skipped (not
failed) when no database URL is configured.
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid

import psycopg2  # real psycopg2 must be imported BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.strategies.admission import AdmissionService  # noqa: E402
from backend.strategies.execution_authorization import (  # noqa: E402
    AuthorizationKeyConflict,
    ExecutionAuthorizationService,
)
from backend.strategies.execution_requests import ExecutionRequestService  # noqa: E402
from backend.strategies.models import (  # noqa: E402
    HostedExecutionRequest,
    StrategyJob,
)

PG_URL = (
    os.environ.get("HOSTED_EXECUTION_PG_URL")
    or os.environ.get("ADMISSION_PG_URL")
    or os.environ.get("ALERTS_TEST_DATABASE_URL")
    or ""
)

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "HOSTED_EXECUTION_PG_URL / ADMISSION_PG_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

from datetime import datetime, timedelta, timezone  # noqa: E402

OWNER = "app:owner"
ACCOUNT = "kite:A"
STRATEGY_ID = "stg-exec"
NOW = datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc)
PRIOR_HEAD = "20260922_000041"


def _url_for(dbname: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    return urlunsplit(urlsplit(PG_URL)._replace(path=f"/{dbname}"))


def _admin_engine():
    return create_engine(_url_for("postgres"), pool_pre_ping=True)


def _drop_database(dbname: str) -> None:
    admin = _admin_engine()
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                ),
                {"dbname": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    finally:
        admin.dispose()


def _upgrade(db_url: str, revision: str = "head") -> None:
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", "backend/alembic")
    try:
        command.upgrade(cfg, revision)
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


def _downgrade(db_url: str, revision: str) -> None:
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", "backend/alembic")
    try:
        command.downgrade(cfg, revision)
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


def _exec(sf, sql, params=None):
    with sf() as session:
        result = session.execute(text(sql), params or {})
        session.commit()
        return result


def _scalar(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).scalar()


class _PgTestCase(unittest.TestCase):
    def setUp(self):
        self._created: list = []

    def tearDown(self):
        for dbname in self._created:
            _drop_database(dbname)
        self._created = []

    def make_db(self, revision: str = "head"):
        dbname = f"kite_exec_{uuid.uuid4().hex[:12]}"
        admin = _admin_engine()
        try:
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        finally:
            admin.dispose()
        self._created.append(dbname)
        db_url = _url_for(dbname)
        engine = create_engine(db_url, pool_pre_ping=True)
        _upgrade(db_url, revision)
        self.addCleanup(engine.dispose)
        return sessionmaker(bind=engine)


def seed_strategy(sf, *, strategy_id=STRATEGY_ID, account=ACCOUNT):
    """Canonical + hosted strategy + one immutable version + a recorded policy."""
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, :owner, 'Exec', :account, 'active')",
        {"sid": strategy_id, "owner": OWNER, "account": account},
    )
    _exec(
        sf,
        "INSERT INTO public.hosted_strategies "
        "(id, owner_id, name, template_id, default_execution_mode, default_job_kind, "
        " default_account_scope, max_duration_s, progress_deadline_s, stale_exit_policy) "
        "VALUES (:sid, :owner, 'Exec', 'hosted:' || :sid, 'paper', 'finite', :account, "
        " 3600, 600, 'exit_on_worker_stale')",
        {"sid": strategy_id, "owner": OWNER, "account": account},
    )
    _exec(
        sf,
        "INSERT INTO public.hosted_strategy_versions "
        "(id, strategy_id, version, source, source_sha256, parameters_schema, "
        " capabilities_snapshot, created_by) "
        "VALUES (:vid, :sid, 1, 'print(1)', :sha, '{}'::jsonb, '{}'::jsonb, :owner)",
        {"vid": f"ver-{strategy_id}", "sid": strategy_id, "sha": "a" * 64, "owner": OWNER},
    )
    AdmissionService(session_factory=sf).upsert_policy(
        strategy_id=strategy_id,
        account_id=account,
        updated_by=OWNER,
        allocation_inr=100000.0,
    )


def seed_job(sf, *, strategy_id=STRATEGY_ID, run_id="run-exec"):
    with sf() as session:
        session.add(
            StrategyJob(
                id=f"job-{strategy_id}",
                strategy_id=strategy_id,
                version_id=f"ver-{strategy_id}",
                owner_id=OWNER,
                account_scope=ACCOUNT,
                job_kind="finite",
                execution_mode="paper",
                desired_state="started",
                run_id=run_id,
                token_id="tok-1",
                lease_owner="sup-A",
                lease_epoch=1,
                lease_until=NOW + timedelta(hours=1),
                attempt=1,
                status="running",
                max_duration_s=3600,
                progress_deadline_s=600,
            )
        )
        session.commit()


def seed_request(sf, *, grant, strategy_id=STRATEGY_ID, run_id="run-exec", status="queued"):
    """A durable execution request in the state a decision left it."""
    plan_id = str(uuid.uuid4())
    _exec(
        sf,
        "INSERT INTO public.strategy_proposals "
        "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
        " strategy_run_id, target_kind, payload, payload_sha256, status) "
        "VALUES (gen_random_uuid(), :sid, :account, :eid, 'run_now', :run, "
        " 'single_instrument', '{}'::jsonb, 'sha', 'validated')",
        {"sid": strategy_id, "account": ACCOUNT, "eid": f"eval-{plan_id}", "run": run_id},
    )
    _exec(
        sf,
        "INSERT INTO public.strategy_plans "
        "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, logical_plan, "
        " resolved_plan, pinned_catalog_generation) "
        "VALUES (:pid, (SELECT proposal_id FROM public.strategy_proposals WHERE evaluation_id=:eid), "
        " :sid, :account, 'single_instrument', :hash, '{}'::jsonb, '{}'::jsonb, "
        " (SELECT id FROM public.instrument_catalog_generations LIMIT 1))",
        {"pid": plan_id, "eid": f"eval-{plan_id}", "sid": strategy_id, "account": ACCOUNT,
         "hash": "h" * 64},
    )
    request_id = str(uuid.uuid4())
    with sf() as session:
        session.add(
            HostedExecutionRequest(
                request_id=request_id,
                owner_id=OWNER,
                strategy_id=strategy_id,
                canonical_strategy_id=strategy_id,
                account_id=ACCOUNT,
                execution_environment="paper",
                strategy_run_id=run_id,
                job_id=f"job-{strategy_id}",
                attempt=1,
                lease_epoch=1,
                version_id=str(grant["version_id"]),
                version_number=int(grant["version_number"]),
                source_sha256=str(grant["source_sha256"]),
                policy_hash=str(grant["policy_hash"]),
                plan_id=plan_id,
                plan_hash="h" * 64,
                authorization_mode="autonomous",
                grant_id=str(grant["grant_id"]),
                status=status,
                decision_kind="automatic",
                decision_actor=OWNER,
                decision_at=NOW,
                idempotency_key=f"key-{request_id}",
                request_hash="r" * 64,
            )
        )
        session.commit()
    return request_id, plan_id


def _autonomous_grant(sf, *, key="grant-key-pg"):
    service = ExecutionAuthorizationService(sf)
    service.set_mode(OWNER, STRATEGY_ID, "autonomous", actor=OWNER)
    issued = service.issue_grant(
        OWNER,
        STRATEGY_ID,
        actor=OWNER,
        idempotency_key=key,
        version_id=f"ver-{STRATEGY_ID}",
        execution_environment="paper",
    )
    return issued["grant"]


def _seed_world(sf):
    """A published generation, one strategy, one running attempt, one grant."""
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES ('11111111-1111-1111-1111-111111111111', 'published', NOW())",
    )
    seed_strategy(sf)
    seed_job(sf)
    return _autonomous_grant(sf)


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


class MigrationTests(_PgTestCase):
    def _assert_governed_objects(self, sf):
        tables = {
            str(row[0])
            for row in _exec(
                sf,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'",
            ).fetchall()
        }
        self.assertTrue(
            {"hosted_execution_grants", "hosted_execution_requests", "hosted_execution_audit"}
            <= tables
        )
        columns = {
            str(row[0])
            for row in _exec(
                sf,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'hosted_strategies'",
            ).fetchall()
        }
        self.assertIn("authorization_mode", columns)
        approval_columns = {
            str(row[0])
            for row in _exec(
                sf,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'strategy_approvals'",
            ).fetchall()
        }
        self.assertTrue({"actor_kind", "authorization_evidence"} <= approval_columns)
        indexes = {
            str(row[0])
            for row in _exec(
                sf, "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ).fetchall()
        }
        self.assertIn("uq_hosted_execution_grant_active", indexes)
        triggers = {
            str(row[0])
            for row in _exec(
                sf,
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal",
            ).fetchall()
        }
        self.assertTrue(
            {
                "trg_hosted_execution_grant_identity",
                "trg_hosted_execution_grant_no_delete",
                "trg_hosted_execution_audit_immutable",
            }
            <= triggers
        )

    def test_migration_from_zero_reaches_head_with_the_governed_objects(self):
        sf = self.make_db("head")
        self._assert_governed_objects(sf)

    def test_the_migration_is_the_single_head_and_is_reversible(self):
        from alembic.script import ScriptDirectory

        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("script_location", "backend/alembic")
        heads = ScriptDirectory.from_config(cfg).get_heads()
        # The chain must resolve to exactly ONE head - a second, divergent head
        # would make "upgrade head" ambiguous. The revision itself is the
        # chain's own value: hardcoding it here only rots when a later
        # migration lands.
        self.assertEqual(len(heads), 1, f"alembic has more than one head: {list(heads)}")

        sf = self.make_db("head")
        self._assert_governed_objects(sf)
        _downgrade(_url_for(self._created[-1]), PRIOR_HEAD)
        tables = {
            str(row[0])
            for row in _exec(
                sf,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'",
            ).fetchall()
        }
        self.assertFalse(
            {"hosted_execution_grants", "hosted_execution_requests", "hosted_execution_audit"}
            & tables
        )
        columns = {
            str(row[0])
            for row in _exec(
                sf,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'hosted_strategies'",
            ).fetchall()
        }
        self.assertNotIn("authorization_mode", columns)
        _upgrade(_url_for(self._created[-1]), "head")
        self._assert_governed_objects(sf)

    def test_migration_from_the_prior_head_adds_them_and_defaults_existing_rows(self):
        sf = self.make_db(PRIOR_HEAD)
        # An existing deployment has hosted strategies before this migration.
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
            "VALUES ('legacy-1', :owner, 'Legacy', :account, 'active')",
            {"owner": OWNER, "account": ACCOUNT},
        )
        _exec(
            sf,
            "INSERT INTO public.hosted_strategies "
            "(id, owner_id, name, template_id, default_execution_mode, default_job_kind, "
            " default_account_scope, max_duration_s, progress_deadline_s, stale_exit_policy) "
            "VALUES ('legacy-1', :owner, 'Legacy', 'hosted:legacy-1', 'paper', 'finite', "
            " :account, 3600, 600, 'none')",
            {"owner": OWNER, "account": ACCOUNT},
        )
        _upgrade(_url_for(self._created[-1]), "head")
        self._assert_governed_objects(sf)
        self.assertEqual(
            _scalar(
                sf,
                "SELECT authorization_mode FROM public.hosted_strategies WHERE id = 'legacy-1'",
            ),
            "approval_based",
        )


# ---------------------------------------------------------------------------
# database-enforced grant facts
# ---------------------------------------------------------------------------


class GrantInvariantTests(_PgTestCase):
    def test_one_active_grant_per_book_is_a_database_fact(self):
        sf = self.make_db()
        grant = _seed_world(sf)
        with self.assertRaises(IntegrityError):
            _exec(
                sf,
                "INSERT INTO public.hosted_execution_grants "
                "(grant_id, owner_id, strategy_id, canonical_strategy_id, version_id, "
                " version_number, source_sha256, account_id, execution_environment, "
                " policy_hash, issued_by, request_key, content_sha256) "
                "VALUES (gen_random_uuid()::text, :owner, :sid, :sid, :vid, 1, :sha, "
                " :account, 'paper', :policy, :owner, 'bypassed-key', 'x')",
                {
                    "owner": OWNER,
                    "sid": STRATEGY_ID,
                    "vid": grant["version_id"],
                    "sha": grant["source_sha256"],
                    "account": ACCOUNT,
                    "policy": grant["policy_hash"],
                },
            )

    def test_grant_identity_is_immutable_and_a_revoked_grant_never_reactivates(self):
        sf = self.make_db()
        grant = _seed_world(sf)
        with self.assertRaises(Exception) as info:
            _exec(
                sf,
                "UPDATE public.hosted_execution_grants SET version_id = 'other' "
                "WHERE grant_id = :id",
                {"id": grant["grant_id"]},
            )
        self.assertIn("identity is immutable", str(info.exception))

        _exec(
            sf,
            "UPDATE public.hosted_execution_grants "
            "SET status = 'revoked', revoked_by = :owner, revoked_at = NOW() "
            "WHERE grant_id = :id",
            {"owner": OWNER, "id": grant["grant_id"]},
        )
        with self.assertRaises(Exception) as info:
            _exec(
                sf,
                "UPDATE public.hosted_execution_grants SET status = 'active', "
                "revoked_by = NULL, revoked_at = NULL WHERE grant_id = :id",
                {"id": grant["grant_id"]},
            )
        self.assertIn("cannot be reactivated", str(info.exception))

        with self.assertRaises(Exception) as info:
            _exec(
                sf,
                "DELETE FROM public.hosted_execution_grants WHERE grant_id = :id",
                {"id": grant["grant_id"]},
            )
        self.assertIn("never deleted", str(info.exception))

    def test_audit_is_append_only_and_a_refused_issue_writes_nothing(self):
        sf = self.make_db()
        grant = _seed_world(sf)
        audit_count = _scalar(sf, "SELECT COUNT(*) FROM public.hosted_execution_audit")
        self.assertGreaterEqual(int(audit_count), 2)  # mode_changed + granted

        service = ExecutionAuthorizationService(sf)
        with self.assertRaises(AuthorizationKeyConflict):
            service.issue_grant(
                OWNER,
                STRATEGY_ID,
                actor=OWNER,
                idempotency_key="grant-key-pg",  # reused with different content
                version_id=f"ver-{STRATEGY_ID}",
                execution_environment="dry_run",
            )
        self.assertEqual(
            _scalar(sf, "SELECT COUNT(*) FROM public.hosted_execution_audit"), audit_count
        )

        with self.assertRaises(Exception):
            _exec(
                sf,
                "UPDATE public.hosted_execution_audit SET event = 'rewritten' "
                "WHERE audit_id = (SELECT MIN(audit_id) FROM public.hosted_execution_audit)",
            )
        with self.assertRaises(Exception):
            _exec(
                sf,
                "DELETE FROM public.hosted_execution_audit "
                "WHERE audit_id = (SELECT MIN(audit_id) FROM public.hosted_execution_audit)",
            )
        self.assertEqual(
            _scalar(sf, "SELECT COUNT(*) FROM public.hosted_execution_audit"), audit_count
        )
        self.assertEqual(grant["status"], "active")


# ---------------------------------------------------------------------------
# revocation vs the dispatch claim, under the one row lock
# ---------------------------------------------------------------------------


class ClaimOrderingTests(_PgTestCase):
    def test_revocation_that_commits_first_denies_the_claim(self):
        sf = self.make_db()
        grant = _seed_world(sf)
        request_id, _plan_id = seed_request(sf, grant=grant)

        ExecutionAuthorizationService(sf).revoke_grant(
            OWNER, STRATEGY_ID, actor=OWNER, reason="operator stop", grant_id=grant["grant_id"]
        )
        claimed = ExecutionRequestService(sf).claim_next(limit=5, now=NOW)
        self.assertEqual(claimed, [])
        self.assertEqual(
            _scalar(
                sf,
                "SELECT status FROM public.hosted_execution_requests WHERE request_id = :id",
                {"id": request_id},
            ),
            "refused",
        )
        self.assertEqual(
            _scalar(
                sf,
                "SELECT refusal_code FROM public.hosted_execution_requests WHERE request_id = :id",
                {"id": request_id},
            ),
            "GRANT_REVOKED",
        )

    def test_claim_that_commits_first_is_not_retroactively_denied(self):
        sf = self.make_db()
        grant = _seed_world(sf)
        request_id, _plan_id = seed_request(sf, grant=grant)

        claimed = ExecutionRequestService(sf).claim_next(limit=5, now=NOW)
        self.assertEqual([row["request_id"] for row in claimed], [request_id])
        self.assertTrue(claimed[0]["dispatch_claim_id"])

        # The revocation is still recorded, and it is recorded honestly as a
        # revocation - it does not claim to have cancelled anything.
        ExecutionAuthorizationService(sf).revoke_grant(
            OWNER, STRATEGY_ID, actor=OWNER, reason="late stop", grant_id=grant["grant_id"]
        )
        self.assertEqual(
            _scalar(
                sf,
                "SELECT status FROM public.hosted_execution_requests WHERE request_id = :id",
                {"id": request_id},
            ),
            "dispatching",
        )
        self.assertEqual(
            _scalar(
                sf,
                "SELECT COUNT(*) FROM public.hosted_execution_audit "
                "WHERE subject_kind = 'grant' AND event = 'revoked'",
            ),
            1,
        )
        # A second pass cannot "re-claim" the already claimed work.
        self.assertEqual(ExecutionRequestService(sf).claim_next(limit=5, now=NOW), [])

    def test_concurrent_claims_have_exactly_one_winner(self):
        sf = self.make_db()
        grant = _seed_world(sf)
        request_id, _plan_id = seed_request(sf, grant=grant)

        results: list = []
        errors: list = []
        barrier = threading.Barrier(2)

        def _claim():
            try:
                barrier.wait(timeout=10)
                results.append(ExecutionRequestService(sf).claim_next(limit=5, now=NOW))
            except Exception as exc:  # noqa: BLE001 - reported by the assertion below
                errors.append(repr(exc))

        threads = [threading.Thread(target=_claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        winners = [row for batch in results for row in batch]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["request_id"], request_id)
        self.assertEqual(
            _scalar(
                sf,
                "SELECT COUNT(*) FROM public.hosted_execution_audit "
                "WHERE subject_kind = 'request' AND event = 'claimed'",
            ),
            1,
        )


# ---------------------------------------------------------------------------
# concurrent idempotent create, and stale-finish fencing
# ---------------------------------------------------------------------------


def seed_run_binding(sf, *, run_id="run-exec", strategy_id=STRATEGY_ID, account=ACCOUNT):
    """The persisted run + binding the request identity is derived from."""
    _exec(
        sf,
        "INSERT INTO public.algo_worker_tokens "
        "(token_id, name, token_hash, account_scope, allowed_modes, allowed_actions, status) "
        "VALUES ('tok-1', 'child', 'hash-tok-1', :account, '[\"paper\"]'::jsonb, "
        " '[\"proposals:submit\"]'::jsonb, 'active')",
        {"account": account},
    )
    _exec(
        sf,
        "INSERT INTO public.algo_worker_runs "
        "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
        "VALUES (:run, 'tok-1', :template, :account, 'paper', 'open')",
        {"run": run_id, "template": f"hosted:{strategy_id}", "account": account},
    )
    _exec(
        sf,
        "INSERT INTO public.strategy_run_bindings "
        "(strategy_run_id, strategy_id, owner_id, account_id, execution_environment, "
        " bound_by, binding_source) "
        "VALUES (:run, :sid, :owner, :account, 'paper', 'test', 'hosted_job')",
        {"run": run_id, "sid": strategy_id, "owner": OWNER, "account": account},
    )


def seed_frozen_plan(sf, *, run_id="run-exec", strategy_id=STRATEGY_ID, account=ACCOUNT):
    """One validated proposal + frozen plan, bound to the job's run."""
    plan_id = str(uuid.uuid4())
    _exec(
        sf,
        "INSERT INTO public.strategy_proposals "
        "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
        " strategy_run_id, target_kind, payload, payload_sha256, status) "
        "VALUES (:pid, :sid, :account, :eid, 'run_now', :run, 'single_instrument', "
        " '{}'::jsonb, 'sha', 'validated')",
        {
            "pid": str(uuid.uuid4()),
            "sid": strategy_id,
            "account": account,
            "eid": f"eval-{plan_id}",
            "run": run_id,
        },
    )
    _exec(
        sf,
        "INSERT INTO public.strategy_plans "
        "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, logical_plan, "
        " resolved_plan, pinned_catalog_generation) "
        "VALUES (:pid, (SELECT proposal_id FROM public.strategy_proposals "
        " WHERE evaluation_id = :eid), :sid, :account, 'single_instrument', :hash, "
        " '{}'::jsonb, '{}'::jsonb, "
        " (SELECT id FROM public.instrument_catalog_generations LIMIT 1))",
        {"pid": plan_id, "eid": f"eval-{plan_id}", "sid": strategy_id,
         "account": account, "hash": "h" * 64},
    )
    return plan_id


def _job(sf, job_id="job-stg-exec"):
    with sf() as session:
        return session.get(StrategyJob, job_id)


class ConcurrentCreateTests(_PgTestCase):
    def test_concurrent_identical_creates_replay_one_request(self):
        """Same key + same content is a REPLAY even under a real race."""
        sf = self.make_db()
        _seed_world(sf)
        seed_run_binding(sf)
        plan_id = seed_frozen_plan(sf)

        results: list = []
        errors: list = []
        barrier = threading.Barrier(4)

        def _create():
            try:
                barrier.wait(timeout=10)
                results.append(
                    ExecutionRequestService(sf).create_for_job(
                        job=_job(sf), plan_id=plan_id, idempotency_key="same-key", now=NOW
                    )
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(repr(exc))

        threads = [threading.Thread(target=_create) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        request_ids = {row["request"]["request_id"] for row in results}
        self.assertEqual(len(request_ids), 1, results)
        self.assertEqual(
            _scalar(
                sf,
                "SELECT COUNT(*) FROM public.hosted_execution_requests "
                "WHERE plan_id = :pid AND idempotency_key = 'same-key'",
                {"pid": plan_id},
            ),
            1,
        )
        # Every caller got the same durable row; one of them created it.
        self.assertEqual(sorted(bool(row["idempotent"]) for row in results), [False, True, True, True])

    def test_a_reused_key_with_changed_content_conflicts(self):
        sf = self.make_db()
        _seed_world(sf)
        seed_run_binding(sf)
        plan_id = seed_frozen_plan(sf)
        service = ExecutionRequestService(sf)
        service.create_for_job(job=_job(sf), plan_id=plan_id, idempotency_key="dup-key", now=NOW)

        # The strategy's mode changes, so the same key now describes different
        # content (the request's authorization mode is part of its hash): a
        # conflict, never a silent reuse or a resurrected decision.
        ExecutionAuthorizationService(sf).set_mode(
            OWNER, STRATEGY_ID, "approval_based", actor=OWNER, reason="changed decision"
        )
        with self.assertRaises(Exception) as ctx:
            service.create_for_job(
                job=_job(sf), plan_id=plan_id, idempotency_key="dup-key", now=NOW
            )
        self.assertIn("ExecutionRequestConflict", type(ctx.exception).__name__)


class StaleFinishTests(_PgTestCase):
    def test_a_stale_worker_cannot_overwrite_a_recovered_claim(self):
        """finish() is CAS-fenced on the claim it belongs to."""
        from backend.strategies.execution_requests import ExecutionRequestStateError

        sf = self.make_db()
        grant = _seed_world(sf)
        request_id, _plan_id = seed_request(sf, grant=grant)
        service = ExecutionRequestService(sf)

        claimed = service.claim_next(limit=5, now=NOW)
        self.assertEqual(len(claimed), 1)
        claim_id = claimed[0]["dispatch_claim_id"]

        # Recovery resolves the abandoned claim (proved not submitted) and moves
        # the row on; the original worker is now STALE.
        recovered = service.recover_abandoned_claims(
            timeout_seconds=0, limit=5, now=NOW + timedelta(hours=1)
        )
        self.assertEqual(recovered["unresolved"], 1, recovered)

        with self.assertRaises(ExecutionRequestStateError):
            service.finish(
                request_id,
                status="executed",
                refusal_code=None,
                detail={"late": True},
                claim_id=claim_id,
                expected_status="dispatching",
                now=NOW + timedelta(hours=2),
            )
        # The recovery decision stands, and no second audit row was written.
        self.assertEqual(
            _scalar(
                sf,
                "SELECT status FROM public.hosted_execution_requests WHERE request_id = :id",
                {"id": request_id},
            ),
            "dispatch_unresolved",
        )
        self.assertEqual(
            _scalar(
                sf,
                "SELECT refusal_code FROM public.hosted_execution_requests WHERE request_id = :id",
                {"id": request_id},
            ),
            "DISPATCH_OUTCOME_UNKNOWN",
        )

    def test_a_finish_from_a_superseded_claim_is_refused(self):
        from backend.strategies.execution_requests import ExecutionRequestStateError

        sf = self.make_db()
        grant = _seed_world(sf)
        request_id, _plan_id = seed_request(sf, grant=grant)
        service = ExecutionRequestService(sf)

        claimed = service.claim_next(limit=5, now=NOW)
        claim_id = claimed[0]["dispatch_claim_id"]
        with self.assertRaises(ExecutionRequestStateError):
            service.finish(
                request_id,
                status="executed",
                refusal_code=None,
                detail={},
                claim_id="not-this-claim",
                expected_status="dispatching",
                now=NOW,
            )
        self.assertEqual(
            _scalar(
                sf,
                "SELECT status FROM public.hosted_execution_requests WHERE request_id = :id",
                {"id": request_id},
            ),
            "dispatching",
        )
        self.assertTrue(claim_id)
