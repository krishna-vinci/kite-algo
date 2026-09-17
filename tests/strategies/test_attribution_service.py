"""Unit tests for the durable strategy attribution domain, store and service.

The store/service tests run on SQLite with the shared ``Base`` metadata. The
platform tables the attribution store reads through Core ``text()`` SQL are
``public.``-qualified in the codebase, so the fixture attaches a ``public``
schema and creates the run table there — the same pattern as
``tests/api/test_algo_worker_api.py``. The new attribution tables come from
``Base.metadata.create_all`` (unqualified, portable).

Real-PostgreSQL enforcement (composite FKs, immutability trigger, RESTRICT,
concurrency, per-fact instrument identity across mapping eras) is verified by
the separate disposable-database suite.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.repositories.algo_worker_repo import WorkerToken
from backend.api.schemas.worker import WorkerRunCreateRequest
from backend.strategies.attribution import AttributionFold, PositionKey, SqlAttributionStore, TradeFact
from backend.workflows.repository import Base
import backend.strategies.models  # noqa: F401  registers the hosted tables on Base
import backend.strategies.attribution_models  # noqa: F401  registers the attribution tables on Base


def _fact(
    source,
    run,
    qty,
    token=738561,
    product="CNC",
    buy=True,
    effective="2026-09-01T10:00:00+00:00",
    env="live",
    identity=("canonical", "uuid-a"),
):
    kind, key = identity
    return TradeFact(
        source_key=source,
        strategy_run_id=run,
        execution_environment=env,
        instrument_token=token,
        exchange="NSE",
        tradingsymbol="RELIANCE",
        product=product,
        signed_quantity=qty if buy else -qty,
        effective_at=datetime.fromisoformat(effective),
        pinned_generation=None,
    ), PositionKey(env, kind, key, product)


def test_fold_aggregates_across_runs_and_partial_fills():
    facts = [_fact("t:1", "run-sep", 60), _fact("t:2", "run-sep", 40), _fact("t:3", "run-oct", 100)]
    assert list(AttributionFold.fold(facts).values()) == [200]


def test_fold_exit_brings_key_to_absence():
    facts = [_fact("t:1", "run-a", 60), _fact("t:2", "run-a", 40), _fact("t:3", "run-a", 100, buy=False)]
    assert AttributionFold.fold(facts) == {}


def test_fold_products_distinct():
    facts = [_fact("t:1", "run-a", 100, product="CNC"), _fact("t:2", "run-a", 50, product="MIS")]
    assert sorted(AttributionFold.fold(facts).values()) == [50, 100]


def test_fold_deduplicates_identical_source_identity():
    facts = [_fact("t:1", "run-a", 60), _fact("t:1", "run-a", 60)]  # duplicate ingestion
    assert list(AttributionFold.fold(facts).values()) == [60]


def test_paper_and_live_are_separate_books():
    paper, paper_key = _fact("t:1", "run-p", 100, env="paper")
    live, live_key = _fact("t:2", "run-l", 20, env="live")
    positions = AttributionFold.fold([(paper, paper_key), (live, live_key)])
    assert positions == {paper_key: 100, live_key: 20}


def test_paper_buy_cannot_offset_live_sell():
    paper, paper_key = _fact("t:1", "run-p", 100, env="paper")
    live, live_key = _fact("t:2", "run-l", 100, buy=False, env="live")
    positions = AttributionFold.fold([(paper, paper_key), (live, live_key)])
    assert positions == {paper_key: 100, live_key: -100}  # NOT flat


def test_same_unresolved_era_nets_across_dates():
    # Same raw tuple, same catalog era (same generation identity in the era
    # segment): a Monday BUY and a Tuesday SELL net correctly.
    monday, monday_key = _fact("t:1", "run-a", 10, effective="2024-01-08T10:00:00+00:00",
                               identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=gen:gen-7"))
    tuesday, tuesday_key = _fact("t:2", "run-a", -10, effective="2024-01-09T10:00:00+00:00",
                                 identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=gen:gen-7"))
    positions = AttributionFold.fold([(monday, monday_key), (tuesday, tuesday_key)])
    assert positions == {}  # same era -> nets; flat keys disappear


def test_distinct_mapping_eras_of_unresolved_identity_never_merge():
    # Same raw tuple, two DISTINCT mapping eras (different generation identity
    # in the era segment): never merged, never netted.
    old, old_key = _fact("t:1", "run-a", 10, effective="2024-01-08T10:00:00+00:00",
                         identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=interval:gen-1..gen-6"))
    new, new_key = _fact("t:2", "run-a", -10, effective="2026-09-08T10:00:00+00:00",
                         identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=interval:gen-7.."))
    positions = AttributionFold.fold([(old, old_key), (new, new_key)])
    assert positions == {old_key: 10, new_key: -10}  # distinct eras never merge


class StoreTestCase(unittest.TestCase):
    """SQLite fixture: attribution tables from the shared metadata, the run
    table in an attached ``public`` schema (the codebase's Core SQL is
    ``public.``-qualified), with foreign keys enforced so the composite-key
    refusals behave as they do on PostgreSQL."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _attach_public(dbapi_connection, connection_record):
            _ = connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            cursor.execute(
                """
                CREATE TABLE public.algo_worker_runs (
                    strategy_run_id TEXT PRIMARY KEY,
                    token_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    account_scope TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_fields_json TEXT,
                    risk_schema_json TEXT,
                    allowed_actions_json TEXT,
                    runtime_state_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.store = SqlAttributionStore(session_factory=self.factory)
        with self.factory() as session:
            session.execute(text(
                "INSERT INTO strategies (id, owner_id, name, account_scope) VALUES ('stg-1', 'app:o', 'Momentum', 'kite:A')"
            ))
            session.commit()

    @staticmethod
    def _token():
        return WorkerToken(
            token_id="worker-1",
            name="worker",
            account_scope="kite:A",
            allowed_modes=["live", "paper"],
            allowed_actions=[],
            allowed_templates=[],
        )

    @staticmethod
    def _good_payload():
        return WorkerRunCreateRequest(template_id="tmpl", account_scope="kite:A", execution_mode="live")

    @staticmethod
    def _bad_payload():
        # template_id is NOT NULL on the run table, so the run INSERT itself fails.
        return SimpleNamespace(
            template_id=None,
            account_scope="kite:A",
            execution_mode="live",
            summary_fields=[],
            risk_schema=[],
            allowed_actions=[],
            runtime_state={},
            metadata={},
        )


class BindImmutabilityTests(StoreTestCase):
    def test_second_binding_for_same_run_fails(self):
        self.store.bind_run(strategy_run_id="run-1", strategy_id="stg-1", owner_id="app:o",
                            account_id="kite:A", execution_environment="live",
                            bound_by="supervisor", binding_source="hosted_job")
        with self.assertRaises(Exception):
            self.store.bind_run(strategy_run_id="run-1", strategy_id="stg-1", owner_id="app:o",
                                account_id="kite:A", execution_environment="live",
                                bound_by="supervisor", binding_source="hosted_job")

    def test_bound_run_ids_are_environment_scoped(self):
        self.store.bind_run(strategy_run_id="run-1", strategy_id="stg-1", owner_id="app:o",
                            account_id="kite:A", execution_environment="live",
                            bound_by="supervisor", binding_source="hosted_job")
        self.store.bind_run(strategy_run_id="run-2", strategy_id="stg-1", owner_id="app:o",
                            account_id="kite:A", execution_environment="paper",
                            bound_by="supervisor", binding_source="hosted_job")
        self.assertEqual(self.store.bound_run_ids(account_id="kite:A", strategy_id="stg-1",
                                                  execution_environment="live"), {"run-1"})
        self.assertEqual(self.store.bound_run_ids(account_id="kite:A", strategy_id="stg-1",
                                                  execution_environment="paper"), {"run-2"})

    def test_binding_rejects_account_that_disagrees_with_canonical_strategy(self):
        # Database-enforced, not conventional: the composite FK refuses a
        # binding whose owner/account differs from the canonical strategy's.
        with self.assertRaises(Exception):
            self.store.bind_run(strategy_run_id="run-x", strategy_id="stg-1", owner_id="app:o",
                                account_id="kite:OTHER", execution_environment="live",
                                bound_by="supervisor", binding_source="hosted_job")

    def test_create_run_with_binding_is_atomic(self):
        from backend.strategies.attribution import RunBindingInput
        binding = RunBindingInput(strategy_id="stg-1", owner_id="app:o", account_id="kite:A",
                                  execution_environment="live",
                                  bound_by="supervisor", binding_source="hosted_job")
        # Force the run insert to fail (payload violates a required field); then a
        # second variant forces only the BINDING to fail (nonexistent strategy).
        with self.assertRaises(Exception):
            self.store.create_run_with_binding(
                token=self._token(), payload=self._bad_payload(), strategy_run_id="run-x", binding=binding,
            )
        with self.assertRaises(Exception):
            self.store.create_run_with_binding(
                token=self._token(), payload=self._good_payload(), strategy_run_id="run-y",
                binding=RunBindingInput(strategy_id="stg-missing", owner_id="app:o", account_id="kite:A",
                                        execution_environment="live", bound_by="t", binding_source="hosted_job"),
            )
        # NEITHER the run nor the binding may exist after either rollback.
        with self.factory() as session:
            runs = session.execute(text(
                "SELECT COUNT(*) FROM algo_worker_runs WHERE strategy_run_id IN ('run-x','run-y')"
            )).scalar()
            bindings = session.execute(text(
                "SELECT COUNT(*) FROM strategy_run_bindings WHERE strategy_run_id IN ('run-x','run-y')"
            )).scalar()
        self.assertEqual((runs, bindings), (0, 0))

    def test_create_run_with_binding_legacy_path_writes_run_only(self):
        result = self.store.create_run_with_binding(
            token=self._token(), payload=self._good_payload(), strategy_run_id="run-legacy", binding=None,
        )
        self.assertEqual(result["strategy_run_id"], "run-legacy")
        with self.factory() as session:
            runs = session.execute(text(
                "SELECT COUNT(*) FROM algo_worker_runs WHERE strategy_run_id='run-legacy'"
            )).scalar()
            bindings = session.execute(text(
                "SELECT COUNT(*) FROM strategy_run_bindings WHERE strategy_run_id='run-legacy'"
            )).scalar()
        self.assertEqual((runs, bindings), (1, 0))


class RecomputePublishTests(StoreTestCase):
    def _pipeline(self, rows, sha):
        return lambda db, bound: (rows, sha)

    def test_publish_is_versioned_and_idempotent_by_content(self):
        rows = [{
            "identity_kind": "canonical", "identity_key": "uuid-a",
            "canonical_instrument_id": "uuid-a", "instrument_token": 738561,
            "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
            "net_quantity": 100, "unresolved_reason": None,
        }]
        r1 = self.store.recompute_publish(account_id="kite:A", strategy_id="stg-1",
                                          execution_environment="live",
                                          resolve_and_fold=self._pipeline(rows, "a" * 64))
        r2 = self.store.recompute_publish(account_id="kite:A", strategy_id="stg-1",
                                          execution_environment="live",
                                          resolve_and_fold=self._pipeline(rows, "a" * 64))
        self.assertTrue(r2["unchanged"])
        self.assertEqual(r1["projection_version"], r2["projection_version"])

    def test_environment_books_are_separate_publications(self):
        rows = [{
            "identity_kind": "canonical", "identity_key": "uuid-a",
            "canonical_instrument_id": "uuid-a", "instrument_token": 738561,
            "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
            "net_quantity": 100, "unresolved_reason": None,
        }]
        self.store.recompute_publish(account_id="kite:A", strategy_id="stg-1", execution_environment="paper",
                                     resolve_and_fold=self._pipeline(rows, "p" * 64))
        self.store.recompute_publish(account_id="kite:A", strategy_id="stg-1", execution_environment="live",
                                     resolve_and_fold=self._pipeline(rows, "l" * 64))
        with self.factory() as session:
            keys = session.execute(text(
                "SELECT execution_environment, net_quantity FROM strategy_position_projection "
                "WHERE account_id='kite:A' AND strategy_id='stg-1'"
            )).fetchall()
        self.assertEqual(sorted(keys), [("live", 100), ("paper", 100)])

    def test_interrupted_publish_retains_previous_projection(self):
        rows = [{
            "identity_kind": "canonical", "identity_key": "uuid-a",
            "canonical_instrument_id": "uuid-a", "instrument_token": 738561,
            "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
            "net_quantity": 100, "unresolved_reason": None,
        }]
        self.store.recompute_publish(account_id="kite:A", strategy_id="stg-1", execution_environment="live",
                                     resolve_and_fold=self._pipeline(rows, "a" * 64))
        with self.assertRaises(RuntimeError):
            self.store.recompute_publish(account_id="kite:A", strategy_id="stg-1", execution_environment="live",
                                         resolve_and_fold=self._pipeline(
                                             [{**rows[0], "net_quantity": 999}], "c" * 64),
                                         on_before_commit=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with self.factory() as session:
            state = session.execute(text(
                "SELECT projection_version, content_sha256 FROM strategy_projection_state "
                "WHERE account_id='kite:A' AND strategy_id='stg-1' AND execution_environment='live'"
            )).fetchone()
            qty = session.execute(text(
                "SELECT net_quantity FROM strategy_position_projection "
                "WHERE account_id='kite:A' AND strategy_id='stg-1' AND execution_environment='live'"
            )).scalar()
        self.assertEqual((state[0], state[1]), (1, "a" * 64))
        self.assertEqual(qty, 100)

    def test_recompute_publish_uses_the_locked_session_for_all_reads(self):
        # Pins the single supported path: the resolve_and_fold pipeline receives
        # the SAME session recompute_publish opened (post-lock); there is no
        # external-snapshot publication path to call.
        seen_sessions = []
        rows = [{
            "identity_kind": "canonical", "identity_key": "uuid-a",
            "canonical_instrument_id": "uuid-a", "instrument_token": 738561,
            "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
            "net_quantity": 100, "unresolved_reason": None,
        }]

        def pipeline(db, bound):
            seen_sessions.append(db)
            return rows, "a" * 64

        self.store.recompute_publish(account_id="kite:A", strategy_id="stg-1",
                                     execution_environment="live", resolve_and_fold=pipeline)
        self.assertEqual(len(seen_sessions), 1)  # one locked session carried snapshot AND publication
        self.assertFalse(hasattr(self.store, "publish_external_snapshot"))
        self.assertFalse(hasattr(self.store, "compute_source_version"))
