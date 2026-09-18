"""MIS intraday policy, square-off evidence and stale exits on PostgreSQL.

Why PostgreSQL: SQLite cannot prove the append-only trigger, the outcome
vocabulary, or the product-separated books that make walkthrough 2 meaningful.
Every test runs against a DISPOSABLE, uniquely named database created on the test
server, upgraded with ``alembic upgrade head`` and dropped afterwards. No existing
database is ever touched.

    MIS_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_mis_paper_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg2  # real psycopg2 must be imported BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.strategies.mis_squareoff import (  # noqa: E402
    MisSquareoffEvidenceStore,
    SquareoffRecord,
    attributed_exit_size,
    scheduled_time_for,
)

PG_URL = os.environ.get("MIS_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "MIS_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc)
SESSION = date(2026, 10, 15)
G1 = "11111111-1111-1111-1111-111111111111"


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
        dbname = f"kite_mis_{uuid.uuid4().hex[:12]}"
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


def seed_world(sf):
    """Two strategies on one account, one catalogued equity."""
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES (:gen, 'published', '2026-09-01T00:00:00+00:00') ON CONFLICT (id) DO NOTHING",
        {"gen": G1},
    )
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_records "
        "(instrument_id, identity_key, public_key, exchange, tradingsymbol, lifecycle_status, "
        " lot_size, instrument_type, current_generation_id) VALUES "
        "('a0000000-0000-0000-0000-000000000001', 'identity-rel', 'NSE:RELIANCE', 'NSE', "
        " 'RELIANCE', 'active', 1, 'EQ', :gen) ON CONFLICT (instrument_id) DO NOTHING",
        {"gen": G1},
    )
    _exec(
        sf,
        "INSERT INTO public.instrument_broker_mappings "
        "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
        " valid_from_generation, is_current) VALUES "
        "('a0000000-0000-0000-0000-000000000001', 'kite', 'NSE', 'RELIANCE', 100, :gen, TRUE) "
        "ON CONFLICT DO NOTHING",
        {"gen": G1},
    )
    for sid, name in (("stg-A", "Momentum A"), ("stg-B", "Mean reversion B")):
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
            "VALUES (:sid, 'app:owner', :name, 'kite:A', 'active') ON CONFLICT (id) DO NOTHING",
            {"sid": sid, "name": name},
        )


def seed_book(sf, strategy_id, product, quantity, *, environment="paper"):
    _exec(
        sf,
        "INSERT INTO strategy_position_projection "
        "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
        " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
        " net_quantity, projection_version) "
        "VALUES ('kite:A', :sid, :env, 'canonical', :key, 'a0000000-0000-0000-0000-000000000001', "
        " :product, 100, 'NSE', 'RELIANCE', :qty, 1)",
        {
            "sid": strategy_id, "env": environment, "product": product,
            "qty": int(quantity), "key": f"{strategy_id}-{product}",
        },
    )


def record(sf, *, strategy_id="stg-B", run_id="run-B", outcome="squared_off", product="MIS",
           exchange="NSE", claim=None, detail=None):
    return MisSquareoffEvidenceStore(session_factory=sf).record(
        SquareoffRecord(
            account_id="kite:A", strategy_id=strategy_id, strategy_run_id=run_id,
            product=product, session_date=SESSION, exchange=exchange, scheduled_at=NOW,
            outcome=outcome, exit_claim_id=claim, detail=detail or {},
        )
    )


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


class TestMigration(_PgTestCase):
    def test_head_and_shape(self):
        sf = self.make_db()
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000032"
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public' "
            "AND tablename='strategy_squareoff_evidence'",
        ) == 1
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM pg_trigger "
            "WHERE tgname='trg_strategy_squareoff_evidence_immutable'",
        ) == 1
        definition = _scalar(
            sf,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_sse_outcome'",
        )
        for outcome in ("squared_off", "action_required", "missed_by_broker", "stale_worker_exit"):
            assert outcome in definition, definition

    def test_upgrade_from_prior_head_is_additive(self):
        sf = self.make_db("20260917_000031")
        db_url = _url_for(self._created[-1])
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope) "
            "VALUES ('stg-prior', 'app:o', 'prior', 'kite:A')",
        )
        _upgrade(db_url, "head")
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000032"
        assert _scalar(sf, "SELECT name FROM public.strategies WHERE id='stg-prior'") == "prior"
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_squareoff_evidence") == 0

    def test_evidence_is_append_only(self):
        sf = self.make_db()
        seed_world(sf)
        record(sf)
        for statement in (
            "UPDATE public.strategy_squareoff_evidence SET outcome='action_required'",
            "DELETE FROM public.strategy_squareoff_evidence",
        ):
            with pytest.raises(Exception) as exc:
                _exec(sf, statement)
            assert "append-only" in str(exc.value).lower()

    def test_an_out_of_vocabulary_outcome_is_refused(self):
        sf = self.make_db()
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_squareoff_evidence "
                "(id, account_id, strategy_id, strategy_run_id, product, session_date, exchange, "
                " scheduled_at, outcome) VALUES (gen_random_uuid(), 'kite:A', 'stg-B', 'run-B', "
                " 'MIS', CURRENT_DATE, 'NSE', NOW(), 'invented')",
            )


# ---------------------------------------------------------------------------
# walkthrough 2 — the acceptance test
# ---------------------------------------------------------------------------


class TestWalkthroughTwo(_PgTestCase):
    def test_b_square_off_cannot_sell_as_shares(self):
        """Momentum A holds RELIANCE CNC +100; mean-reversion B holds RELIANCE MIS +40."""
        sf = self.make_db()
        seed_world(sf)
        seed_book(sf, "stg-A", "CNC", 100)
        seed_book(sf, "stg-B", "MIS", 40)

        # Attribution is separate by product, so each book is its own quantity.
        a_attributed = _scalar(
            sf,
            "SELECT net_quantity FROM strategy_position_projection "
            "WHERE strategy_id='stg-A' AND product='CNC'",
        )
        b_attributed = _scalar(
            sf,
            "SELECT net_quantity FROM strategy_position_projection "
            "WHERE strategy_id='stg-B' AND product='MIS'",
        )
        assert (a_attributed, b_attributed) == (100, 40)

        # B's square-off asks for far more than B owns, and is clamped to B's book.
        b_exit = attributed_exit_size(
            attributed_quantity=b_attributed, requested_quantity=-b_attributed * 10
        )
        assert b_exit == -40

        # A's book is untouched: nothing in the exit path reads or writes it.
        assert _scalar(
            sf,
            "SELECT net_quantity FROM strategy_position_projection "
            "WHERE strategy_id='stg-A' AND product='CNC'",
        ) == 100
        assert _scalar(sf, "SELECT COUNT(*) FROM strategy_position_projection") == 2

    def test_the_evidence_separates_the_two_strategies(self):
        sf = self.make_db()
        seed_world(sf)
        record(sf, strategy_id="stg-B", run_id="run-B", outcome="squared_off")
        record(sf, strategy_id="stg-A", run_id="run-A", product="CNC", outcome="squared_off")
        store = MisSquareoffEvidenceStore(session_factory=sf)
        assert len(store.for_run(strategy_run_id="run-B")) == 1
        assert store.for_run(strategy_run_id="run-B")[0]["product"] == "MIS"
        assert len(store.for_strategy(strategy_id="stg-A")) == 1

    def test_a_failed_square_off_is_action_required_and_keeps_reconciling(self):
        sf = self.make_db()
        seed_world(sf)
        record(sf, outcome="action_required", detail={"reason": "exit rejected"})
        store = MisSquareoffEvidenceStore(session_factory=sf)
        # NOT settlement: the failure is recorded and stays unresolved.
        assert [row["outcome"] for row in store.unresolved_for_run(strategy_run_id="run-B")] == [
            "action_required"
        ]
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM public.strategy_squareoff_evidence WHERE outcome='squared_off'",
        ) == 0

    def test_a_broker_fallback_is_evidence_of_a_missed_square_off(self):
        sf = self.make_db()
        seed_world(sf)
        record(sf, outcome="missed_by_broker", detail={"observed_at": "15:35"})
        store = MisSquareoffEvidenceStore(session_factory=sf)
        # A fallback firing is itself proof the platform's square-off did not.
        assert [row["outcome"] for row in store.unresolved_for_run(strategy_run_id="run-B")] == [
            "missed_by_broker"
        ]


# ---------------------------------------------------------------------------
# scheduling and policy
# ---------------------------------------------------------------------------


class TestSchedulesAndPolicy(_PgTestCase):
    def test_per_product_schedules_and_the_override(self):
        sf = self.make_db()
        seed_world(sf)
        assert scheduled_time_for("NSE") == "15:20"
        assert scheduled_time_for("BSE") == "15:20"
        assert scheduled_time_for("NFO") == "15:25"
        assert scheduled_time_for("CDS") == "16:45"
        assert scheduled_time_for("MCX") == "23:20"
        os.environ["WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON"] = '{"NSE:MIS": "14:00"}'
        try:
            assert scheduled_time_for("NSE") == "14:00"
            assert scheduled_time_for("MCX") == "23:20"
        finally:
            os.environ.pop("WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON", None)

    def test_a_multi_day_mis_plan_is_refused_end_to_end(self):
        """Through the real store: the refusal spends the evaluation."""
        from backend.strategies.proposals import (
            ProposalConflict,
            ProposalStore,
            ProposalSubmission,
        )

        sf = self.make_db()
        seed_world(sf)
        store = ProposalStore(session_factory=sf)

        def submission(evaluation_id, hold_days, product="MIS"):
            return ProposalSubmission(
                strategy_id="stg-B", account_id="kite:A", evaluation_id=evaluation_id,
                evaluation_kind="run_now", strategy_run_id="run-B",
                target_kind="single_instrument",
                payload={
                    "instrument_token": 100, "exchange": "NSE", "tradingsymbol": "RELIANCE",
                    "product": product, "target_quantity": 10, "reference_price": 100.0,
                    "hold_days": hold_days,
                },
            )

        refused = store.submit(submission("eval-mis", 3))
        assert refused["status"] == "refused"
        assert refused["plan"] is None
        assert _scalar(
            sf,
            "SELECT reason_code FROM public.strategy_proposal_journal "
            "WHERE event='validation_refused'",
        ) == "MIS_OVERNIGHT_REFUSED"

        # The identity is spent: the corrected intent needs a new evaluation_id.
        with pytest.raises(ProposalConflict):
            store.submit(submission("eval-mis", 3, product="CNC"))
        corrected = store.submit(submission("eval-mis-2", 30, product="CNC"))
        assert corrected["status"] == "validated"

    def test_a_same_session_mis_plan_validates(self):
        from backend.strategies.proposals import ProposalStore, ProposalSubmission

        sf = self.make_db()
        seed_world(sf)
        result = ProposalStore(session_factory=sf).submit(
            ProposalSubmission(
                strategy_id="stg-B", account_id="kite:A", evaluation_id="eval-ok",
                evaluation_kind="run_now", strategy_run_id="run-B",
                target_kind="single_instrument",
                payload={
                    "instrument_token": 100, "exchange": "NSE", "tradingsymbol": "RELIANCE",
                    "product": "MIS", "target_quantity": 10, "reference_price": 100.0,
                },
            )
        )
        assert result["status"] == "validated", result
        assert result["plan"]["resolved_plan"]["legs"][0]["product"] == "MIS"


# ---------------------------------------------------------------------------
# stale exit and tick certification
# ---------------------------------------------------------------------------


class TestStaleExit(_PgTestCase):
    def test_a_stale_mis_worker_is_exited_through_the_claim_path(self):
        import asyncio

        from backend.strategies.mis_stale_exit import MisStaleExitPolicy

        sf = self.make_db()
        seed_world(sf)
        claims: list = []

        async def submitter(run, leg, quantity):
            claims.append((run.get("strategy_run_id"), leg.get("tradingsymbol"), quantity))
            return "claim-1"

        policy = MisStaleExitPolicy(session_factory=sf, claim_submitter=submitter)
        outcome = asyncio.run(
            policy.apply(
                {
                    "strategy_run_id": "run-B",
                    "account_scope": "kite:A",
                    "last_heartbeat_at": (NOW - timedelta(minutes=30)).isoformat(),
                    "runtime_state": {
                        "backend_protection": {
                            "enabled": True,
                            "operations": {"exit_on_worker_stale": True, "worker_stale_sec": 300},
                        }
                    },
                },
                positions=[
                    {"strategy_id": "stg-B", "tradingsymbol": "RELIANCE", "product": "MIS",
                     "exchange": "NSE", "attributed_quantity": 40},
                    {"strategy_id": "stg-A", "tradingsymbol": "RELIANCE", "product": "CNC",
                     "exchange": "NSE", "attributed_quantity": 100},
                ],
                session_date=SESSION,
                now=NOW,
            )
        )
        assert outcome.acted
        assert claims == [("run-B", "RELIANCE", -40)]
        rows = policy.evidence.for_run(strategy_run_id="run-B")
        assert rows[0]["outcome"] == "stale_worker_exit"
        assert rows[0]["exit_claim_id"] == "claim-1"
        assert rows[0]["detail"]["attributed_quantity"] == 40


class TestTickCertification(_PgTestCase):
    def test_a_partial_rebalance_converges_across_ticks(self):
        import asyncio

        from backend.paper_runtime.partial_fills import (
            PaperFillProgressStore,
            next_tranche,
            partial_fill_ratio,
        )
        from backend.paper_runtime.tick_driver import SyntheticTickDriver

        sf = self.make_db()
        os.environ["PAPER_PARTIAL_FILL_RATIO"] = "0.5"
        try:
            store = PaperFillProgressStore(session_factory=sf)
            store.start(account_scope="kite:paper", paper_order_id="PAPER-1", quantity=100)

            class _Service:
                async def process_tick(self, tick):
                    progress = store.progress_for(
                        account_scope="kite:paper", paper_order_id="PAPER-1"
                    )
                    if progress is None or progress.is_complete:
                        return
                    store.record_fill(
                        account_scope="kite:paper", paper_order_id="PAPER-1",
                        filled_quantity=next_tranche(
                            progress.remaining_quantity, partial_fill_ratio()
                        ),
                        quantity=100,
                    )

            driver = SyntheticTickDriver(_Service(), fill_progress_store=store)
            run = asyncio.run(
                driver.run(
                    instrument_token=100,
                    prices=driver.price_series(start=100.0, count=12),
                    paper_order_ids=["PAPER-1"],
                )
            )
            assert run.monotonic, run.filled_per_tick
            assert run.final_filled == 100
            # Quiescent only once nothing is outstanding.
            assert not driver.outstanding(account_scope="kite:paper", paper_order_ids=["PAPER-1"])
            assert _scalar(
                sf, "SELECT remaining_quantity FROM public.paper_order_fill_progress"
            ) == 0
        finally:
            os.environ.pop("PAPER_PARTIAL_FILL_RATIO", None)


if __name__ == "__main__":
    unittest.main()
