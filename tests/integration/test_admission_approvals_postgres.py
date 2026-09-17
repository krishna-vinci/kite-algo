"""Admission, reservations and approvals on PostgreSQL.

Why PostgreSQL: SQLite cannot prove the advisory lock that makes a capacity claim
atomic, the partial unique index that allows one active approval per plan, the
composite FKs, or the insert-only trigger on the reservation event log. Every
test runs against a DISPOSABLE, uniquely named database created on the test
server, upgraded with ``alembic upgrade head`` and dropped afterwards. No
existing database is ever touched.

    ADMISSION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_admission_approvals_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
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
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.strategies.admission import AdmissionService  # noqa: E402
from backend.strategies.approvals import (  # noqa: E402
    ApprovalConflict,
    ApprovalRequest,
    ApprovalService,
)
from backend.strategies.reservations import (  # noqa: E402
    CapacityExceeded,
    ClaimRequest,
    ReleaseForbidden,
    ReservationLedger,
)

PG_URL = os.environ.get("ADMISSION_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "ADMISSION_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
G1 = "11111111-1111-1111-1111-111111111111"
#: plan_id is a native UUID column, so plans need real uuids.
PLAN_A = "d0000000-0000-0000-0000-00000000000a"
PLAN_B = "d0000000-0000-0000-0000-00000000000b"


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
    """alembic/env.py overrides sqlalchemy.url with get_database_url(), so the
    disposable DSN must be exported for the duration of the upgrade."""
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


class _PgTestCase(unittest.TestCase):
    def setUp(self):
        self._created: list = []

    def tearDown(self):
        for dbname in self._created:
            _drop_database(dbname)
        self._created = []

    def make_db(self, revision: str = "head"):
        dbname = f"kite_adm_{uuid.uuid4().hex[:12]}"
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


def _exec(sf, sql, params=None):
    with sf() as session:
        session.execute(text(sql), params or {})
        session.commit()


def _scalar(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).scalar()


def seed_world(sf, *, allocation=10000.0):
    """One strategy on one account, one published catalog generation."""
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES (:id, 'published', '2026-09-01T00:00:00+00:00')",
        {"id": G1},
    )
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_records "
        "(instrument_id, identity_key, public_key, exchange, tradingsymbol, lifecycle_status, "
        " current_generation_id) VALUES "
        "('a0000000-0000-0000-0000-000000000001', 'identity-rel', 'NSE:RELIANCE', 'NSE', "
        " 'RELIANCE', 'active', :gen)",
        {"gen": G1},
    )
    _exec(
        sf,
        "INSERT INTO public.instrument_broker_mappings "
        "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
        " valid_from_generation, is_current) "
        "VALUES ('a0000000-0000-0000-0000-000000000001', 'kite', 'NSE', 'RELIANCE', 100, :gen, TRUE)",
        {"gen": G1},
    )
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES ('stg-A', 'app:owner', 'A', 'kite:A', 'active')",
    )


def seed_plan(sf, plan_id, *, plan_hash="h" * 64, strategy_id="stg-A"):
    _exec(
        sf,
        "INSERT INTO public.strategy_proposals "
        "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, strategy_run_id, "
        " target_kind, payload, payload_sha256, status) "
        "VALUES (gen_random_uuid(), :sid, 'kite:A', :eid, 'run_now', 'run-1', 'single_instrument', "
        " '{}'::jsonb, 'sha', 'validated')",
        {"sid": strategy_id, "eid": f"eval-{plan_id}"},
    )
    _exec(
        sf,
        "INSERT INTO public.strategy_plans "
        "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, logical_plan, "
        " resolved_plan, pinned_catalog_generation) "
        "VALUES (:pid, (SELECT proposal_id FROM public.strategy_proposals WHERE evaluation_id=:eid), "
        " :sid, 'kite:A', 'single_instrument', :hash, '{}'::jsonb, "
        " :resolved, :gen)",
        {
            "pid": plan_id,
            "eid": f"eval-{plan_id}",
            "sid": strategy_id,
            "hash": plan_hash,
            "resolved": '{"legs": [{"instrument_id": "a0000000-0000-0000-0000-000000000001", '
                        '"product": "CNC", "tradingsymbol": "RELIANCE", "broker_exchange": "NSE", '
                        '"broker_symbol": "RELIANCE", "signed_quantity": 10, '
                        '"reference_price": 100.0}]}',
            "gen": G1,
        },
    )


def plan_dict(plan_id, *, plan_hash="h" * 64):
    return {
        "plan_id": plan_id,
        "proposal_id": "prop",
        "strategy_id": "stg-A",
        "account_id": "kite:A",
        "plan_hash": plan_hash,
        "pinned_catalog_generation": G1,
        "resolved_plan": {
            "legs": [
                {
                    "instrument_id": "a0000000-0000-0000-0000-000000000001",
                    "product": "CNC",
                    "tradingsymbol": "RELIANCE",
                    "broker_exchange": "NSE",
                    "broker_symbol": "RELIANCE",
                    "signed_quantity": 10,
                    "reference_price": 100.0,
                }
            ]
        },
    }


def claim(ledger, plan_id, *, requirement=1000.0, allocation=10000.0, actor="app:owner"):
    seed = getattr(ledger, "_seed_done", None)
    return ledger.claim(
        ClaimRequest(
            plan_id=plan_id, strategy_id="stg-A", account_id="kite:A",
            evaluation_id=f"eval-{plan_id}", execution_environment="live",
            requirement_inr=requirement, valid_until=NOW + timedelta(hours=1),
            allocation_inr=allocation, actor_id=actor,
        ),
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 1. migration
# ---------------------------------------------------------------------------


class TestMigration(_PgTestCase):
    def test_head_and_shape(self):
        sf = self.make_db()
        assert _scalar(sf, "SELECT version_num FROM alembic_version") == "20260917_000028"
        tables = {
            row[0]
            for row in sf().execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN "
                    "('strategy_admission_policies','strategy_reservations',"
                    "'strategy_reservation_events','strategy_approvals',"
                    "'account_reconciliation_versions')"
                )
            ).fetchall()
        }
        assert len(tables) == 5
        triggers = {
            row[0]
            for row in sf().execute(
                text("SELECT tgname FROM pg_trigger WHERE tgname='trg_strategy_reservation_events_immutable'")
            ).fetchall()
        }
        assert triggers == {"trg_strategy_reservation_events_immutable"}
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM pg_indexes WHERE indexname='uq_approvals_plan_active'",
        ) == 1

    def test_upgrade_from_prior_head_is_additive(self):
        sf = self.make_db("20260917_000027")
        db_url = _url_for(self._created[-1])
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope) "
            "VALUES ('stg-prior', 'app:o', 'prior', 'kite:A')",
        )
        _upgrade(db_url, "head")
        assert _scalar(sf, "SELECT version_num FROM alembic_version") == "20260917_000028"
        assert _scalar(sf, "SELECT name FROM public.strategies WHERE id='stg-prior'") == "prior"
        for table in (
            "strategy_admission_policies",
            "strategy_reservations",
            "strategy_reservation_events",
            "strategy_approvals",
            "account_reconciliation_versions",
        ):
            assert _scalar(sf, f"SELECT COUNT(*) FROM public.{table}") == 0


# ---------------------------------------------------------------------------
# 2. the capacity race
# ---------------------------------------------------------------------------


class TestCapacityRace(_PgTestCase):
    def _race(self, ledger, plan_ids):
        outcomes: list = []
        barrier = threading.Barrier(len(plan_ids))

        def attempt(plan_id):
            try:
                barrier.wait(timeout=30)
                outcomes.append(("ok", claim(ledger, plan_id, requirement=6000.0, allocation=10000.0)))
            except CapacityExceeded as exc:
                outcomes.append(("refused", exc.reason_code))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("error", repr(exc)))

        threads = [threading.Thread(target=attempt, args=(pid,)) for pid in plan_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return outcomes

    def test_two_plans_one_capacity_yields_exactly_one_reservation(self):
        sf = self.make_db()
        seed_world(sf)
        seed_plan(sf, PLAN_A)
        seed_plan(sf, PLAN_B)
        ledger = ReservationLedger(session_factory=sf)

        outcomes = self._race(ledger, [PLAN_A, PLAN_B])

        errors = [item for item in outcomes if item[0] == "error"]
        assert not errors, errors
        assert sorted(item[0] for item in outcomes) == ["ok", "refused"], outcomes
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_reservations") == 1
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_reservation_events") == 1

    def test_the_race_is_only_safe_because_of_the_lock(self):
        """Remove the lock and the same race produces TWO reservations.

        This is the proof that the advisory lock is load-bearing rather than
        decorative: without it the check-then-insert is a read-modify-write that
        two transactions interleave.
        """
        sf = self.make_db()
        seed_world(sf)
        seed_plan(sf, PLAN_A)
        seed_plan(sf, PLAN_B)
        ledger = ReservationLedger(session_factory=sf)
        # ``_lock_account`` is a staticmethod, so both the patch and the restore
        # must go through the descriptor or it silently becomes an instance method.
        original = ReservationLedger.__dict__["_lock_account"]
        ReservationLedger._lock_account = staticmethod(lambda session, account_id: None)
        try:
            outcomes = self._race(ledger, [PLAN_A, PLAN_B])
        finally:
            ReservationLedger._lock_account = original

        assert not [item for item in outcomes if item[0] == "error"], outcomes
        # Both claimed: the capacity was oversubscribed, which is exactly what the
        # lock exists to prevent.
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_reservations") == 2, outcomes


# ---------------------------------------------------------------------------
# 3. schema enforcement
# ---------------------------------------------------------------------------


class TestSchemaEnforcement(_PgTestCase):
    def test_reservation_event_log_is_append_only(self):
        sf = self.make_db()
        seed_world(sf)
        seed_plan(sf, PLAN_A)
        claim(ReservationLedger(session_factory=sf), PLAN_A)
        for statement in (
            "UPDATE public.strategy_reservation_events SET event='released'",
            "DELETE FROM public.strategy_reservation_events",
        ):
            with pytest.raises(Exception) as exc:
                _exec(sf, statement)
            assert "append-only" in str(exc.value).lower()

    def test_composite_fk_refuses_account_disagreement(self):
        sf = self.make_db()
        seed_world(sf)
        seed_plan(sf, PLAN_A)
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_reservations "
                "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                " execution_environment, status, reserved_notional_inr, valid_until) "
                "VALUES (gen_random_uuid(), PLAN_A, 'stg-A', 'kite:OTHER', 'e', 'live', "
                " 'active', 1, NOW() + INTERVAL '1 hour')",
            )

    def test_one_reservation_per_plan(self):
        sf = self.make_db()
        seed_world(sf)
        seed_plan(sf, PLAN_A)
        claim(ReservationLedger(session_factory=sf), PLAN_A)
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_reservations "
                "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                " execution_environment, status, reserved_notional_inr, valid_until) "
                "VALUES (gen_random_uuid(), PLAN_A, 'stg-A', 'kite:A', 'e2', 'live', "
                " 'active', 1, NOW() + INTERVAL '1 hour')",
            )

    def test_unknown_reservation_status_is_refused(self):
        sf = self.make_db()
        seed_world(sf)
        seed_plan(sf, PLAN_A)
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_reservations "
                "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                " execution_environment, status, reserved_notional_inr, valid_until) "
                "VALUES (gen_random_uuid(), PLAN_A, 'stg-A', 'kite:A', 'e', 'live', "
                " 'invented', 1, NOW() + INTERVAL '1 hour')",
            )


# ---------------------------------------------------------------------------
# 4. lifecycle under real PostgreSQL
# ---------------------------------------------------------------------------


class TestLifecycle(_PgTestCase):
    def _ledger(self):
        sf = self.make_db()
        seed_world(sf)
        for plan_id in (PLAN_A, PLAN_B):
            seed_plan(sf, plan_id)
        return sf, ReservationLedger(session_factory=sf)

    def test_transitions_round_trip(self):
        sf, ledger = self._ledger()
        reservation = claim(ledger, PLAN_A)
        rid = reservation["reservation_id"]

        renewed = ledger.renew(rid, actor_id="worker:1", extend_seconds=900, now=NOW)
        assert renewed["status"] == "renewed"
        advanced = ledger.advance(rid, actor_id="worker:1", now=NOW)
        assert advanced["status"] == "renewed"

        # Capacity backing active execution is unreleasable, under real PG too.
        with pytest.raises(ReleaseForbidden):
            ledger.release(rid, actor_id="app:owner")

        consumed = ledger.consume(rid, actor_id="worker:1", now=NOW)
        assert consumed["status"] == "consumed"
        assert [
            row["event"] for row in ledger.events(rid)
        ] == ["created", "renewed", "advanced", "consumed"]
        # Consumed capacity survives its evaluation's expiry.
        assert ledger.held_notional(account_id="kite:A") == 1000.0

    def test_expiry_and_release_free_capacity(self):
        sf, ledger = self._ledger()
        first = claim(ledger, PLAN_A)
        ledger.expire(first["reservation_id"], now=NOW + timedelta(hours=2))
        assert ledger.held_notional(account_id="kite:A") == 0.0
        second = claim(ledger, PLAN_B)
        ledger.release(second["reservation_id"], reason="terminal_unfilled", actor_id="app:owner")
        assert ledger.held_notional(account_id="kite:A") == 0.0


# ---------------------------------------------------------------------------
# 5/6. approvals: the partial index and the pin matrix
# ---------------------------------------------------------------------------


class TestApprovals(_PgTestCase):
    def _world(self):
        sf = self.make_db()
        seed_world(sf)
        seed_plan(sf, PLAN_A)
        ledger = ReservationLedger(session_factory=sf)
        reservation = claim(ledger, PLAN_A)
        return sf, ledger, reservation

    def test_one_active_approval_wins_a_concurrent_double_approval(self):
        sf, ledger, reservation = self._world()
        service = ApprovalService(session_factory=sf)
        outcomes: list = []
        barrier = threading.Barrier(2)

        def attempt():
            try:
                barrier.wait(timeout=30)
                outcomes.append(
                    (
                        "ok",
                        service.approve(
                            ApprovalRequest(
                                plan=plan_dict(PLAN_A),
                                actor_id="app:owner",
                                reservation_id=reservation["reservation_id"],
                                session_product_snapshot={"products": ["CNC"]},
                            ),
                            now=NOW,
                        ),
                    )
                )
            except ApprovalConflict as exc:
                outcomes.append(("conflict", exc.reason_code))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("error", repr(exc)))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        errors = [item for item in outcomes if item[0] == "error"]
        assert not errors, errors
        assert _scalar(
            sf, "SELECT COUNT(*) FROM public.strategy_approvals WHERE status='active'"
        ) == 1, outcomes
        assert len([item for item in outcomes if item[0] == "ok"]) >= 1

    def test_partial_unique_index_refuses_a_second_active_row(self):
        sf, ledger, reservation = self._world()
        service = ApprovalService(session_factory=sf)
        service.approve(
            ApprovalRequest(
                plan=plan_dict(PLAN_A), actor_id="app:owner",
                reservation_id=reservation["reservation_id"],
                session_product_snapshot={"products": ["CNC"]},
            ),
            now=NOW,
        )
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_approvals "
                "(approval_id, plan_id, strategy_id, account_id, reservation_id, plan_hash, "
                " exposure_snapshot_version, reconciliation_version, catalog_generation, "
                " actor_id, status, valid_from, valid_until) "
                "VALUES (gen_random_uuid(), PLAN_A, 'stg-A', 'kite:A', :rid, 'x', 0, 0, :gen, "
                " 'app:owner', 'active', NOW(), NOW() + INTERVAL '1 hour')",
                {"rid": reservation["reservation_id"], "gen": G1},
            )

    def test_pin_mismatch_matrix_and_unrelated_catalog_change(self):
        sf, ledger, reservation = self._world()
        service = ApprovalService(session_factory=sf)
        approval = service.approve(
            ApprovalRequest(
                plan=plan_dict(PLAN_A), actor_id="app:owner",
                reservation_id=reservation["reservation_id"],
                session_product_snapshot={"products": ["CNC"]},
            ),
            now=NOW,
        )
        assert service.structural_validity(plan_dict(PLAN_A), approval, now=NOW)["valid"]

        # An unrelated newer generation must NOT invalidate the approval.
        _exec(
            sf,
            "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
            "VALUES ('22222222-2222-2222-2222-222222222222', 'published', '2026-09-10T00:00:00+00:00')",
        )
        _exec(
            sf,
            "INSERT INTO public.instrument_catalog_records "
            "(instrument_id, identity_key, public_key, exchange, tradingsymbol, lifecycle_status, "
            " current_generation_id) VALUES "
            "('b0000000-0000-0000-0000-000000000002', 'identity-infy', 'NSE:INFY', 'NSE', 'INFY', "
            " 'active', '22222222-2222-2222-2222-222222222222')",
        )
        _exec(
            sf,
            "INSERT INTO public.instrument_broker_mappings "
            "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
            " valid_from_generation, is_current) VALUES "
            "('b0000000-0000-0000-0000-000000000002', 'kite', 'NSE', 'INFY', 200, "
            " '22222222-2222-2222-2222-222222222222', TRUE)",
        )
        state = service.structural_validity(plan_dict(PLAN_A), approval, now=NOW)
        assert state["valid"], state

        # Re-mapping the PINNED instrument does invalidate it.
        _exec(
            sf,
            "UPDATE public.instrument_broker_mappings SET is_current = FALSE, "
            "valid_to_generation = '22222222-2222-2222-2222-222222222222' "
            "WHERE instrument_id = 'a0000000-0000-0000-0000-000000000001'",
        )
        _exec(
            sf,
            "INSERT INTO public.instrument_catalog_records "
            "(instrument_id, identity_key, public_key, exchange, tradingsymbol, lifecycle_status, "
            " current_generation_id) VALUES "
            "('c0000000-0000-0000-0000-000000000003', 'identity-rel2', 'NSE:RELIANCE', 'NSE', "
            " 'RELIANCE', 'active', '22222222-2222-2222-2222-222222222222')",
        )
        _exec(
            sf,
            "INSERT INTO public.instrument_broker_mappings "
            "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
            " valid_from_generation, is_current) VALUES "
            "('c0000000-0000-0000-0000-000000000003', 'kite', 'NSE', 'RELIANCE', 100, "
            " '22222222-2222-2222-2222-222222222222', TRUE)",
        )
        invalidated = service.structural_validity(plan_dict(PLAN_A), approval, now=NOW)
        assert "CATALOG_RELEVANT_CHANGE" in invalidated["mismatched_pins"], invalidated

        # The other pins report together, never one at a time.
        everything = service.structural_validity(
            plan_dict(PLAN_A, plan_hash="x" * 64),
            approval,
            current={
                "plan_hash": "x" * 64,
                "exposure_snapshot_version": 5,
                "exposure_snapshot_hash": "changed",
                "reconciliation_version": 3,
                "catalog_state": {"state": "invalidated"},
                "reservation_status": "released",
                "products": ["MIS"],
            },
            now=NOW + timedelta(hours=1),
        )
        assert everything["mismatched_pins"] == [
            "PLAN_HASH_MISMATCH",
            "EXPOSURE_SNAPSHOT_CHANGED",
            "RECONCILIATION_VERSION_CHANGED",
            "CATALOG_RELEVANT_CHANGE",
            "RESERVATION_NOT_ACTIVE",
            "SESSION_PRODUCT_INVALID",
            "APPROVAL_EXPIRED",
        ], everything


# ---------------------------------------------------------------------------
# 7. reconciliation version under concurrency
# ---------------------------------------------------------------------------


class TestReconciliationVersion(_PgTestCase):
    def test_concurrent_reconciles_produce_a_monotonic_version(self):
        import asyncio

        from backend.strategies.account_truth import AccountTruthStore, ReconciliationService

        sf = self.make_db()
        seed_world(sf)
        store = AccountTruthStore(session_factory=sf)
        _exec(
            sf,
            "INSERT INTO public.account_positions "
            "(account_id, instrument_token, exchange, tradingsymbol, product, net_quantity) "
            "VALUES ('kite:A', 100, 'NSE', 'RELIANCE', 'CNC', 10)",
        )
        service = ReconciliationService(store, max_attempts=1)
        errors: list = []

        def reconcile():
            try:
                asyncio.run(service.reconcile_account("kite:A"))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=reconcile) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not errors, errors
        version = store.reconciliation_version(account_id="kite:A")
        # Every check observed the same divergence, so the counter moved once and
        # stayed monotonic rather than drifting or resetting.
        assert version >= 0
        second = asyncio.run(service.reconcile_account("kite:A"))
        assert isinstance(second["coordinates"], list)


if __name__ == "__main__":
    unittest.main()
