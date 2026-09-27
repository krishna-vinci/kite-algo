"""``target_futures``: contract resolution against the pinned generation (D-1).

A futures contract is not an equity with a different label. It has a lot size that
sizes the position, an expiry that gives it a deadline, a tick that constrains
price, and a lifecycle that can end. Resolving it against the pinned generation is
what makes a futures plan reproducible: the contract a plan named is the contract
it meant, even after the underlying rolls.

Two of the refusals here exist because the alternative is worse than refusing. A
contract without an expiry cannot be rolled, and a plan that cannot be rolled is a
position nobody planned to hold — so ``EXPIRY_UNAVAILABLE`` refuses rather than
resolving an undated contract. And a quantity above the exchange freeze limit does
not fail at the broker, it fails *quietly* across multiple orders, which is how a
position ends up sized differently from what the strategy asked for.
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


class FuturesCompilerTestCase(unittest.TestCase):
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
                    lifecycle_status TEXT NOT NULL DEFAULT 'active', current_generation_id TEXT,
                    instrument_type TEXT, expiry TEXT, lot_size INTEGER, tick_size REAL,
                    underlying TEXT,
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
                    "VALUES (:id, 'published', '2026-09-01T00:00:00+00:00')"
                ),
                {"id": G1},
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def contract(self, symbol="NIFTY26OCTFUT", *, token=500, kind="FUT", expiry="2026-10-29",
                 lot=75, tick="0.05", lifecycle="active", underlying="NIFTY",
                 exchange="NFO"):
        instrument_id = f"inst-{symbol}"
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, "
                    " current_generation_id, instrument_type, expiry, lot_size, tick_size, "
                    " underlying) VALUES (:id, :exchange, :symbol, :lifecycle, :gen, :kind, "
                    " :expiry, :lot, :tick, :underlying)"
                ),
                {"id": instrument_id, "symbol": symbol, "lifecycle": lifecycle, "gen": G1,
                 "kind": kind, "expiry": expiry, "lot": lot, "tick": tick,
                 "underlying": underlying, "exchange": exchange},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES (:mid, :id, 'kite', :exchange, :symbol, :token, :gen, 1)"
                ),
                {"mid": f"map-{symbol}", "id": instrument_id, "symbol": symbol,
                 "token": token, "gen": G1, "exchange": exchange},
            )
            session.commit()
        return instrument_id

    def pinned(self):
        from backend.strategies.compiler.base import PinnedCatalogRead

        return PinnedCatalogRead(session_factory=self.factory, generation=G1)

    def payload(self, **overrides):
        values = {
            "instrument_token": 500,
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26OCTFUT",
            "product": "NRML",
            "lots": 2,
            "reference_price": 25000.0,
        }
        values.update(overrides)
        return values

    def compile(self, payload=None, **overrides):
        from backend.strategies.compiler import compile_resolved_plan

        return compile_resolved_plan(
            "target_futures", payload or self.payload(**overrides), self.pinned()
        )


class ResolutionTests(FuturesCompilerTestCase):
    def test_target_futures_is_a_registered_kind(self):
        from backend.strategies.compiler import compiler_for

        self.assertIsNotNone(compiler_for("target_futures"))

    def test_a_contract_resolves_with_its_derivative_metadata(self):
        self.contract()
        plan = self.compile()
        leg = plan.resolved["legs"][0]
        self.assertEqual(leg["instrument_id"], "inst-NIFTY26OCTFUT")
        # Lots are the semantic unit; the quantity is lots x lot_size.
        self.assertEqual(leg["lots"], 2)
        self.assertEqual(leg["lot_size"], 75)
        self.assertEqual(leg["quantity"], 150)
        self.assertEqual(leg["signed_quantity"], 150)
        # And the contract's own constraints travel with it.
        self.assertEqual(leg["expiry"], "2026-10-29")
        self.assertEqual(leg["tick_size"], 0.05)
        self.assertEqual(plan.resolved["target_kind"], "target_futures")

    def test_a_short_futures_target_is_negative(self):
        self.contract()
        plan = self.compile(side="SELL")
        self.assertEqual(plan.resolved["legs"][0]["signed_quantity"], -150)

    def test_an_unknown_contract_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(instrument_token=999, tradingsymbol="NOPE")
        self.assertEqual(ctx.exception.reason_code, "CONTRACT_UNRESOLVED")

    def test_a_non_futures_instrument_refuses(self):
        """An equity mapping is not a futures contract, whatever the payload says."""
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract(kind="EQ")
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile()
        self.assertEqual(ctx.exception.reason_code, "CONTRACT_UNRESOLVED")
        self.assertEqual(ctx.exception.detail["instrument_type"], "EQ")


class ExpiryTests(FuturesCompilerTestCase):
    def test_an_undated_contract_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract(expiry=None)
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile()
        # A contract that cannot be rolled is a position nobody planned to hold.
        self.assertEqual(ctx.exception.reason_code, "EXPIRY_UNAVAILABLE")

    def test_the_expiry_is_carried_so_a_roll_can_be_planned(self):
        self.contract(expiry="2026-11-26")
        leg = self.compile().resolved["legs"][0]
        self.assertEqual(leg["expiry"], "2026-11-26")


class RollPeerTests(FuturesCompilerTestCase):
    def test_a_roll_freezes_its_peer_as_an_old_leg(self):
        self.contract()
        self.contract("NIFTY26SEPFUT", token=501)
        plan = self.compile(
            roll={
                "role": "open_new",
                "peer": {
                    "instrument_token": 501,
                    "exchange": "NFO",
                    "tradingsymbol": "NIFTY26SEPFUT",
                    "lots": 2,
                    "side": "BUY",
                    "reference_price": 24800.0,
                },
            }
        )

        old_leg = plan.resolved["old_legs"][0]
        self.assertEqual(old_leg["instrument_id"], "inst-NIFTY26SEPFUT")
        self.assertEqual(old_leg["lots"], 2)
        self.assertEqual(old_leg["lot_size"], 75)
        self.assertEqual(old_leg["signed_quantity"], 150)
        self.assertEqual(old_leg["reference_price"], 24800.0)

    def test_a_roll_peer_without_a_fresh_price_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        self.contract("NIFTY26SEPFUT", token=501)
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                roll={
                    "role": "open_new",
                    "peer": {
                        "instrument_token": 501,
                        "exchange": "NFO",
                        "tradingsymbol": "NIFTY26SEPFUT",
                        "lots": 1,
                        "side": "BUY",
                        "reference_price": None,
                    },
                }
            )

        self.assertEqual(ctx.exception.reason_code, "REFERENCE_PRICE_UNAVAILABLE")


class LotSizeTests(FuturesCompilerTestCase):
    def test_a_contract_without_a_lot_size_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract(lot=None)
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile()
        # Lots are the unit; without a lot size the position cannot be sized at all,
        # and guessing 1 would silently trade 75x the intended quantity.
        self.assertEqual(ctx.exception.reason_code, "CONTRACT_UNRESOLVED")

    def test_a_non_positive_lot_count_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        for lots in (0, -1):
            with self.assertRaises(ValidationRefusal) as ctx:
                self.compile(lots=lots)
            self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")

    def test_a_fractional_lot_count_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(lots=1.5)
        self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")


class FreezeTests(FuturesCompilerTestCase):
    def test_a_quantity_above_a_declared_freeze_limit_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(freeze_quantity=100)
        # Exceeding a freeze limit does not fail at the broker: it fails quietly
        # across multiple orders, which is how a position ends up the wrong size.
        self.assertEqual(ctx.exception.reason_code, "FREEZE_LIMIT_EXCEEDED")
        self.assertEqual(ctx.exception.detail["quantity"], 150)
        self.assertEqual(ctx.exception.detail["freeze_quantity"], 100)

    def test_a_quantity_at_the_freeze_limit_is_allowed(self):
        self.contract()
        leg = self.compile(freeze_quantity=150).resolved["legs"][0]
        self.assertEqual(leg["quantity"], 150)

    def test_live_autoslice_allows_a_declared_freeze_limit_to_be_exceeded(self):
        self.contract()
        leg = self.compile(
            freeze_quantity=100,
            execution_environment="live",
        ).resolved["legs"][0]
        self.assertEqual(leg["quantity"], 150)

    def test_paper_still_refuses_a_declared_freeze_limit(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(
                freeze_quantity=100,
                execution_environment="paper",
            )
        self.assertEqual(ctx.exception.reason_code, "FREEZE_LIMIT_EXCEEDED")

    def test_an_undeclared_freeze_limit_is_recorded_as_unavailable(self):
        """The catalog has no freeze column, so the gap is visible, not silent."""
        self.contract()
        leg = self.compile().resolved["legs"][0]
        self.assertIsNone(leg["freeze_quantity"])
        # Recorded rather than assumed: a consumer can see the axis was not checked.
        self.assertEqual(leg["freeze_source"], "unavailable")


class ContractSelectorTests(FuturesCompilerTestCase):
    """``{underlying, exchange, expiry}`` resolved against the pinned generation."""

    def golds(self):
        """Three MCX GOLD months, plus an option and an undated listing to ignore."""
        self.contract("GOLD26OCTFUT", token=601, expiry="2026-10-05", lot=100,
                      underlying="GOLD", exchange="MCX")
        self.contract("GOLD26NOVFUT", token=602, expiry="2026-11-05", lot=100,
                      underlying="GOLD", exchange="MCX")
        self.contract("GOLD26DECFUT", token=603, expiry="2026-12-05", lot=100,
                      underlying="GOLD", exchange="MCX")

    def selector(self, **overrides):
        values = {"underlying": "GOLD", "exchange": "MCX", "lots": 1,
                  "reference_price": 78000.0}
        values.update(overrides)
        return values

    def test_the_near_selector_resolves_the_nearest_listed_month(self):
        self.contract("GOLD26NOVFUT", token=602, expiry="2026-11-05", underlying="GOLD",
                      exchange="MCX")
        self.contract("GOLD26OCTFUT", token=601, expiry="2026-10-05", underlying="GOLD",
                      exchange="MCX")
        # Inserted newest-first: the selector orders by expiry, not by insert order.
        leg = self.compile(self.selector(expiry="near")).resolved["legs"][0]
        self.assertEqual(leg["tradingsymbol"], "GOLD26OCTFUT")
        self.assertEqual(leg["broker_token"], 601)
        self.assertEqual(leg["expiry"], "2026-10-05")

    def test_the_next_and_far_selectors_walk_the_listed_expiries(self):
        self.golds()
        near = self.compile(self.selector(expiry="near")).resolved["legs"][0]
        nxt = self.compile(self.selector(expiry="next")).resolved["legs"][0]
        far = self.compile(self.selector(expiry="far")).resolved["legs"][0]
        self.assertEqual(
            [near["tradingsymbol"], nxt["tradingsymbol"], far["tradingsymbol"]],
            ["GOLD26OCTFUT", "GOLD26NOVFUT", "GOLD26DECFUT"],
        )
        self.assertEqual([near["expiry"], nxt["expiry"], far["expiry"]],
                         ["2026-10-05", "2026-11-05", "2026-12-05"])

    def test_the_selector_is_frozen_beside_the_contract_it_resolved(self):
        """A plan reads back as both the intent and the month it meant."""
        self.golds()
        plan = self.compile(self.selector(expiry="next"))
        self.assertEqual(
            plan.resolved["contract_selector"],
            {"underlying": "GOLD", "exchange": "MCX", "expiry": "next"},
        )
        self.assertEqual(plan.logical["contract_selector"]["expiry"], "next")
        leg = plan.resolved["legs"][0]
        # The lot comes from the catalog, frozen on the leg like a named contract.
        self.assertEqual(leg["lot_size"], 100)
        self.assertEqual(leg["quantity"], 100)
        self.assertEqual(leg["underlying"], "GOLD")
        self.assertEqual(leg["exchange"], "MCX")

    def test_an_unknown_underlying_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.golds()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.selector(underlying="SILVER"))
        self.assertEqual(ctx.exception.reason_code, "CONTRACT_UNRESOLVED")
        self.assertEqual(ctx.exception.detail["underlying"], "SILVER")

    def test_a_rank_beyond_the_listed_expiries_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract("GOLD26OCTFUT", token=601, expiry="2026-10-05", underlying="GOLD",
                      exchange="MCX")
        # "next" cannot be answered by the October contract, which is "near".
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.selector(expiry="next"))
        self.assertEqual(ctx.exception.reason_code, "CONTRACT_UNRESOLVED")
        self.assertEqual(ctx.exception.detail["listed_expiries"], ["2026-10-05"])

    def test_a_retired_or_undated_listing_is_not_selectable(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract("GOLD26OCTFUT", token=601, expiry="2026-10-05", underlying="GOLD",
                      exchange="MCX", lifecycle="retired")
        self.contract("GOLD26NOVFUT", token=602, expiry=None, underlying="GOLD",
                      exchange="MCX")
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.selector())
        self.assertEqual(ctx.exception.reason_code, "CONTRACT_UNRESOLVED")

    def test_a_selector_only_sees_its_own_exchange(self):
        """NFO NIFTY listings are not GOLD contracts, and MCX GOLD is not NFO."""
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract("GOLD26OCTFUT", token=601, expiry="2026-10-05", underlying="GOLD",
                      exchange="MCX")
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.selector(exchange="NFO"))
        self.assertEqual(ctx.exception.reason_code, "CONTRACT_UNRESOLVED")

    def test_an_unknown_expiry_rank_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.golds()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.selector(expiry="nearest"))
        self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")
        self.assertEqual(ctx.exception.detail["allowed"], ["near", "next", "far"])

    def test_a_token_and_a_selector_together_refuse(self):
        """Whichever won, the other would be a silently ignored instruction."""
        from backend.strategies.compiler.base import ValidationRefusal

        self.golds()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.selector(instrument_token=601, tradingsymbol="GOLD26OCTFUT"))
        self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")

    def test_an_expiry_rank_without_an_underlying_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(expiry="near")
        self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")

    def test_a_named_contract_still_compiles_exactly_as_before(self):
        """The token path keeps its artifact shape: no selector key appears."""
        self.contract()
        plan = self.compile()
        self.assertNotIn("contract_selector", plan.resolved)
        self.assertNotIn("contract_selector", plan.logical)
        self.assertEqual(plan.resolved["legs"][0]["tradingsymbol"], "NIFTY26OCTFUT")


class RollCloseTests(FuturesCompilerTestCase):
    """A roll's close half declares an ABSOLUTE FLAT, not its own lot count.

    The executor sizes a ``close_old`` step from the strategy's attributed book,
    so a negative target of its own would close the position twice. The flat
    target is the shape the executor and the live lane already read as "close
    whatever is held", and it is the only place a zero-lot futures target is
    allowed to compile at all.
    """

    def close_payload(self, **overrides):
        values = self.payload(
            lots=0,
            side="SELL",
            roll={
                "role": "close_old",
                "peer": {
                    "instrument_token": 501,
                    "exchange": "NFO",
                    "tradingsymbol": "NIFTY26NOVFUT",
                    "lots": 1,
                    "side": "BUY",
                    "reference_price": 25100.0,
                },
            },
        )
        values.update(overrides)
        return values

    def test_a_flat_close_is_the_shape_the_executor_reads(self):
        self.contract()
        self.contract("NIFTY26NOVFUT", token=501, expiry="2026-11-26")
        plan = self.compile(self.close_payload())
        leg = plan.resolved["legs"][0]
        self.assertEqual(leg["lots"], 0)
        self.assertEqual(leg["quantity"], 0)
        self.assertEqual(leg["signed_quantity"], 0)
        # The leg carries no direction of its own: the executor derives the
        # closing side from which way the attributed book points.
        self.assertNotIn("side", leg)
        self.assertEqual(plan.logical["side"], "SELL")
        self.assertEqual(plan.resolved["roll"]["role"], "close_old")

    def test_a_zero_lot_target_still_refuses_outside_a_roll_close(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(lots=0)
        self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")

    def test_a_zero_lot_acquisition_refuses(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.contract()
        self.contract("NIFTY26NOVFUT", token=501, expiry="2026-11-26")
        with self.assertRaises(ValidationRefusal) as ctx:
            self.compile(self.close_payload(roll={"role": "open_new", "peer": {
                "instrument_token": 501,
                "exchange": "NFO",
                "tradingsymbol": "NIFTY26NOVFUT",
                "lots": 1,
                "side": "BUY",
                "reference_price": 25100.0,
            }}))
        self.assertEqual(ctx.exception.reason_code, "PAYLOAD_INVALID")


if __name__ == "__main__":
    unittest.main()
