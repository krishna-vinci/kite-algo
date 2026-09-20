"""Option structures, fill gating, expiry and settled on PostgreSQL.

Why PostgreSQL: SQLite cannot prove the append-only settlement-evidence trigger, the
evidence-source CHECK that makes "the position disappeared" structurally unusable, or
the expiry-policy CHECK on the option-run table. Every test runs against a
DISPOSABLE, uniquely named database created on the test server, upgraded with
``alembic upgrade head`` and dropped afterwards. No existing database is ever
touched.

    OPTIONS_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_options_structures_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import os
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

from backend.options.protection.exit_builder import build_structure_exit_orders  # noqa: E402
from backend.options.protection.expiry_policy import (  # noqa: E402
    OptionExpiryPolicy,
    OptionSettlementService,
    SettlementRefusal,
    option_settlement_axes,
)
from backend.options.protection.hedge_gate import hedge_fill_gate  # noqa: E402

PG_URL = os.environ.get("OPTIONS_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "OPTIONS_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc)
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
        self.notified: list = []

    def tearDown(self):
        for dbname in self._created:
            _drop_database(dbname)
        self._created = []

    def make_db(self, revision: str = "head"):
        dbname = f"kite_opt_{uuid.uuid4().hex[:12]}"
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

    def settlement(self, sf):
        return OptionSettlementService(session_factory=sf)


def _scalar(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).scalar()


def _exec(sf, sql, params=None):
    with sf() as session:
        session.execute(text(sql), params or {})
        session.commit()


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


class TestMigration(_PgTestCase):
    def test_head_and_shape(self):
        sf = self.make_db()
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000034"
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public' "
            "AND tablename='option_settlement_evidence'",
        ) == 1
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM pg_trigger "
            "WHERE tgname='trg_option_settlement_evidence_immutable'",
        ) == 1
        columns = {
            row[0]
            for row in sf().execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='option_run_states' "
                    "AND column_name IN ('structure_digest','expiry_policy')"
                )
            ).fetchall()
        }
        assert len(columns) == 2

    def test_upgrade_from_prior_head_is_additive(self):
        sf = self.make_db("20260917_000033")
        db_url = _url_for(self._created[-1])
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope) "
            "VALUES ('stg-prior', 'app:o', 'prior', 'kite:A')",
        )
        _upgrade(db_url, "head")
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000034"
        assert _scalar(sf, "SELECT name FROM public.strategies WHERE id='stg-prior'") == "prior"
        assert _scalar(sf, "SELECT COUNT(*) FROM public.option_settlement_evidence") == 0

    def test_evidence_is_append_only(self):
        sf = self.make_db()
        self.settlement(sf).settle(
            account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
            evidence_source="contract_note", recorded_by="app:owner",
        )
        for statement in (
            "UPDATE public.option_settlement_evidence SET settlement_kind='physical'",
            "DELETE FROM public.option_settlement_evidence",
        ):
            with pytest.raises(Exception) as exc:
                _exec(sf, statement)
            assert "append-only" in str(exc.value).lower()

    def test_a_non_authoritative_source_is_refused_by_the_database(self):
        """'The position disappeared' is not an authoritative source, structurally."""
        sf = self.make_db()
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.option_settlement_evidence "
                "(id, account_id, option_run_id, structure_digest, settlement_kind, "
                " evidence_source, evidence_ref, recorded_by) "
                "VALUES (gen_random_uuid(), 'kite:A', 'run-1', 'd-1', 'cash', "
                " 'position_disappeared', '{}'::jsonb, 'app:owner')",
            )

    def test_an_invented_expiry_policy_is_refused_by_the_database(self):
        sf = self.make_db()
        with pytest.raises(Exception):
            _exec(
                sf,
                "INSERT INTO public.option_run_states "
                "(strategy_run_id, strategy_name, product, status, expiry_policy) "
                "VALUES ('run-9', 'S', 'NRML', 'entered', 'make_it_up')",
            )

    def test_a_settled_run_state_is_admitted(self):
        """There is no CHECK to widen (verified in the migration's docstring)."""
        sf = self.make_db()
        _exec(
            sf,
            "INSERT INTO public.option_run_states "
            "(strategy_run_id, strategy_name, product, status, structure_digest, expiry_policy) "
            "VALUES ('run-s', 'S', 'NRML', 'settled', 'd-1', 'allow_cash_settlement')",
        )
        assert _scalar(
            sf, "SELECT status FROM public.option_run_states WHERE strategy_run_id='run-s'"
        ) == "settled"


# ---------------------------------------------------------------------------
# writeups 4-6
# ---------------------------------------------------------------------------


class TestWalkthroughFour(_PgTestCase):
    """Intentional naked admission on paper: an admitted SHAPE, not an accident."""

    def test_an_intentional_naked_structure_is_admitted(self):
        sf = self.make_db()
        # A naked short has no hedge, so there is no hedge leg to gate on and
        # nothing to release: the structure is admitted through the same gates as
        # any other, and it is the OWNER's declared intent rather than a failure.
        orders, detail = build_structure_exit_orders(
            [{"tradingsymbol": "NIFTY26OCT25000CE", "side": "SELL", "quantity": -75,
              "exchange": "NFO", "product": "NRML"}],
            closed_short_quantities={},
        )
        assert len(orders) == 1
        assert orders[0]["transaction_type"] == "BUY"
        assert detail["released_hedges"] == 0
        # Naked is visible in the detail rather than hidden: the exposure is known.
        assert detail["naked_short_quantity"] == 75

    def test_the_gate_does_not_block_a_structure_that_has_no_hedge(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=0, confirmed_filled_quantity=0,
            dependent_short_quantity=75,
        )
        # Nothing to wait for, so nothing to block: an intentional naked short is
        # admitted, not parked at action_required.
        assert decision.released_quantity == 0
        assert decision.action_required is False
        assert decision.reason == "nothing_to_release"


class TestWalkthroughFive(_PgTestCase):
    """Iron condor: 50% hedge fill plus a rejected leg."""

    def test_a_partial_hedge_releases_only_its_proportional_share(self):
        sf = self.make_db()
        legs = [
            {"tradingsymbol": "SHORT-CE", "side": "SELL", "quantity": -75,
             "exchange": "NFO", "product": "NRML", "underlying": "NIFTY",
             "expiry": "2026-10-29"},
            {"tradingsymbol": "HEDGE-CE", "side": "BUY", "quantity": 75,
             "exchange": "NFO", "product": "NRML", "underlying": "NIFTY",
             "expiry": "2026-10-29"},
        ]
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=38,
            dependent_short_quantity=75, outcome="partially_filled",
        )
        # 38/75 of 75 = floor(38) -> 38. Never more than the hedge that exists.
        assert decision.released_quantity == 38
        assert decision.detail["unreleased_quantity"] == 37
        assert decision.blocked is True

        orders, detail = build_structure_exit_orders(
            legs, closed_short_quantities={"SHORT-CE": decision.released_quantity}
        )
        by_symbol = {order["tradingsymbol"]: order for order in orders}
        # The remainder of the short is closed and the remainder of the hedge held.
        assert by_symbol["SHORT-CE"]["quantity"] == 37
        assert by_symbol["HEDGE-CE"]["quantity"] == 38
        assert detail["naked_short_quantity"] == 37

    def test_a_rejected_leg_releases_nothing_and_needs_an_operator(self):
        sf = self.make_db()
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=50,
            dependent_short_quantity=75, outcome="rejected",
        )
        assert decision.released_quantity == 0
        assert decision.action_required is True

        orders, detail = build_structure_exit_orders(
            [{"tradingsymbol": "SHORT-CE", "side": "SELL", "quantity": -75,
              "exchange": "NFO", "product": "NRML"},
             {"tradingsymbol": "HEDGE-CE", "side": "BUY", "quantity": 75,
              "exchange": "NFO", "product": "NRML"}],
            closed_short_quantities={},
        )
        # The hedge is withheld entirely and the short close is still submitted.
        assert [order["tradingsymbol"] for order in orders] == ["SHORT-CE"]
        assert detail["withheld_hedges"][0]["reason"] == "short_not_proven_closed"

    def test_a_hedge_timeout_releases_nothing(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=50,
            dependent_short_quantity=75, outcome="pending",
            elapsed_seconds=31, timeout_seconds=30,
        )
        assert decision.released_quantity == 0
        assert decision.action_required is True
        assert decision.reason == "hedge_fill_timeout"


class TestWalkthroughSix(_PgTestCase):
    """Index cash settlement with evidence, and expiry time alone adjusting nothing."""

    def test_cash_settlement_with_evidence_settles_the_run(self):
        sf = self.make_db()
        result = self.settlement(sf).settle(
            account_id="kite:A", option_run_id="run-w6", structure_digest="digest-w6",
            settlement_kind="cash", evidence_source="exchange_file",
            evidence_ref={"file": "F-1"}, recorded_by="app:owner",
        )
        assert result["run_state"] == "settled"
        rows = self.settlement(sf).evidence_for(option_run_id="run-w6")
        assert len(rows) == 1
        assert rows[0]["evidence_source"] == "exchange_file"

        with sf() as session:
            axes = option_settlement_axes(
                account_id="kite:A", strategy_id="stg-A", execution_environment="paper",
                db=session,
            )
        assert axes[0]["state"] == "settled"

    def test_expiry_time_alone_adjusts_nothing(self):
        """THE negative walkthrough: no evidence, no settlement, nothing recorded."""
        sf = self.make_db()
        with pytest.raises(SettlementRefusal):
            self.settlement(sf).settle(
                account_id="kite:A", option_run_id="run-w6", structure_digest="digest-w6"
            )
        assert _scalar(sf, "SELECT COUNT(*) FROM public.option_settlement_evidence") == 0
        with sf() as session:
            axes = option_settlement_axes(
                account_id="kite:A", strategy_id="stg-A", execution_environment="paper",
                db=session,
            )
        assert axes[0]["state"] == "unsettled"


class TestExpiryAndMis(_PgTestCase):
    def test_an_index_structure_escalates_inside_the_window(self):
        sf = self.make_db()
        policy = OptionExpiryPolicy(
            notifier=lambda account_id, detail: self.notified.append(account_id) or True
        )
        check = policy.check(
            account_id="kite:A", run={"expiry_policy": "allow_cash_settlement"},
            expiry=(NOW + timedelta(days=2)).date().isoformat(), now=NOW,
        )
        assert check.escalated is True
        # Cash settlement is legitimate, so no action_required.
        assert check.action_required is False
        assert self.notified == ["kite:A"]

    def test_mis_options_square_off_and_never_reach_expiry(self):
        sf = self.make_db()
        policy = OptionExpiryPolicy(
            notifier=lambda account_id, detail: self.notified.append(account_id) or True
        )
        check = policy.check(
            account_id="kite:A", run={},
            expiry=(NOW + timedelta(days=1)).date().isoformat(),
            product="MIS", now=NOW,
        )
        assert check.escalated is False
        assert check.reason == "mis_squared_off_by_schedule"
        assert self.notified == []


if __name__ == "__main__":
    unittest.main()
