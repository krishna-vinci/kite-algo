"""MIS intraday policy: multi-day MIS is refused, not silently liquidated (D-1).

R3 §12 records a correction rather than a feature: ordinary MIS is intraday, so a
multi-day holding cannot live in it. The architecture must **refuse** the
multi-day MIS intent, because the alternative — accepting it and letting the
15:20 square-off liquidate a position the operator meant to hold — turns a policy
mistake into a forced trade at whatever price the close offers.

The refusal is a validation refusal, so it ends the evaluation (Phase 3
semantics): the envelope is stored ``refused``, the identity is spent, and a
corrected intent needs a new one.
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401

G1 = "11111111-1111-1111-1111-111111111111"
T1 = "2026-09-01T00:00:00+00:00"


class MisTestCase(unittest.TestCase):
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
                CREATE TABLE public.instrument_catalog_generations (
                    id TEXT PRIMARY KEY, status TEXT, published_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_records (
                    instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,
                    lifecycle_status TEXT NOT NULL DEFAULT 'active', lot_size INTEGER,
                    instrument_type TEXT, current_generation_id TEXT,
                    expiry TEXT, tick_size REAL, underlying TEXT,
                    strike REAL, option_type TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_broker_mappings (
                    mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT,
                    broker_exchange TEXT, broker_symbol TEXT, broker_token INTEGER,
                    valid_from_generation TEXT, valid_to_generation TEXT, is_current INTEGER
                )
                """
            )
            dbapi_connection.commit()

        _Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES (:id, 'published', :published_at)"
                ),
                {"id": G1, "published_at": T1},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, lot_size, "
                    " instrument_type, current_generation_id) "
                    "VALUES ('inst-REL', 'NSE', 'RELIANCE', 'active', 1, 'EQ', :id)"
                ),
                {"id": G1},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES ('map-REL', 'inst-REL', 'kite', 'NSE', 'RELIANCE', 100, :id, 1)"
                ),
                {"id": G1},
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def pinned(self):
        from backend.strategies.compiler.base import PinnedCatalogRead

        return PinnedCatalogRead(session_factory=self.factory, generation=G1)

    def payload(self, **overrides):
        values = {
            "instrument_token": 100,
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "product": "MIS",
            "target_quantity": 10,
            "reference_price": 100.0,
        }
        values.update(overrides)
        return values


class IntradayScopeTests(MisTestCase):
    def test_a_same_session_mis_plan_is_accepted(self):
        from backend.strategies.compiler import compile_resolved_plan

        # hold_days omitted means intraday, which is what MIS IS: no refusal.
        plan = compile_resolved_plan("single_instrument", self.payload(), self.pinned())
        self.assertEqual(plan.resolved["legs"][0]["product"], "MIS")

        explicit = compile_resolved_plan(
            "single_instrument", self.payload(hold_days=1), self.pinned()
        )
        self.assertEqual(explicit.resolved["legs"][0]["product"], "MIS")

    def test_a_multi_day_mis_long_is_refused_with_guidance(self):
        from backend.strategies.compiler import compile_resolved_plan
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            compile_resolved_plan(
                "single_instrument", self.payload(hold_days=3), self.pinned()
            )
        self.assertEqual(ctx.exception.reason_code, "MIS_OVERNIGHT_REFUSED")
        # The refusal must say what to do instead, or it is just an obstacle.
        message = str(ctx.exception.detail.get("message") or "")
        self.assertIn("CNC", message)
        self.assertIn("NRML", message)

    def test_a_multi_day_mis_short_names_futures_and_options(self):
        from backend.strategies.compiler import compile_resolved_plan
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            compile_resolved_plan(
                "single_instrument",
                self.payload(hold_days=2, target_quantity=-10),
                self.pinned(),
            )
        self.assertEqual(ctx.exception.reason_code, "MIS_OVERNIGHT_REFUSED")
        message = str(ctx.exception.detail.get("message") or "")
        # Equity shorts cannot be held overnight in the cash segment at all.
        self.assertIn("futures", message)
        self.assertIn("options", message)

    def test_the_refusal_carries_the_evidence(self):
        from backend.strategies.compiler import compile_resolved_plan
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            compile_resolved_plan(
                "single_instrument", self.payload(hold_days=5), self.pinned()
            )
        detail = ctx.exception.detail
        self.assertEqual(detail["product"], "MIS")
        self.assertEqual(detail["hold_days"], 5)
        self.assertIn("intraday", str(detail.get("message") or "").lower())

    def test_a_multi_day_cnc_plan_is_fine(self):
        from backend.strategies.compiler import compile_resolved_plan

        # The rule is specific to MIS: CNC is exactly what multi-day long equity is for.
        plan = compile_resolved_plan(
            "single_instrument", self.payload(product="CNC", hold_days=30), self.pinned()
        )
        self.assertEqual(plan.resolved["legs"][0]["product"], "CNC")

    def test_a_multi_day_nrml_plan_is_fine(self):
        from backend.strategies.compiler import compile_resolved_plan

        plan = compile_resolved_plan(
            "single_instrument", self.payload(product="NRML", hold_days=30), self.pinned()
        )
        self.assertEqual(plan.resolved["legs"][0]["product"], "NRML")

    def test_a_multi_day_mis_leg_refuses_the_whole_bundle(self):
        from backend.strategies.compiler import compile_resolved_plan
        from backend.strategies.compiler.base import ValidationRefusal

        payload = {
            "legs": [
                self.payload(product="CNC", hold_days=30),
                self.payload(product="MIS", hold_days=2),
            ]
        }
        with self.assertRaises(ValidationRefusal) as ctx:
            compile_resolved_plan("intent_bundle", payload, self.pinned())
        self.assertEqual(ctx.exception.reason_code, "MIS_OVERNIGHT_REFUSED")


class ValidatorTests(MisTestCase):
    def test_the_validator_is_a_pure_function(self):
        from backend.strategies.mis_policy import validate_intraday_scope

        # Intraday MIS passes...
        validate_intraday_scope(product="MIS", hold_days=1, signed_quantity=10)
        validate_intraday_scope(product="MIS", hold_days=None, signed_quantity=10)
        # ...everything else is untouched by the rule...
        validate_intraday_scope(product="CNC", hold_days=99, signed_quantity=10)
        validate_intraday_scope(product="NRML", hold_days=99, signed_quantity=-10)
        # ...and a multi-day MIS raises.
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal):
            validate_intraday_scope(product="MIS", hold_days=2, signed_quantity=10)

    def test_the_reason_code_is_in_the_vocabulary(self):
        from backend.strategies.compiler.base import REFUSAL_REASONS

        self.assertIn("MIS_OVERNIGHT_REFUSED", REFUSAL_REASONS)

    def test_hold_days_is_parsed_leniently_but_never_silently_downgraded(self):
        from backend.strategies.compiler.base import ValidationRefusal
        from backend.strategies.mis_policy import validate_intraday_scope

        # A horizon that cannot be read is not evidence of intraday, but it is also
        # not a claim of multi-day: the default is intraday, which is what MIS means.
        validate_intraday_scope(product="MIS", hold_days="1", signed_quantity=10)
        with self.assertRaises(ValidationRefusal):
            validate_intraday_scope(product="MIS", hold_days="7", signed_quantity=10)


if __name__ == "__main__":
    unittest.main()
