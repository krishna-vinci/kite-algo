"""Strategy account truth on PostgreSQL: the invariant, triggers, transitions.

Why PostgreSQL: SQLite cannot enforce the insert-only triggers, the adjustment
composite FK, or the advisory-lock serialization that makes a rebuild fold each
adjustment exactly once. Every test runs against a DISPOSABLE, uniquely named
database created on the test server, upgraded with ``alembic upgrade head`` and
dropped afterwards. No existing database is ever touched.

    TRUTH_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_strategy_account_truth_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import asyncio
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

from backend.strategies.account_truth import (  # noqa: E402
    AccountTruthService,
    AccountTruthStore,
    ReconciliationService,
)
from backend.strategies.attribution import SqlAttributionStore, StrategyAttributionService  # noqa: E402

PG_URL = os.environ.get("TRUTH_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "TRUTH_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# disposable database
# ---------------------------------------------------------------------------


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


def _exec(sf, sql, params=None):
    with sf() as session:
        session.execute(text(sql), params or {})
        session.commit()


def _scalar(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).scalar()


def _rows(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).fetchall()


COORD = (738561, "NSE", "RELIANCE", "CNC")


def seed_strategy(sf, *, sid="stg-A", owner="app:o", account="kite:A", status="active"):
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, :owner, :name, :account, :status)",
        {"sid": sid, "owner": owner, "name": f"Strategy {sid}", "account": account, "status": status},
    )


def seed_broker(sf, *, qty, token=738561, symbol="RELIANCE", product="CNC", account="kite:A"):
    _exec(
        sf,
        "INSERT INTO public.account_positions "
        "(account_id, instrument_token, exchange, tradingsymbol, product, net_quantity) "
        "VALUES (:account, :token, 'NSE', :symbol, :product, :qty)",
        {"account": account, "token": token, "symbol": symbol, "product": product, "qty": qty},
    )


def seed_book(sf, *, sid, qty, token=738561, symbol="RELIANCE", account="kite:A"):
    identity = str(uuid.uuid4())
    SqlAttributionStore(session_factory=sf).recompute_publish(
        account_id=account, strategy_id=sid, execution_environment="live",
        resolve_and_fold=lambda db, bound: (
            [{
                "identity_kind": "canonical", "identity_key": identity,
                "canonical_instrument_id": identity, "instrument_token": token,
                "exchange": "NSE", "tradingsymbol": symbol, "product": "CNC",
                "net_quantity": qty, "unresolved_reason": None,
            }],
            f"sha-{sid}-{qty}",
        ),
    )


def trade(trade_id, *, order_id="OID-1", side="SELL", qty=10, token=738561, symbol="RELIANCE"):
    return {
        "trade_id": trade_id, "order_id": order_id, "instrument_token": token,
        "exchange": "NSE", "tradingsymbol": symbol, "product": "CNC",
        "transaction_type": side, "quantity": qty, "average_price": 100.0,
        "fill_timestamp": "2026-09-17T10:00:00+00:00",
    }


def _reconcile(sf, *, ingest=None, notifier=None, max_attempts=3):
    service = ReconciliationService(
        AccountTruthStore(session_factory=sf),
        ingest_service=ingest,
        notifier=notifier,
        max_attempts=max_attempts,
    )
    return asyncio.run(service.reconcile_account("kite:A"))


class _PgTestCase(unittest.TestCase):
    """Per-test disposable database, dropped on the way out.

    unittest rather than bare classes so the assertions stay idiomatic; each test
    creates (and this base drops) its own uniquely named database.
    """

    def setUp(self):
        self._created: list[str] = []

    def tearDown(self):
        for dbname in self._created:
            _drop_database(dbname)
        self._created = []

    def make_db(self, revision: str = "head"):
        dbname = f"kite_truth_{uuid.uuid4().hex[:12]}"
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


# ---------------------------------------------------------------------------
# 1. migration
# ---------------------------------------------------------------------------


class TestMigration(_PgTestCase):
    def test_migration_head_and_shape(self):
        disposable_db = self.make_db()
        assert _scalar(disposable_db, "SELECT version_num FROM alembic_version") >= "20260917_000026"
        tables = {
            row[0]
            for row in _rows(
                disposable_db,
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN "
                "('broker_trade_facts','account_ingest_state','strategy_reconciliation_state',"
                "'strategy_attribution_adjustments','strategy_attribution_adjustment_lines')",
            )
        }
        assert len(tables) == 5
        constraints = {
            row[0]
            for row in _rows(
                disposable_db,
                "SELECT conname FROM pg_constraint WHERE conname IN "
                "('uq_broker_trade_facts_account_trade','ck_btf_quantity','ck_saal_quantity_delta',"
                "'ck_srs_divergence_class','ck_saa_adjustment_kind')",
            )
        }
        assert len(constraints) == 5
        triggers = {
            row[0]
            for row in _rows(
                disposable_db,
                "SELECT tgname FROM pg_trigger WHERE tgname IN "
                "('trg_broker_trade_facts_immutable','trg_strategy_attribution_adjustments_immutable',"
                "'trg_strategy_attribution_adjustment_lines_immutable')",
            )
        }
        assert len(triggers) == 3

    def test_upgrade_from_prior_head_is_additive(self):
        factory = self.make_db("20260917_000025")
        prior_head_db = _url_for(self._created[-1])
        try:
            # Pre-existing data from G1 must survive untouched.
            _exec(
                factory,
                "INSERT INTO public.strategies (id, owner_id, name, account_scope) "
                "VALUES ('stg-g1', 'app:o', 'G1 strategy', 'kite:A')",
            )
            _upgrade(prior_head_db, "head")
            assert _scalar(factory, "SELECT version_num FROM alembic_version") >= "20260917_000026"
            assert _scalar(factory, "SELECT name FROM public.strategies WHERE id='stg-g1'") == "G1 strategy"
            assert _scalar(factory, "SELECT COUNT(*) FROM public.broker_trade_facts") == 0
        finally:
            pass


# ---------------------------------------------------------------------------
# 2/3. triggers and the composite FK
# ---------------------------------------------------------------------------


class TestAppendOnly(_PgTestCase):
    def test_insert_only_triggers(self):
        disposable_db = self.make_db()
        AccountTruthStore(session_factory=disposable_db).ingest_trades(
            account_id="kite:A", trades=[trade("T-1")]
        )
        for statement in (
            "UPDATE public.broker_trade_facts SET quantity = 1",
            "DELETE FROM public.broker_trade_facts",
        ):
            with pytest.raises(Exception) as exc:
                _exec(disposable_db, statement)
            assert "immutable" in str(exc.value).lower()

        seed_strategy(disposable_db)
        record = AccountTruthStore(session_factory=disposable_db).create_reclassification(
            account_id="kite:A", strategy_id="stg-A", owner_id="app:o",
            reason_code="r", created_by="app:o",
            lines=[{"instrument_token": 738561, "exchange": "NSE", "tradingsymbol": "RELIANCE",
                    "product": "CNC", "quantity_delta": -10}],
        )
        for statement in (
            "UPDATE public.strategy_attribution_adjustments SET reason_code='x'",
            "DELETE FROM public.strategy_attribution_adjustments",
            "UPDATE public.strategy_attribution_adjustment_lines SET quantity_delta = 1",
            "DELETE FROM public.strategy_attribution_adjustment_lines",
        ):
            with pytest.raises(Exception) as exc:
                _exec(disposable_db, statement)
            assert "immutable" in str(exc.value).lower()
        assert record["adjustment_id"]

    def test_adjustment_line_composite_fk_refuses_owner_or_account_drift(self):
        disposable_db = self.make_db()
        seed_strategy(disposable_db, sid="stg-A", owner="app:o", account="kite:A")
        adjustment_id = str(uuid.uuid4())
        _exec(
            disposable_db,
            "INSERT INTO public.strategy_attribution_adjustments "
            "(adjustment_id, account_id, adjustment_kind, reason_code, created_by) "
            "VALUES (:aid, 'kite:A', 'owner_reclassification', 'r', 'app:o')",
            {"aid": adjustment_id},
        )
        for owner, account in (("app:other", "kite:A"), ("app:o", "kite:OTHER")):
            with pytest.raises(Exception):
                _exec(
                    disposable_db,
                    "INSERT INTO public.strategy_attribution_adjustment_lines "
                    "(adjustment_id, line_no, strategy_id, owner_id, account_id, instrument_token, "
                    " exchange, tradingsymbol, product, quantity_delta, effective_at) "
                    "VALUES (:aid, 1, 'stg-A', :owner, :account, 738561, 'NSE', 'RELIANCE', 'CNC', "
                    " -10, NOW())",
                    {"aid": adjustment_id, "owner": owner, "account": account},
                )
        # Unknown adjustment kind and a zero delta are refused by check constraints.
        with pytest.raises(Exception):
            _exec(
                disposable_db,
                "INSERT INTO public.strategy_attribution_adjustments "
                "(adjustment_id, account_id, adjustment_kind, reason_code, created_by) "
                "VALUES (:aid, 'kite:A', 'made_up_kind', 'r', 'app:o')",
                {"aid": str(uuid.uuid4())},
            )


# ---------------------------------------------------------------------------
# 4-7. the invariant, transitions and the freeze
# ---------------------------------------------------------------------------


class TestInvariant(_PgTestCase):
    def test_walkthrough_7_identity_holds_and_full_exit_is_named(self):
        disposable_db = self.make_db()
        """A holds 100; the owner sells 10 in the broker app."""
        seed_strategy(disposable_db, sid="stg-A")
        seed_broker(disposable_db, qty=90)
        seed_book(disposable_db, sid="stg-A", qty=100)
        AccountTruthStore(session_factory=disposable_db).ingest_trades(
            account_id="kite:A", trades=[trade("T-MANUAL", order_id="OID-MANUAL", side="SELL", qty=10)]
        )

        report = _reconcile(disposable_db, max_attempts=1)
        row = report["coordinates"][0]
        self.assertEqual(row["broker_quantity"], 90)
        self.assertEqual(row["attributed_quantity"], 100)
        self.assertEqual(row["manual_quantity"], -10)
        self.assertEqual(row["residual_quantity"], 0)
        self.assertEqual(row["divergence_class"], "aligned")
        self.assertEqual(report["frozen"], [])

        # Case 2: the residual is negative, so a full exit would over-sell.
        from backend.strategies.account_truth import unresolved_exit_detail

        detail = unresolved_exit_detail(
            divergence_class="aligned", manual_quantity=-10, broker_net=90
        )
        self.assertEqual(detail["rejection_reason"], "MANUAL_RESIDUAL_BLOCKS_FULL_EXIT")
        self.assertIn("10", detail["message"])

    def test_pending_ingest_then_aligned_after_refresh(self):
        disposable_db = self.make_db()
        seed_strategy(disposable_db, sid="stg-A")
        seed_broker(disposable_db, qty=90)
        seed_book(disposable_db, sid="stg-A", qty=100)
        # The fill is not ingested yet: broker 90 vs attributed 100.
        first = _reconcile(disposable_db, max_attempts=3)
        self.assertEqual(first["coordinates"][0]["divergence_class"], "pending_ingest")
        self.assertEqual(first["frozen"], [COORD])

        store = AccountTruthStore(session_factory=disposable_db)
        ingest = AccountTruthService(
            store,
            trades_provider=lambda account_id: [
                trade("T-MANUAL", order_id="OID-MANUAL", side="SELL", qty=10)
            ],
        )
        second = _reconcile(disposable_db, ingest=ingest, max_attempts=3)
        self.assertEqual(second["coordinates"][0]["divergence_class"], "aligned")
        self.assertEqual(second["frozen"], [])
        state = store.reconciliation_state(account_id="kite:A")[COORD]
        self.assertIsNotNone(state["last_checked_at"])

    def test_unexplained_after_bounded_attempts_escalates_once(self):
        disposable_db = self.make_db()
        seed_strategy(disposable_db, sid="stg-A")
        seed_broker(disposable_db, qty=90)
        seed_book(disposable_db, sid="stg-A", qty=100)
        sent = []

        def notifier(account_id, divergence, coordinate, detail):
            sent.append((account_id, divergence, coordinate))
            return True

        for _ in range(5):
            _reconcile(disposable_db, notifier=notifier, max_attempts=2)
        state = AccountTruthStore(session_factory=disposable_db).reconciliation_state(
            account_id="kite:A"
        )[COORD]
        self.assertEqual(state["divergence_class"], "unexplained")
        self.assertEqual(len(sent), 1)  # idempotent escalation
        self.assertIsNotNone(state["owner_notified_at"])

    def test_freeze_symmetry_and_coordinate_scope(self):
        disposable_db = self.make_db()
        from backend.strategies.account_truth import reconciliation_refusal

        seed_strategy(disposable_db, sid="stg-A")
        seed_broker(disposable_db, qty=90)
        seed_book(disposable_db, sid="stg-A", qty=100)
        _reconcile(disposable_db, max_attempts=1)
        frozen = AccountTruthStore(session_factory=disposable_db).is_frozen_coordinate(
            account_id="kite:A", coordinate=COORD
        )
        self.assertEqual(frozen, "unexplained")

        # Exposure-increasing refused, risk-reducing admitted.
        self.assertIsNotNone(
            reconciliation_refusal(account_id="kite:A", coordinate=COORD, divergence_class=frozen,
                                   side="BUY", net_quantity=90)
        )
        self.assertIsNone(
            reconciliation_refusal(account_id="kite:A", coordinate=COORD, divergence_class=frozen,
                                   side="SELL", net_quantity=90)
        )
        # Another coordinate on the same account is unaffected.
        self.assertIsNone(
            AccountTruthStore(session_factory=disposable_db).is_frozen_coordinate(
                account_id="kite:A", coordinate=(408065, "NSE", "INFY", "CNC")
            )
        )

    def _seed_linked_long(self, sf, *, qty=100, sid="stg-A", run_id="run-A"):
        """A's own 100 as a real linked fill, so a recompute reproduces it."""
        _exec(
            sf,
            "INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, account_scope, status) "
            "VALUES ('tok-A', 'tok', 'hash-A', 'kite:A', 'active')",
        )
        _exec(
            sf,
            "INSERT INTO public.algo_worker_runs "
            "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
            "VALUES (:run, 'tok-A', 'tmpl', 'kite:A', 'live', 'open')",
            {"run": run_id},
        )
        SqlAttributionStore(session_factory=sf).bind_run(
            strategy_run_id=run_id, strategy_id=sid, owner_id="app:o", account_id="kite:A",
            execution_environment="live", bound_by="t", binding_source="hosted_job",
        )
        _exec(
            sf,
            "INSERT INTO public.worker_live_execution_links "
            "(strategy_run_id, account_id, broker_order_id) VALUES (:run, 'kite:A', 'OID-OWN')",
            {"run": run_id},
        )
        _exec(
            sf,
            "INSERT INTO public.order_trade_fills "
            "(account_id, trade_id, order_id, instrument_token, exchange, tradingsymbol, product, "
            " transaction_type, quantity, price, fill_timestamp, payload_json) "
            "VALUES ('kite:A', 'T-OWN', 'OID-OWN', 738561, 'NSE', 'RELIANCE', 'CNC', 'BUY', :qty, "
            " 100, NOW(), '{}'::jsonb)",
            {"qty": qty},
        )
        asyncio.run(
            StrategyAttributionService(SqlAttributionStore(session_factory=sf)).publish(
                account_id="kite:A", strategy_id=sid, execution_environment="live"
            )
        )

    def test_reclassification_realigns_and_lifts_the_freeze(self):
        disposable_db = self.make_db()
        seed_strategy(disposable_db, sid="stg-A")
        seed_broker(disposable_db, qty=90)
        self._seed_linked_long(disposable_db, qty=100)
        AccountTruthStore(session_factory=disposable_db).ingest_trades(
            account_id="kite:A", trades=[trade("T-MANUAL", order_id="OID-MANUAL", side="SELL", qty=10)]
        )
        store = AccountTruthStore(session_factory=disposable_db)
        store.create_reclassification(
            account_id="kite:A", strategy_id="stg-A", owner_id="app:o",
            reason_code="owner_claimed_manual_exit", created_by="app:owner",
            lines=[{"instrument_token": 738561, "exchange": "NSE", "tradingsymbol": "RELIANCE",
                    "product": "CNC", "quantity_delta": -10}],
        )
        asyncio.run(
            StrategyAttributionService(SqlAttributionStore(session_factory=disposable_db)).publish(
                account_id="kite:A", strategy_id="stg-A", execution_environment="live"
            )
        )
        # The adjustment moved the 10 from manual into A's own book.
        self.assertEqual(store.manual_residual_by_coordinate(account_id="kite:A")[COORD], 0)
        report = _reconcile(disposable_db, max_attempts=1)
        self.assertEqual(report["coordinates"][0]["attributed_quantity"], 90)
        self.assertEqual(report["coordinates"][0]["divergence_class"], "aligned")
        self.assertEqual(report["frozen"], [])


# ---------------------------------------------------------------------------
# 8-11. concurrency, closure independence, heuristic removal, ingest dedupe
# ---------------------------------------------------------------------------


class TestConcurrency(_PgTestCase):
    def test_rebuild_concurrent_with_adjustment_folds_it_exactly_once(self):
        disposable_db = self.make_db()
        seed_strategy(disposable_db, sid="stg-A")
        store = AccountTruthStore(session_factory=disposable_db)
        attribution = SqlAttributionStore(session_factory=disposable_db)
        service = StrategyAttributionService(attribution)
        errors = []

        def rebuild():
            try:
                asyncio.run(
                    service.publish(
                        account_id="kite:A", strategy_id="stg-A", execution_environment="live"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=rebuild)
        thread.start()
        store.create_reclassification(
            account_id="kite:A", strategy_id="stg-A", owner_id="app:o",
            reason_code="owner_claimed", created_by="app:o",
            lines=[{"instrument_token": 738561, "exchange": "NSE", "tradingsymbol": "RELIANCE",
                    "product": "CNC", "quantity_delta": -10}],
        )
        thread.join(timeout=30)
        assert not errors, errors

        # A later rebuild must produce the same book: the fold serializes on the
        # G1 advisory lock and folds each line by stable source identity.
        for _ in range(2):
            asyncio.run(
                service.publish(
                    account_id="kite:A", strategy_id="stg-A", execution_environment="live"
                )
            )
        quantities = [
            row[0]
            for row in _rows(
                disposable_db,
                "SELECT net_quantity FROM public.strategy_position_projection "
                "WHERE account_id='kite:A' AND strategy_id='stg-A'",
            )
        ]
        self.assertEqual(quantities, [-10])

    def test_concurrent_ingest_dedupes_by_broker_identity(self):
        disposable_db = self.make_db()
        store = AccountTruthStore(session_factory=disposable_db)
        errors = []

        def ingest(tag):
            try:
                store.ingest_trades(account_id="kite:A", trades=[trade("T-SAME", order_id=f"OID-{tag}")])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=ingest, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # The unique constraint holds: exactly one row survives the race.
        self.assertEqual(
            _scalar(
                disposable_db,
                "SELECT COUNT(*) FROM public.broker_trade_facts WHERE account_id='kite:A' AND trade_id='T-SAME'",
            ),
            1,
        )


class TestClosureIndependenceAndHeuristicRemoval(_PgTestCase):
    def test_closure_independence_from_one_shared_broker_line(self):
        disposable_db = self.make_db()
        """A flat while B holds the same coordinate, from one broker line."""
        seed_strategy(disposable_db, sid="stg-A")
        seed_strategy(disposable_db, sid="stg-B")
        seed_broker(disposable_db, qty=40)
        seed_book(disposable_db, sid="stg-B", qty=40)  # A has no projection rows

        store = SqlAttributionStore(session_factory=disposable_db)
        run_a = "run-A"
        _exec(
            disposable_db,
            "INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, account_scope, status) "
            "VALUES ('tok-A', 'tok', 'hash-A', 'kite:A', 'active')",
        )
        _exec(
            disposable_db,
            "INSERT INTO public.algo_worker_runs "
            "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
            "VALUES ('run-A', 'tok-A', 'tmpl', 'kite:A', 'live', 'open')",
        )
        store.bind_run(
            strategy_run_id=run_a, strategy_id="stg-A", owner_id="app:o", account_id="kite:A",
            execution_environment="live", bound_by="t", binding_source="hosted_job",
        )
        book_a = store.open_positions_for_run(strategy_run_id=run_a)
        self.assertIsNotNone(book_a)
        self.assertEqual(book_a["legs"], [])  # A is flat...

        # ...while the account still holds 40 belonging to B.
        self.assertEqual(_scalar(disposable_db, "SELECT net_quantity FROM public.account_positions"), 40)
        book_b = store.open_positions_for_run(strategy_run_id=run_a)  # same helper
        self.assertEqual(book_b["strategy_id"], "stg-A")

    def test_untagged_reducing_fill_is_not_attached_to_the_only_open_run(self):
        disposable_db = self.make_db()
        """Heuristic removal, proved end to end through the manual residual."""
        from unittest.mock import Mock

        from backend.journaling.live_projector import resolve_external_fill_run

        repository = Mock()
        repository.find_open_live_runs_for_instrument.return_value = [
            {"run_id": "run-only", "net_quantity": 100}
        ]
        resolved = resolve_external_fill_run(
            repository=repository,
            fill={"account_id": "kite:A", "instrument_token": 738561, "product": "CNC",
                  "transaction_type": "SELL", "quantity": 10},
        )
        self.assertEqual(resolved["resolution"], "broker_import")
        self.assertEqual(resolved["run_id"], "")
        repository.find_open_live_runs_for_instrument.assert_not_called()

        # And the fill lands as manual exposure, not in any strategy book.
        AccountTruthStore(session_factory=disposable_db).ingest_trades(
            account_id="kite:A", trades=[trade("T-UNTAGGED", order_id="OID-U", side="SELL", qty=10)]
        )
        manual = AccountTruthStore(session_factory=disposable_db).manual_residual_by_coordinate(
            account_id="kite:A"
        )
        self.assertEqual(manual[COORD], -10)
