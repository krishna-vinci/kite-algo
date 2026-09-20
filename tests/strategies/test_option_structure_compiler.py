"""``option_structure``: pinned leg resolution and a frozen expiry policy (D-1, D-7).

An option structure is a set of contracts that only exists as a *set*: the legs are
chosen together and their identity is what makes the structure that structure. So
every leg resolves against the pinned catalog generation, the resolved legs freeze
into the immutable plan, and nothing re-resolves them later — chain data at runtime
feeds evaluation metrics, never identity.

The expiry policy is frozen with the structure for the same reason. A short leg
cannot be decided at expiry time to have been physical all along, so the policy is
chosen at plan time and a structure that would need physical settlement without the
capability to take delivery is refused then, not at the last session.
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401
import backend.strategies.attribution_models  # noqa: F401

G1 = "11111111-1111-1111-1111-111111111111"


class OptionStructureTestCase(unittest.TestCase):
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
                "CREATE TABLE public.instrument_catalog_generations "
                "(id TEXT PRIMARY KEY, status TEXT, published_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE public.instrument_catalog_records "
                "(instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT, "
                " lifecycle_status TEXT NOT NULL DEFAULT 'active', current_generation_id TEXT, "
                " instrument_type TEXT, expiry TEXT, lot_size INTEGER, tick_size REAL, "
                " underlying TEXT, strike REAL, option_type TEXT)"
            )
            cursor.execute(
                "CREATE TABLE public.instrument_broker_mappings "
                "(mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT, "
                " broker_exchange TEXT, broker_symbol TEXT, broker_token INTEGER, "
                " valid_from_generation TEXT, valid_to_generation TEXT, is_current INTEGER)"
            )
            dbapi_connection.commit()

        _Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES (:id, 'published', '2026-09-01T00:00:00+00:00')"
                ),
                {"id": G1},
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def option(self, *, strike, option_type, token, expiry="2026-10-29", lot=75, kind=None):
        symbol = f"NIFTY26OCT{strike}{option_type}"
        instrument_id = f"opt-{strike}-{option_type}"
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, "
                    " current_generation_id, instrument_type, expiry, lot_size, tick_size, "
                    " underlying, strike, option_type) "
                    "VALUES (:id, 'NFO', :symbol, 'active', :gen, :kind, :expiry, :lot, 0.05, "
                    " 'NIFTY', :strike, :option_type)"
                ),
                {"id": instrument_id, "symbol": symbol, "gen": G1,
                 "kind": kind or option_type, "expiry": expiry, "lot": lot,
                 "strike": strike, "option_type": option_type},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES (:mid, :id, 'kite', 'NFO', :symbol, :token, :gen, 1)"
                ),
                {"mid": f"map-{strike}-{option_type}", "id": instrument_id,
                 "symbol": symbol, "token": token, "gen": G1},
            )
            session.commit()
        return instrument_id

    def pinned(self):
        from backend.strategies.compiler.base import PinnedCatalogRead

        return PinnedCatalogRead(session_factory=self.factory, generation=G1)

    def leg(self, *, strike, option_type, token, side="SELL", ratio=1, price=100.0, **over):
        values = {
            "instrument_token": token,
            "exchange": "NFO",
            "tradingsymbol": f"NIFTY26OCT{strike}{option_type}",
            "option_type": option_type,
            "strike": strike,
            "side": side,
            "ratio": ratio,
            "reference_price": price,
        }
        values.update(over)
        return values

    def payload(self, legs, **overrides):
        values = {"underlying": "NIFTY", "expiry": "2026-10-29", "product": "NRML",
                  "legs": legs}
        values.update(overrides)
        return values

    def compile(self, payload, *, chain_resolver=None):
        from backend.strategies.compiler import compile_resolved_plan

        return compile_resolved_plan(
            "option_structure", payload, self.pinned(), chain_resolver=chain_resolver
        )


class ResolutionTests(OptionStructureTestCase):
    def test_option_structure_is_a_registered_kind(self):
        from backend.strategies.compiler import compiler_for

        self.assertIsNotNone(compiler_for("option_structure"))

    def test_legs_resolve_against_the_pinned_generation(self):
        self.option(strike=25000, option_type="CE", token=501)
        self.option(strike=24800, option_type="PE", token=502)
        plan = self.compile(
            self.payload([
                self.leg(strike=25000, option_type="CE", token=501, side="SELL"),
                self.leg(strike=24800, option_type="PE", token=502, side="SELL"),
            ])
        )
        legs = plan.resolved["legs"]
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0]["option_type"], "CE")
        self.assertEqual(legs[0]["strike"], 25000.0)
        self.assertEqual(legs[0]["lot_size"], 75)
        # Lots are the unit; the quantity is ratio x lot_size.
        self.assertEqual(legs[0]["signed_quantity"], -75)
        self.assertEqual(legs[1]["signed_quantity"], -75)
        self.assertIn("structure_digest", plan.resolved)

    def test_a_long_leg_is_positive(self):
        self.option(strike=25000, option_type="CE", token=501)
        plan = self.compile(
            self.payload([self.leg(strike=25000, option_type="CE", token=501, side="BUY")])
        )
        self.assertEqual(plan.resolved["legs"][0]["signed_quantity"], 75)

    def test_an_unresolvable_leg_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                self.payload([self.leg(strike=25000, option_type="CE", token=999)])
            )
        self.assertEqual(ctx.exception.reason_code, "OPTION_LEG_UNRESOLVED")
        self.assertEqual(ctx.exception.detail["leg_index"], 0)

    def test_an_equity_mapping_is_not_an_option_leg(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.option(strike=25000, option_type="CE", token=501, kind="EQ")
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                self.payload([self.leg(strike=25000, option_type="CE", token=501)])
            )
        self.assertEqual(ctx.exception.reason_code, "OPTION_LEG_UNRESOLVED")
        self.assertEqual(ctx.exception.detail["instrument_type"], "EQ")

    def test_a_contract_without_an_expiry_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.option(strike=25000, option_type="CE", token=501, expiry=None)
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                self.payload([self.leg(strike=25000, option_type="CE", token=501)])
            )
        self.assertEqual(ctx.exception.reason_code, "EXPIRY_UNAVAILABLE")

    def test_the_digest_is_stable_and_discriminating(self):
        self.option(strike=25000, option_type="CE", token=501)
        self.option(strike=24800, option_type="PE", token=502)
        legs = [
            self.leg(strike=25000, option_type="CE", token=501, side="SELL"),
            self.leg(strike=24800, option_type="PE", token=502, side="SELL"),
        ]
        first = self.compile(self.payload(legs)).resolved["structure_digest"]
        second = self.compile(self.payload(legs)).resolved["structure_digest"]
        self.assertEqual(first, second)
        changed = self.compile(
            self.payload([
                self.leg(strike=25000, option_type="CE", token=501, side="BUY"),
                self.leg(strike=24800, option_type="PE", token=502, side="SELL"),
            ])
        ).resolved["structure_digest"]
        self.assertNotEqual(first, changed)


class SelectionPolicyTests(OptionStructureTestCase):
    def test_a_selection_leg_without_a_chain_resolver_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                self.payload([
                    {"selection": {"option_type": "CE", "moneyness": "ATM", "offset": 0},
                     "side": "SELL", "ratio": 1, "reference_price": 100.0}
                ])
            )
        self.assertEqual(ctx.exception.reason_code, "SELECTION_POLICY_UNRESOLVABLE")

    def test_a_selection_leg_resolves_through_the_chain(self):
        instrument_id = self.option(strike=25000, option_type="CE", token=501)

        def resolver(*, underlying, expiry, option_type, moneyness, offset):
            return {
                "instrument_token": 501,
                "instrument_id": instrument_id,
                "strike": 25000,
                "option_type": option_type,
                "expiry": "2026-10-29",
            }

        plan = self.compile(
            self.payload([
                {"selection": {"option_type": "CE", "moneyness": "ATM", "offset": 0},
                 "side": "SELL", "ratio": 1, "reference_price": 100.0}
            ]),
            chain_resolver=resolver,
        )
        self.assertEqual(plan.resolved["legs"][0]["instrument_id"], instrument_id)

    def test_a_chain_that_cannot_match_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                self.payload([
                    {"selection": {"option_type": "CE", "moneyness": "OTM", "offset": 9},
                     "side": "SELL", "ratio": 1, "reference_price": 100.0}
                ]),
                chain_resolver=lambda **_: None,
            )
        self.assertEqual(ctx.exception.reason_code, "SELECTION_POLICY_UNRESOLVABLE")

    def test_a_malformed_selection_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                self.payload([{"selection": {"option_type": "XX"}, "side": "SELL",
                               "ratio": 1}])
            )
        self.assertEqual(ctx.exception.reason_code, "SELECTION_POLICY_UNRESOLVABLE")


class ExpiryPolicyTests(OptionStructureTestCase):
    def _short_structure(self):
        self.option(strike=25000, option_type="CE", token=501)
        return self.payload([self.leg(strike=25000, option_type="CE", token=501, side="SELL")])

    def test_a_short_structure_defaults_to_exit_before_cutoff(self):
        plan = self.compile(self._short_structure())
        # A short leg cannot be discovered at expiry to have been physical all along.
        self.assertEqual(plan.resolved["expiry_policy"], "exit_before_cutoff")

    def test_a_long_only_index_structure_defaults_to_cash_settlement(self):
        self.option(strike=25000, option_type="CE", token=501)
        plan = self.compile(
            self.payload([self.leg(strike=25000, option_type="CE", token=501, side="BUY")])
        )
        self.assertEqual(plan.resolved["expiry_policy"], "allow_cash_settlement")

    def test_physical_settlement_without_capability_refuses_at_plan_time(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.option(strike=25000, option_type="CE", token=501)
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                self.payload(
                    [self.leg(strike=25000, option_type="CE", token=501, side="BUY")],
                    expiry_policy="allow_physical_settlement",
                )
            )
        self.assertEqual(
            ctx.exception.reason_code, "PHYSICAL_SETTLEMENT_CAPABILITY_REQUIRED"
        )

    def test_physical_settlement_with_capability_evidence_is_allowed(self):
        self.option(strike=25000, option_type="CE", token=501)
        plan = self.compile(
            self.payload(
                [self.leg(strike=25000, option_type="CE", token=501, side="BUY")],
                expiry_policy="allow_physical_settlement",
                settlement_capability={"delivery": True, "funding_ref": "note-1"},
            )
        )
        self.assertEqual(plan.resolved["expiry_policy"], "allow_physical_settlement")

    def test_an_invented_policy_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        # The legs resolve first: a policy cannot be validated for a structure
        # whose legs do not exist.
        self.option(strike=25000, option_type="CE", token=501)
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.payload(
                [self.leg(strike=25000, option_type="CE", token=501)],
                expiry_policy="make_it_up",
            ))
        self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")


if __name__ == "__main__":
    unittest.main()
