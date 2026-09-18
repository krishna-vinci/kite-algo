"""The CNC portfolio compiler: weights → quantity deltas (D-1).

A ``target_weights`` plan names fractions, not quantities. Turning a fraction into
an order means asking three questions the plan alone cannot answer: what is the
strategy's capital, what does it already hold, and how much cash would all the
buys need at once. This compiler answers them and refuses by name when the answer
is "it does not fit" — before any order exists.

The two rules that make the arithmetic honest:

* **Sells before buys.** A rebalance that sells to fund its buys must not assume
  the proceeds arrived; the gross cash reservation covers every buy upfront.
* **Full-snapshot deltas.** A member omitted from the payload is target ZERO, so
  the strategy sells it — and an instrument outside the pinned scope is untouched,
  because its absence carries no instruction.
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the new tables

G1 = "11111111-1111-1111-1111-111111111111"


class WeightsCompilerTestCase(unittest.TestCase):
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
                CREATE TABLE public.instrument_catalog_records (
                    instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,
                    lifecycle_status TEXT NOT NULL DEFAULT 'active', lot_size INTEGER,
                    current_generation_id TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_generations (
                    id TEXT PRIMARY KEY, status TEXT, published_at TEXT
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

        from backend.strategies.attribution_models import (
            Strategy,
            StrategyAdmissionPolicy,
            StrategyPositionProjection,
            StrategyProjectionState,
        )

        _Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyAdmissionPolicy.__table__,
                StrategyPositionProjection.__table__,
                StrategyProjectionState.__table__,
            ],
        )
        self.factory = sessionmaker(bind=self.engine)
        self.compiler = self._compiler()

        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
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

    def _compiler(self):
        from backend.strategies.compiler.weights import WeightsPortfolioCompiler

        return WeightsPortfolioCompiler(session_factory=self.factory)

    # -- fixtures -----------------------------------------------------------

    def instrument(self, symbol, *, lot=1, lifecycle="active"):
        instrument_id = f"inst-{symbol}"
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, lot_size, "
                    " current_generation_id) VALUES (:id, 'NSE', :symbol, :lifecycle, :lot, :gen)"
                ),
                {"id": instrument_id, "symbol": symbol, "lot": lot, "lifecycle": lifecycle,
                 "gen": G1},
            )
            session.commit()
        return instrument_id

    def book(self, symbol, quantity, *, product="CNC"):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES ('kite:A', 'stg-A', 'paper', 'canonical', :key, :key, :product, "
                    " 100, 'NSE', :symbol, :qty, 1)"
                ),
                {"key": f"inst-{symbol}", "symbol": symbol, "qty": int(quantity),
                 "product": product},
            )
            session.commit()

    def policy(self, allocation):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_admission_policies "
                    "(strategy_id, account_id, allocation_inr, updated_by) "
                    "VALUES ('stg-A', 'kite:A', :alloc, 'app:o')"
                ),
                {"alloc": allocation},
            )
            session.commit()

    def plan(self, legs, **overrides):
        values = {
            "plan_id": "plan-1",
            "strategy_id": "stg-A",
            "account_id": "kite:A",
            "plan_hash": "h" * 64,
            "pinned_catalog_generation": G1,
            "resolved_plan": {"legs": legs},
        }
        values.update(overrides)
        return values

    def leg(self, symbol, weight, price):
        return {
            "instrument_id": f"inst-{symbol}",
            "exchange": "NSE",
            "tradingsymbol": symbol,
            "broker_exchange": "NSE",
            "broker_symbol": symbol,
            "broker_token": 100,
            "product": "CNC",
            "target_weight": weight,
            "reference_price": price,
        }


class DeltaTests(WeightsCompilerTestCase):
    def test_target_quantity_is_weight_times_capital_over_price(self):
        self.instrument("RELIANCE")
        self.policy(100000.0)
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        leg = compilation.legs[0]
        # 0.5 x 100_000 / 100 = 500 shares, and nothing held, so the delta is a buy.
        self.assertEqual(leg["target_quantity"], 500)
        self.assertEqual(leg["current_quantity"], 0)
        self.assertEqual(leg["delta"], 500)
        self.assertEqual(leg["side"], "BUY")

    def test_delta_is_measured_against_the_strategy_book(self):
        self.instrument("RELIANCE")
        self.policy(100000.0)
        self.book("RELIANCE", 200)
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        leg = compilation.legs[0]
        # Only the difference trades: 500 target - 200 held = 300.
        self.assertEqual(leg["current_quantity"], 200)
        self.assertEqual(leg["delta"], 300)

    def test_holding_above_target_produces_a_sell(self):
        self.instrument("RELIANCE")
        self.policy(100000.0)
        self.book("RELIANCE", 800)
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        leg = compilation.legs[0]
        self.assertEqual(leg["delta"], -300)
        self.assertEqual(leg["side"], "SELL")

    def test_in_scope_omission_sells_to_zero(self):
        """A member omitted from the payload is target ZERO, not untouched.

        The plan arrives as Phase 3 builds it: EVERY in-scope member is a leg, and
        an omitted one carries an explicit zero weight. So "omitted" is a real
        instruction the compiler can act on, not an absent row.
        """
        self.instrument("RELIANCE")
        self.instrument("INFY")
        self.policy(100000.0)
        self.book("INFY", 50)
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0), self.leg("INFY", 0.0, 100.0)]),
            execution_environment="paper",
        )
        by_symbol = {leg["tradingsymbol"]: leg for leg in compilation.legs}
        # The omitted member carries an explicit zero target and is sold.
        self.assertEqual(by_symbol["INFY"]["target_quantity"], 0)
        self.assertEqual(by_symbol["INFY"]["delta"], -50)
        self.assertEqual(by_symbol["INFY"]["side"], "SELL")

    def test_out_of_scope_instrument_is_untouched(self):
        """An instrument outside the pinned revision carries no instruction."""
        self.instrument("RELIANCE")
        self.policy(100000.0)
        self.book("WIPRO", 75)  # held, but not a member of this plan's scope
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        symbols = [leg["tradingsymbol"] for leg in compilation.legs]
        self.assertEqual(symbols, ["RELIANCE"])

    def test_deltas_are_floored_to_lot_size(self):
        self.instrument("NIFTYBEES", lot=75)
        self.policy(100000.0)
        compilation = self.compiler.compile(
            self.plan([self.leg("NIFTYBEES", 0.5, 100.0)]), execution_environment="paper"
        )
        leg = compilation.legs[0]
        # 500 target, lot 75: 500 -> 450, and a non-zero target is never floored away.
        self.assertEqual(leg["target_quantity"], 450)


class OrderingAndCashTests(WeightsCompilerTestCase):
    def test_sells_come_before_buys(self):
        self.instrument("RELIANCE")
        self.instrument("INFY")
        self.policy(100000.0)
        self.book("RELIANCE", 500)  # will be sold to zero
        compilation = self.compiler.compile(
            self.plan([self.leg("INFY", 0.5, 100.0), self.leg("RELIANCE", 0.0, 100.0)]),
            execution_environment="paper",
        )
        sides = [leg["side"] for leg in compilation.legs]
        self.assertEqual(sides, ["SELL", "BUY"])
        self.assertEqual(compilation.legs[0]["tradingsymbol"], "RELIANCE")
        self.assertEqual(compilation.legs[1]["tradingsymbol"], "INFY")

    def test_gross_cash_reservation_covers_every_buy_upfront(self):
        self.instrument("RELIANCE")
        self.instrument("INFY")
        self.policy(100000.0)
        compilation = self.compiler.compile(
            self.plan(
                [self.leg("RELIANCE", 0.3, 100.0), self.leg("INFY", 0.3, 200.0)]
            ),
            execution_environment="paper",
        )
        # 300 x 100 + 150 x 200 = 30_000 + 30_000 = 60_000, all of it reserved before
        # the first buy is released.
        self.assertEqual(compilation.gross_cash_reservation_inr, 60000.0)
        self.assertEqual(compilation.buy_notional_inr, 60000.0)

    def test_sell_proceeds_are_not_counted_before_they_fill(self):
        self.instrument("RELIANCE")
        self.instrument("INFY")
        self.policy(100000.0)
        self.book("RELIANCE", 500)  # 50_000 of sell proceeds available only later
        compilation = self.compiler.compile(
            self.plan([self.leg("INFY", 0.5, 100.0), self.leg("RELIANCE", 0.0, 100.0)]),
            execution_environment="paper", available_cash_inr=10000.0,
        )
        # The buys need 50_000 in cash and only 10_000 is actually there: the
        # 50_000 the sells will raise does not count until it has.
        self.assertTrue(compilation.refused)
        self.assertEqual(compilation.refusal_reason, "INSUFFICIENT_PORTFOLIO_CASH")
        self.assertEqual(compilation.gross_cash_reservation_inr, 50000.0)

    def test_a_balanced_rebalance_fits(self):
        self.instrument("RELIANCE")
        self.instrument("INFY")
        self.policy(200000.0)
        self.book("RELIANCE", 500)
        compilation = self.compiler.compile(
            self.plan([self.leg("INFY", 0.5, 100.0), self.leg("RELIANCE", 0.0, 100.0)]),
            execution_environment="paper", available_cash_inr=100000.0,
        )
        self.assertFalse(compilation.refused, compilation.refusal_detail)


class RefusalTests(WeightsCompilerTestCase):
    def test_missing_policy_refuses(self):
        self.instrument("RELIANCE")
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        self.assertTrue(compilation.refused)
        self.assertEqual(compilation.refusal_reason, "ADMISSION_POLICY_MISSING")

    def test_missing_reference_price_refuses(self):
        self.instrument("RELIANCE")
        self.policy(100000.0)
        leg = dict(self.leg("RELIANCE", 0.5, 100.0))
        leg.pop("reference_price")
        compilation = self.compiler.compile(
            self.plan([leg]), execution_environment="paper"
        )
        self.assertEqual(compilation.refusal_reason, "REFERENCE_PRICE_UNAVAILABLE")

    def test_allocation_shortfall_refuses_by_name(self):
        """Weights summing above 1.0 ask for more capital than exists."""
        self.instrument("RELIANCE")
        self.instrument("INFY")
        self.policy(1000.0)
        compilation = self.compiler.compile(
            self.plan(
                [self.leg("RELIANCE", 0.75, 100.0), self.leg("INFY", 0.75, 100.0)]
            ),
            execution_environment="paper",
        )
        # 7 shares each at 100 is 1400 of buys against a 1000 allocation.
        self.assertTrue(compilation.refused)
        self.assertEqual(compilation.refusal_reason, "GROSS_NOTIONAL_EXCEEDED")
        self.assertEqual(compilation.gross_cash_reservation_inr, 1400.0)

    def test_a_small_allocation_scales_the_target_instead_of_refusing(self):
        """The allocation IS the capital being invested, so it sizes positions."""
        self.instrument("RELIANCE")
        self.policy(1000.0)
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        self.assertFalse(compilation.refused, compilation.refusal_detail)
        # 0.5 x 1000 / 100 = 5 shares, funded entirely within the allocation.
        self.assertEqual(compilation.legs[0]["target_quantity"], 5)
        self.assertEqual(compilation.gross_cash_reservation_inr, 500.0)

    def test_instrument_notional_axis_refuses(self):
        self.instrument("RELIANCE")
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_admission_policies "
                    "(strategy_id, account_id, allocation_inr, per_instrument_notional_inr, "
                    " updated_by) VALUES ('stg-A', 'kite:A', 1000000, 1000, 'app:o')"
                )
            )
            session.commit()
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        self.assertEqual(compilation.refusal_reason, "INSTRUMENT_NOTIONAL_EXCEEDED")

    def test_max_open_instruments_axis_refuses(self):
        self.instrument("RELIANCE")
        self.instrument("INFY")
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_admission_policies "
                    "(strategy_id, account_id, allocation_inr, max_open_instruments, updated_by) "
                    "VALUES ('stg-A', 'kite:A', 1000000, 1, 'app:o')"
                )
            )
            session.commit()
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.3, 100.0), self.leg("INFY", 0.3, 100.0)]),
            execution_environment="paper",
        )
        self.assertEqual(compilation.refusal_reason, "MAX_OPEN_INSTRUMENTS_EXCEEDED")

    def test_a_flat_no_op_plan_is_not_refused(self):
        self.instrument("RELIANCE")
        self.policy(100000.0)
        self.book("RELIANCE", 500)
        compilation = self.compiler.compile(
            self.plan([self.leg("RELIANCE", 0.5, 100.0)]), execution_environment="paper"
        )
        # Target already met: no legs to execute, and that is success, not refusal.
        self.assertFalse(compilation.refused)
        self.assertEqual(compilation.legs, [])
        self.assertEqual(compilation.gross_cash_reservation_inr, 0.0)


if __name__ == "__main__":
    unittest.main()
