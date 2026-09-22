"""Futures contracts, full-fill-gated rolls and margin peaks on PostgreSQL.

Why PostgreSQL: SQLite cannot prove the append-only roll trail, the advisory lock
that makes a duplicate-roll refusal real under concurrency, or the composite FKs.
Every test runs against a DISPOSABLE, uniquely named database created on the test
server, upgraded with ``alembic upgrade head`` and dropped afterwards. No existing
database is ever touched.

    FUTURES_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_futures_rolls_postgres.py -q

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

from backend.strategies.expiry_policy import check_expiry_cutoff  # noqa: E402
from backend.strategies.futures_margin import (  # noqa: E402
    futures_peak_refusal,
    peak_margin_evidence,
)
from backend.strategies.rolls import (  # noqa: E402
    ReleaseRefused,
    RollDuplicate,
    RollNotFlat,
    RollStateMachine,
)

PG_URL = os.environ.get("FUTURES_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "FUTURES_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc)
G1 = "11111111-1111-1111-1111-111111111111"
OLD = "a0000000-0000-0000-0000-00000000000a"
NEW = "b0000000-0000-0000-0000-00000000000b"


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
        dbname = f"kite_fut_{uuid.uuid4().hex[:12]}"
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

    def machine(self, sf):
        return RollStateMachine(
            session_factory=sf,
            notifier=lambda account_id, roll: self.notified.append(account_id) or True,
        )


def _exec(sf, sql, params=None):
    with sf() as session:
        session.execute(text(sql), params or {})
        session.commit()


def _scalar(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).scalar()


def seed_world(sf):
    """One strategy, one catalogued futures contract with its derivative metadata."""
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
        " instrument_type, expiry, lot_size, tick_size, underlying, current_generation_id) "
        "VALUES (:iid, 'identity-nifty-fut', 'NFO:NIFTY26OCTFUT', 'NFO', 'NIFTY26OCTFUT', "
        " 'active', 'FUT', '2026-10-29', 75, 0.05, 'NIFTY', :gen) "
        "ON CONFLICT (instrument_id) DO NOTHING",
        {"iid": "c0000000-0000-0000-0000-00000000000c", "gen": G1},
    )
    _exec(
        sf,
        "INSERT INTO public.instrument_broker_mappings "
        "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
        " valid_from_generation, is_current) VALUES "
        "('c0000000-0000-0000-0000-00000000000c', 'kite', 'NFO', 'NIFTY26OCTFUT', 500, :gen, TRUE) "
        "ON CONFLICT DO NOTHING",
        {"gen": G1},
    )
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES ('stg-F', 'app:owner', 'Futures F', 'kite:A', 'active') "
        "ON CONFLICT (id) DO NOTHING",
    )


def book(sf, instrument_id, quantity):
    _exec(
        sf,
        "INSERT INTO strategy_position_projection "
        "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
        " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
        " net_quantity, projection_version) "
        "VALUES ('kite:A', 'stg-F', 'paper', 'canonical', :key, :iid, 'NRML', 500, 'NFO', "
        " 'NIFTY26OCTFUT', :qty, 1)",
        {"key": f"{instrument_id}-{uuid.uuid4().hex[:6]}", "iid": instrument_id,
         "qty": int(quantity)},
    )


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


class TestMigration(_PgTestCase):
    def test_head_and_shape(self):
        sf = self.make_db()
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000033"
        for table in ("strategy_rolls", "strategy_roll_events"):
            assert _scalar(
                sf,
                "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public' AND tablename=:t",
                {"t": table},
            ) == 1
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM pg_trigger "
            "WHERE tgname='trg_strategy_roll_events_immutable'",
        ) == 1

    def test_upgrade_from_prior_head_is_additive(self):
        sf = self.make_db("20260917_000032")
        db_url = _url_for(self._created[-1])
        _exec(
            sf,
            "INSERT INTO public.strategies (id, owner_id, name, account_scope) "
            "VALUES ('stg-prior', 'app:o', 'prior', 'kite:A')",
        )
        _upgrade(db_url, "head")
        assert _scalar(sf, "SELECT version_num FROM alembic_version") >= "20260917_000033"
        assert _scalar(sf, "SELECT name FROM public.strategies WHERE id='stg-prior'") == "prior"
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_rolls") == 0

    def test_the_roll_trail_is_append_only(self):
        sf = self.make_db()
        seed_world(sf)
        roll = self.machine(sf).create(
            strategy_id="stg-F", account_id="kite:A",
            old_instrument_id=OLD, new_instrument_id=NEW,
            required_replacement_quantity=75,
        )
        for statement in (
            "UPDATE public.strategy_roll_events SET event='completed'",
            "DELETE FROM public.strategy_roll_events",
        ):
            with pytest.raises(Exception) as exc:
                _exec(sf, statement)
            assert "append-only" in str(exc.value).lower()
        assert roll["roll_id"]


# ---------------------------------------------------------------------------
# the roll invariant
# ---------------------------------------------------------------------------


class TestRollInvariant(_PgTestCase):
    def _roll(self, sf, *, required=75):
        return self.machine(sf).create(
            strategy_id="stg-F", account_id="kite:A",
            old_instrument_id=OLD, new_instrument_id=NEW,
            required_replacement_quantity=required,
            old_coordinate={"product": "NRML", "expiry": "2026-10-29"},
            new_coordinate={"product": "NRML", "expiry": "2026-11-26", "side": "BUY"},
        )

    def test_the_partial_new_fill_walkthrough(self):
        """THE acceptance test: a stalled replacement never releases the close."""
        sf = self.make_db()
        seed_world(sf)
        machine = self.machine(sf)
        roll = self._roll(sf)
        rid = roll["roll_id"]
        machine.acquire(rid)

        # The replacement is only partly filled: the CONFIRMED execution is
        # recorded against the roll (the durable proof - a book alone proves
        # nothing about this roll).
        machine.record_replacement_fill(
            rid, paper_order_id="PAPER-PARTIAL", quantity=30, instrument_id=NEW
        )
        stalled = machine.prove_filled(rid)
        assert stalled["state"] == "action_required"
        assert stalled["proven_filled_quantity"] == 30
        # Old attribution is INTACT and both identities are still on the roll.
        assert stalled["old_instrument_id"] == OLD
        assert stalled["new_instrument_id"] == NEW
        # And the close step is unreachable at the state-machine level.
        with pytest.raises(ReleaseRefused):
            machine.release_close(rid)

        # No auto-reverse: the old book was never touched.
        assert _scalar(
            sf,
            "SELECT COUNT(*) FROM strategy_position_projection "
            "WHERE canonical_instrument_id = :old",
            {"old": OLD},
        ) == 0

        # The replacement completes and only then does the close become reachable.
        machine.record_replacement_fill(
            rid, paper_order_id="PAPER-REST", quantity=50, instrument_id=NEW
        )
        proven = machine.prove_filled(rid)
        assert proven["state"] == "releasing_old"
        assert proven["proven_filled_quantity"] == 80
        machine.release_close(rid)

        # The old book must be PROVEN flat before the roll completes.
        book(sf, OLD, 75)
        with pytest.raises(RollNotFlat):
            machine.mark_old_flat(rid)
        assert machine.get(rid)["state"] == "releasing_old"

    def test_the_close_step_stays_unreachable_until_full_proof(self):
        sf = self.make_db()
        seed_world(sf)
        machine = self.machine(sf)
        roll = self._roll(sf, required=75)
        rid = roll["roll_id"]

        for state_check in (0, 74):
            machine.acquire(rid) if state_check == 0 else None
            machine.prove_filled(rid, proven_quantity=state_check)
            with pytest.raises(ReleaseRefused) as ctx:
                machine.release_close(rid)
            assert ctx.value.reason_code == "ROLL_FILL_NOT_PROVEN"
        machine.prove_filled(rid, proven_quantity=75)
        assert machine.release_close(rid)["state"] == "releasing_old"

    def test_one_open_roll_per_old_contract(self):
        sf = self.make_db()
        seed_world(sf)
        self._roll(sf)
        with pytest.raises(RollDuplicate) as ctx:
            self._roll(sf)
        assert ctx.value.reason_code == "ROLL_ALREADY_OPEN"

    def test_concurrent_roll_opens_produce_exactly_one(self):
        """The advisory lock is what makes the duplicate check real."""
        sf = self.make_db()
        seed_world(sf)
        outcomes: list = []
        barrier = threading.Barrier(2)

        def attempt():
            machine = self.machine(sf)
            try:
                barrier.wait(timeout=30)
                outcomes.append(("ok", self._roll(sf)["roll_id"]))
            except RollDuplicate:
                outcomes.append(("duplicate", None))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("error", repr(exc)))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        errors = [item for item in outcomes if item[0] == "error"]
        assert not errors, errors
        assert _scalar(sf, "SELECT COUNT(*) FROM public.strategy_rolls") == 1, outcomes
        assert sorted(item[0] for item in outcomes) == ["duplicate", "ok"], outcomes


class TestBasketFlagIrrelevance(_PgTestCase):
    def test_the_flag_changes_nothing_about_roll_behaviour(self):
        sf = self.make_db()
        seed_world(sf)
        states = []
        # A distinct old contract per case rather than clearing the trail: the
        # append-only trigger refuses the DELETE, which is the point of it.
        for index, flagged in enumerate((True, False)):
            machine = self.machine(sf)
            roll = machine.create(
                strategy_id="stg-F", account_id="kite:A",
                old_instrument_id=f"a0000000-0000-0000-0000-0000000000{index + 1:02d}",
                new_instrument_id=NEW,
                required_replacement_quantity=75,
                new_coordinate={"product": "NRML", "side": "BUY"},
                # A basket-atomicity label the roll ignores entirely.
                peak_margin_evidence={"all_or_none": flagged},
            )
            machine.acquire(roll["roll_id"])
            states.append(machine.prove_filled(roll["roll_id"], proven_quantity=20)["state"])
        assert states == ["action_required", "action_required"]


# ---------------------------------------------------------------------------
# margin and expiry
# ---------------------------------------------------------------------------


class TestMarginAndExpiry(_PgTestCase):
    def test_the_peak_is_both_legs_and_refuses_with_its_arithmetic(self):
        sf = self.make_db()
        seed_world(sf)
        new_leg = {
            "instrument_type": "FUT", "product": "NRML", "quantity": 75,
            "signed_quantity": 75, "side": "BUY", "reference_price": 25000.0,
        }
        old_leg = {**new_leg, "side": "SELL", "signed_quantity": -75}
        single = peak_margin_evidence(new_legs=[new_leg])["peak_margin_inr"]
        both = peak_margin_evidence(new_legs=[new_leg], old_legs=[old_leg])
        assert both["concurrent"] is True
        assert abs(both["peak_margin_inr"] - single * 2) < 1.0

        refusal = futures_peak_refusal(peak=both, available_inr=single * 1.5)
        assert refusal["rejection_reason"] == "MARGIN_INSUFFICIENT"
        assert refusal["required_peak_margin_inr"] > refusal["available_inr"]
        assert refusal["old_legs_margin_inr"] > 0

    def test_an_unrolled_expiring_contract_escalates_once(self):
        sf = self.make_db()
        seed_world(sf)
        machine = self.machine(sf)
        expiry = (NOW + timedelta(days=3)).date().isoformat()
        roll = machine.create(
            strategy_id="stg-F", account_id="kite:A",
            old_instrument_id=OLD, new_instrument_id=NEW,
            required_replacement_quantity=75,
            old_coordinate={"product": "NRML", "expiry": expiry},
            new_coordinate={"product": "NRML", "expiry": expiry, "side": "BUY"},
        )
        machine.acquire(roll["roll_id"])
        result = check_expiry_cutoff(machine, roll["roll_id"], now=NOW)
        assert result["escalated"] is True
        assert result["days_to_expiry"] == 3
        # ONE owner notification, and no improvised close.
        assert self.notified == ["kite:A"]
        events = [row["event"] for row in machine.events(roll["roll_id"])]
        assert "escalated" in events
        assert "close_released" not in events
        assert machine.get(roll["roll_id"])["state"] == "action_required"

    def test_a_contract_outside_the_window_is_left_alone(self):
        sf = self.make_db()
        seed_world(sf)
        machine = self.machine(sf)
        expiry = (NOW + timedelta(days=40)).date().isoformat()
        roll = machine.create(
            strategy_id="stg-F", account_id="kite:A",
            old_instrument_id=OLD, new_instrument_id=NEW,
            required_replacement_quantity=75,
            new_coordinate={"product": "NRML", "expiry": expiry},
        )
        result = check_expiry_cutoff(machine, roll["roll_id"], now=NOW)
        assert result["escalated"] is False
        assert self.notified == []


class TestFuturesCompiler(_PgTestCase):
    def test_a_contract_resolves_with_its_metadata(self):
        from backend.strategies.compiler import compile_resolved_plan
        from backend.strategies.compiler.base import PinnedCatalogRead

        sf = self.make_db()
        seed_world(sf)
        pinned = PinnedCatalogRead(session_factory=sf, generation=G1)
        plan = compile_resolved_plan(
            "target_futures",
            {
                "instrument_token": 500, "exchange": "NFO",
                "tradingsymbol": "NIFTY26OCTFUT", "product": "NRML",
                "lots": 2, "reference_price": 25000.0,
            },
            pinned,
        )
        leg = plan.resolved["legs"][0]
        assert leg["lot_size"] == 75
        assert leg["quantity"] == 150
        assert leg["expiry"] == "2026-10-29"
        assert leg["tick_size"] == 0.05
        # The freeze axis is honestly recorded as unchecked: the catalog has no
        # freeze column.
        assert leg["freeze_source"] == "unavailable"

    def test_an_equity_mapping_is_not_a_futures_contract(self):
        from backend.strategies.compiler import compile_resolved_plan
        from backend.strategies.compiler.base import PinnedCatalogRead, ValidationRefusal

        sf = self.make_db()
        seed_world(sf)
        pinned = PinnedCatalogRead(session_factory=sf, generation=G1)
        with pytest.raises(ValidationRefusal) as ctx:
            compile_resolved_plan(
                "target_futures",
                {
                    "instrument_token": 999, "exchange": "NFO",
                    "tradingsymbol": "NOPE", "lots": 1,
                },
                pinned,
            )
        assert ctx.value.reason_code == "CONTRACT_UNRESOLVED"


if __name__ == "__main__":
    unittest.main()
