"""CNC portfolios, scheduling and corporate actions on PostgreSQL.

Why PostgreSQL: SQLite cannot prove the occurrence unique index under two racing
schedulers, the composite FKs, the append-only corporate-action log, or the
partial-fill CHECKs. Every test runs against a DISPOSABLE, uniquely named database
created on the test server, upgraded with ``alembic upgrade head`` and dropped
afterwards. No existing database is ever touched.

    CNC_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_cnc_scheduling_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2  # real psycopg2 must be imported BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.strategies.compiler.weights import WeightsPortfolioCompiler  # noqa: E402
from backend.strategies.corporate_actions import CorporateActionDetector  # noqa: E402
from backend.strategies.scheduling import ScheduleScheduler  # noqa: E402

PG_URL = os.environ.get("CNC_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "CNC_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
G1 = "11111111-1111-1111-1111-111111111111"
#: instrument_id is a native UUID column, so instruments need real uuids.
INSTRUMENTS = {
    "RELIANCE": "a0000000-0000-0000-0000-000000000001",
    "INFY": "a0000000-0000-0000-0000-000000000002",
}


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
        dbname = f"kite_cnc_{uuid.uuid4().hex[:12]}"
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


def seed_world(sf, *, strategy_id="stg-A"):
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES (:gen, 'published', '2026-09-01T00:00:00+00:00') ON CONFLICT (id) DO NOTHING",
        {"gen": G1},
    )
    for symbol, lot in (("RELIANCE", 1), ("INFY", 1)):
        _exec(
            sf,
            "INSERT INTO public.instrument_catalog_records "
            "(instrument_id, identity_key, public_key, exchange, tradingsymbol, lifecycle_status, "
            " lot_size, current_generation_id) VALUES "
            "(:iid, :identity, :public, 'NSE', :symbol, 'active', :lot, :gen) "
            "ON CONFLICT (instrument_id) DO NOTHING",
            {
                "iid": INSTRUMENTS[symbol],
                "identity": f"identity-{symbol}",
                "public": f"NSE:{symbol}",
                "symbol": symbol,
                "lot": lot,
                "gen": G1,
            },
        )
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, 'app:owner', :name, 'kite:A', 'active') "
        "ON CONFLICT (id) DO NOTHING",
        {"sid": strategy_id, "name": f"Strategy {strategy_id}"},
    )


def seed_policy(sf, *, allocation=100000.0, **axes):
    params = {"alloc": allocation, "sid": "stg-A"}
    extra_columns = ", ".join(axes) if axes else ""
    extra_values = ", ".join(":" + name for name in axes) if axes else ""
    separator = ", " if axes else ""
    _exec(
        sf,
        "INSERT INTO strategy_admission_policies "
        f"(strategy_id, account_id, allocation_inr, updated_by{separator}{extra_columns}) "
        f"VALUES ('stg-A', 'kite:A', :alloc, 'app:owner'{separator}{extra_values})",
        {**params, **axes},
    )


def seed_book(sf, symbol, quantity, *, environment="paper", strategy_id="stg-A"):
    instrument_id = INSTRUMENTS[symbol]
    _exec(
        sf,
        "INSERT INTO strategy_position_projection "
        "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
        " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
        " net_quantity, projection_version) "
        "VALUES ('kite:A', :sid, :env, 'canonical', :iid, :iid, 'CNC', 100, 'NSE', :symbol, "
        " :qty, 1)",
        {"sid": strategy_id, "env": environment, "iid": instrument_id, "symbol": symbol,
         "qty": int(quantity)},
    )


def leg(symbol, weight, price):
    return {
        "instrument_id": INSTRUMENTS[symbol],
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "broker_exchange": "NSE",
        "broker_symbol": symbol,
        "broker_token": 100,
        "product": "CNC",
        "target_weight": weight,
        "reference_price": price,
    }


def plan(legs):
    return {
        "plan_id": str(uuid.uuid4()),
        "strategy_id": "stg-A",
        "account_id": "kite:A",
        "plan_hash": "h" * 64,
        "pinned_catalog_generation": G1,
        "resolved_plan": {"legs": legs},
    }


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


class TestMigration(_PgTestCase):
    def test_head_and_shape(self):
        sf = self.make_db()
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000031"
        tables = {
            row[0]
            for row in sf().execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN "
                    "('strategy_schedule_occurrences','paper_order_fill_progress',"
                    "'strategy_corporate_action_events','strategy_corporate_action_event_log')"
                )
            ).fetchall()
        }
        assert len(tables) == 4
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM pg_trigger WHERE tgname='trg_strategy_corporate_action_log_immutable'",
        ) == 1

    def test_schedule_vocabulary_admits_the_new_kinds(self):
        sf = self.make_db()
        definition = _scalar(
            sf,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_hosted_strategy_schedules_kind'",
        )
        for kind in ("monthly", "calendar", "daily", "weekly"):
            assert kind in definition, definition
        columns = {
            row[0]
            for row in sf().execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='hosted_strategy_schedules' "
                    "AND column_name IN ('day_of_month','calendar_dates')"
                )
            ).fetchall()
        }
        assert len(columns) == 2

    def test_upgrade_from_prior_head_is_additive(self):
        sf = self.make_db("20260917_000030")
        db_url = _url_for(self._created[-1])
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope) "
            "VALUES ('stg-prior', 'app:o', 'prior', 'kite:A')",
        )
        _upgrade(db_url, "head")
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000031"
        assert _scalar(sf, "SELECT name FROM public.strategies WHERE id='stg-prior'") == "prior"
        for table in (
            "strategy_schedule_occurrences",
            "paper_order_fill_progress",
            "strategy_corporate_action_events",
            "strategy_corporate_action_event_log",
        ):
            assert _scalar(sf, f"SELECT COUNT(*) FROM public.{table}") == 0


# ---------------------------------------------------------------------------
# the acceptance walkthrough: month-over-month continuity
# ---------------------------------------------------------------------------


class TestWalkthrough(_PgTestCase):
    def test_september_holdings_are_octobers_current_book(self):
        """THE acceptance criterion: October trades only the delta.

        September's plan bought 100 RELIANCE and it filled. October's plan targets
        the same weight, so the book it compiles against already holds the 100 and
        the only trade is whatever the target moved by.
        """
        sf = self.make_db()
        seed_world(sf)
        seed_policy(sf, allocation=100000.0)

        september = plan([leg("RELIANCE", 0.5, 100.0)])
        first = WeightsPortfolioCompiler(session_factory=sf).compile(
            september, execution_environment="paper"
        )
        assert not first.refused, first.refusal_detail
        assert first.legs[0]["delta"] == 500  # 0.5 x 100_000 / 100

        # September fills: the attributed paper book now holds the 100.
        seed_book(sf, "RELIANCE", 100, environment="paper")

        # October the target moved to 0.6, and the compiler sees the 100 as current.
        october = plan([leg("RELIANCE", 0.6, 100.0)])
        second = WeightsPortfolioCompiler(session_factory=sf).compile(
            october, execution_environment="paper"
        )
        assert not second.refused, second.refusal_detail
        assert second.legs[0]["current_quantity"] == 100
        assert second.legs[0]["target_quantity"] == 600
        assert second.legs[0]["delta"] == 500  # only the delta trades

    def test_an_unchanged_target_trades_nothing(self):
        sf = self.make_db()
        seed_world(sf)
        seed_policy(sf, allocation=100000.0)
        seed_book(sf, "RELIANCE", 500, environment="paper")
        compilation = WeightsPortfolioCompiler(session_factory=sf).compile(
            plan([leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        # Target already met: no legs at all, and that is success.
        assert compilation.legs == []
        assert not compilation.refused

    def test_sells_precede_buys_and_the_gross_reservation_covers_every_buy(self):
        sf = self.make_db()
        seed_world(sf)
        seed_policy(sf, allocation=100000.0)
        seed_book(sf, "RELIANCE", 500, environment="paper")
        compilation = WeightsPortfolioCompiler(session_factory=sf).compile(
            plan([leg("INFY", 0.5, 100.0), leg("RELIANCE", 0.0, 100.0)]),
            execution_environment="paper",
        )
        assert [row["side"] for row in compilation.legs] == ["SELL", "BUY"]
        # 500 INFY buy = 50_000, reserved upfront; the 50_000 of RELIANCE proceeds
        # is NOT counted toward it.
        assert compilation.gross_cash_reservation_inr == 50000.0

    def test_an_unaffordable_bundle_is_refused_before_any_order(self):
        sf = self.make_db()
        seed_world(sf)
        seed_policy(sf, allocation=1000.0)
        compilation = WeightsPortfolioCompiler(session_factory=sf).compile(
            plan([leg("RELIANCE", 0.75, 100.0), leg("INFY", 0.75, 100.0)]),
            execution_environment="paper",
        )
        assert compilation.refused
        assert compilation.refusal_reason == "GROSS_NOTIONAL_EXCEEDED"

    def test_out_of_scope_instruments_are_untouched(self):
        sf = self.make_db()
        seed_world(sf)
        seed_policy(sf, allocation=100000.0)
        seed_book(sf, "INFY", 75, environment="paper")
        compilation = WeightsPortfolioCompiler(session_factory=sf).compile(
            plan([leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        # INFY is held but is not a member of this plan's scope, so no row is
        # invented for it and it is not sold.
        assert [row["tradingsymbol"] for row in compilation.legs] == ["RELIANCE"]


# ---------------------------------------------------------------------------
# scheduling under concurrency
# ---------------------------------------------------------------------------


class TestScheduling(_PgTestCase):
    def _hosted(self, sf, hosted_id):
        seed_world(sf, strategy_id=hosted_id)
        _exec(
            sf,
            "INSERT INTO public.hosted_strategies "
            "(id, owner_id, name, template_id, default_execution_mode, default_account_scope, "
            " default_job_kind, stale_exit_policy, max_duration_s, progress_deadline_s, status) "
            "VALUES (:id, 'app:owner', :name, :template, 'paper', 'kite:A', 'finite', "
            " 'exit_on_worker_stale', 3600, 600, 'active')",
            {"id": hosted_id, "name": f"A {hosted_id}", "template": f"hosted:{hosted_id}"},
        )
        _exec(
            sf,
            "INSERT INTO public.hosted_strategy_versions "
            "(id, strategy_id, version, source, source_sha256, parameters_schema, "
            " capabilities_snapshot, created_by) "
            "VALUES (:vid, :sid, 1, 'inline', 'sha', '{}', '{}', 'app:owner')",
            {"vid": f"v-{hosted_id}", "sid": hosted_id},
        )

    def _schedule(self, sf):
        seed_world(sf, strategy_id="hs-1")
        _exec(
            sf,
            "INSERT INTO public.hosted_strategies "
            "(id, owner_id, name, template_id, default_execution_mode, default_account_scope, "
            " default_job_kind, stale_exit_policy, max_duration_s, progress_deadline_s, status) "
            "VALUES ('hs-1', 'app:owner', 'A', 'hosted:hs-1', 'paper', 'kite:A', 'finite', "
            " 'exit_on_worker_stale', 3600, 600, 'active')",
        )
        _exec(
            sf,
            "INSERT INTO public.hosted_strategy_versions "
            "(id, strategy_id, version, source, source_sha256, parameters_schema, "
            " capabilities_snapshot, created_by) VALUES ('v-1','hs-1',1,'inline','sha','{}','{}','app:o')",
        )
        _exec(
            sf,
            "INSERT INTO public.hosted_strategy_schedules "
            "(id, strategy_id, version_id, owner_id, account_scope, execution_mode, job_kind, "
            " max_duration_s, progress_deadline_s, schedule_kind, at_time, timezone, "
            " calendar_dates, enabled, params_snapshot, policy_snapshot, capabilities_snapshot) "
            "VALUES ('sch-1', 'hs-1', 'v-1', 'app:owner', 'kite:A', 'paper', 'finite', 3600, 600, "
            " 'calendar', '09:30', 'Asia/Kolkata', '[\"2026-10-10\"]'::jsonb, TRUE, '{}', '{}', '{}')",
        )

    def test_a_monthly_schedule_is_admitted_by_the_widened_check(self):
        sf = self.make_db()
        self._schedule(sf)
        self._hosted(sf, "hs-m")
        _exec(
            sf,
            "INSERT INTO public.hosted_strategy_schedules "
            "(id, strategy_id, version_id, owner_id, account_scope, execution_mode, job_kind, "
            " max_duration_s, progress_deadline_s, schedule_kind, at_time, timezone, "
            " day_of_month, enabled, params_snapshot, policy_snapshot, capabilities_snapshot) "
            "VALUES ('sch-m', 'hs-m', 'v-hs-m', 'app:owner', 'kite:A', 'paper', 'finite', 3600, 600, "
            " 'monthly', '09:30', 'Asia/Kolkata', 15, FALSE, '{}', '{}', '{}')",
        )
        assert _scalar(
            sf, "SELECT day_of_month FROM public.hosted_strategy_schedules WHERE id='sch-m'"
        ) == 15

    def test_a_monthly_schedule_without_its_day_is_refused(self):
        sf = self.make_db()
        self._schedule(sf)
        self._hosted(sf, "hs-bad")
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.hosted_strategy_schedules "
                "(id, strategy_id, version_id, owner_id, account_scope, execution_mode, job_kind, "
                " max_duration_s, progress_deadline_s, schedule_kind, at_time, timezone, enabled, "
                " params_snapshot, policy_snapshot, capabilities_snapshot) "
                "VALUES ('sch-bad', 'hs-bad', 'v-hs-bad', 'app:owner', 'kite:A', 'paper', 'finite', 3600, "
                " 600, 'monthly', '09:30', 'Asia/Kolkata', FALSE, '{}', '{}', '{}')",
            )

    def test_two_schedulers_racing_one_occurrence_fire_it_exactly_once(self):
        """The unique index IS the fencing: the loser cannot create a second row."""
        sf = self.make_db()
        self._schedule(sf)
        fired: list = []
        lock = threading.Lock()

        def submitter(schedule, occurrence, detail):
            with lock:
                fired.append(occurrence.occurrence_key)
            return True

        outcomes: list = []
        barrier = threading.Barrier(2)
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = str(30 * 24 * 3600)

        def race():
            scheduler = ScheduleScheduler(session_factory=sf, proposal_submitter=submitter)
            try:
                barrier.wait(timeout=30)
                outcomes.append(scheduler.tick(now=NOW))
            except Exception as exc:  # noqa: BLE001
                outcomes.append({"error": repr(exc)})

        threads = [threading.Thread(target=race) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)
        errors = [item for item in outcomes if "error" in item]
        assert not errors, errors
        assert _scalar(
            sf, "SELECT COUNT(*) FROM public.strategy_schedule_occurrences"
        ) == 1, outcomes
        # Exactly one fire across both schedulers.
        assert len(fired) == 1, (fired, outcomes)

    def test_occurrences_are_unique_per_schedule(self):
        sf = self.make_db()
        self._schedule(sf)
        _exec(
            sf,
            "INSERT INTO public.strategy_schedule_occurrences "
            "(id, schedule_id, strategy_id, occurrence_key, due_at, status) "
            "VALUES (gen_random_uuid(), 'sch-1', 'stg-A', 'sch-1:2026-10-10', NOW(), 'pending')",
        )
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.strategy_schedule_occurrences "
                "(id, schedule_id, strategy_id, occurrence_key, due_at, status) "
                "VALUES (gen_random_uuid(), 'sch-1', 'stg-A', 'sch-1:2026-10-10', NOW(), 'fired')",
            )

    def test_a_misfire_beyond_grace_is_skipped_with_its_reason(self):
        sf = self.make_db()
        self._schedule(sf)
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = "60"
        try:
            scheduler = ScheduleScheduler(session_factory=sf, proposal_submitter=lambda *_: True)
            result = scheduler.tick(now=NOW)
            assert result["fired"] == []
            row = sf().execute(
                text(
                    "SELECT status, skip_reason FROM public.strategy_schedule_occurrences"
                )
            ).fetchone()
            assert row[0] == "skipped"
            assert row[1] == "misfire_beyond_grace"
        finally:
            os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)


# ---------------------------------------------------------------------------
# corporate actions
# ---------------------------------------------------------------------------


class TestCorporateActions(_PgTestCase):
    def test_detection_freezes_escalates_and_never_rebases(self):
        sf = self.make_db()
        seed_world(sf)
        _exec(
            sf,
            "INSERT INTO public.account_positions "
            "(account_id, instrument_token, exchange, tradingsymbol, product, net_quantity) "
            "VALUES ('kite:A', 100, 'NSE', 'RELIANCE', 'CNC', 200)",
        )
        seed_book(sf, "RELIANCE", 100, environment="live")

        notified: list = []

        def notifier(account_id, event):
            notified.append((account_id, event["action_kind"]))
            return True

        detector = CorporateActionDetector(session_factory=sf, notifier=notifier)
        result = detector.detect(
            account_id="kite:A",
            coordinate={"instrument_token": 100, "exchange": "NSE",
                        "tradingsymbol": "RELIANCE", "product": "CNC"},
            broker_quantity=200,
            attributed_quantity=100,
            manual_quantity=0,
        )
        assert result["action_kind"] == "suspected_split"
        assert result["frozen"] is True
        assert result["escalated"] is True
        assert notified == [("kite:A", "suspected_split")]

        # Frozen through the Phase 2 machinery, so new exposure is refused.
        from backend.strategies.account_truth import AccountTruthStore

        store = AccountTruthStore(session_factory=sf)
        assert store.is_frozen_coordinate(
            account_id="kite:A", coordinate=(100, "NSE", "RELIANCE", "CNC")
        ) == "unexplained"

        # Nothing about the book was rewritten: no automatic rebasing.
        assert _scalar(
            sf, "SELECT net_quantity FROM strategy_position_projection"
        ) == 100

        # And the log records the sequence.
        events = [row["event"] for row in detector.log_for(result["id"])]
        assert events == ["detected", "freeze_confirmed", "escalated"]

    def test_the_log_is_append_only(self):
        sf = self.make_db()
        seed_world(sf)
        detector = CorporateActionDetector(session_factory=sf)
        result = detector.detect(
            account_id="kite:A",
            coordinate={"instrument_token": 100, "exchange": "NSE",
                        "tradingsymbol": "RELIANCE", "product": "CNC"},
            broker_quantity=200,
            attributed_quantity=100,
            manual_quantity=0,
        )
        for statement in (
            "UPDATE public.strategy_corporate_action_event_log SET event='resolved'",
            "DELETE FROM public.strategy_corporate_action_event_log",
        ):
            with pytest.raises(Exception) as exc:
                _exec(sf, statement)
            assert "append-only" in str(exc.value).lower()
        assert result["id"]

    def test_resolution_requires_a_named_adjustment(self):
        sf = self.make_db()
        seed_world(sf)
        detector = CorporateActionDetector(session_factory=sf)
        result = detector.detect(
            account_id="kite:A",
            coordinate={"instrument_token": 100, "exchange": "NSE",
                        "tradingsymbol": "RELIANCE", "product": "CNC"},
            broker_quantity=200, attributed_quantity=100, manual_quantity=0,
        )
        resolved = detector.resolve(
            result["id"], adjustment_id=str(uuid.uuid4()), actor_id="app:owner"
        )
        assert resolved["status"] == "resolved"
        assert _scalar(
            sf, "SELECT resolved_adjustment_id IS NOT NULL FROM public.strategy_corporate_action_events"
        ) is True

    def test_an_offsetting_gap_is_not_a_corporate_action(self):
        sf = self.make_db()
        seed_world(sf)
        detector = CorporateActionDetector(session_factory=sf)
        assert detector.detect(
            account_id="kite:A",
            coordinate={"instrument_token": 100, "exchange": "NSE",
                        "tradingsymbol": "RELIANCE", "product": "CNC"},
            broker_quantity=200, attributed_quantity=100, manual_quantity=0,
            offsetting_trade_quantity=100,
        ) is None
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_corporate_action_events") == 0


if __name__ == "__main__":
    unittest.main()
