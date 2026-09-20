"""The structure-aware exit builder: shorts first, hedges against proof (D-6).

The ordering is the invariant. A structure's long legs bound its liability, so
releasing a hedge before its short is closed opens a naked window — and that window
is precisely the maximum-loss shape the structure was built to avoid. The tests pin
the ordering, the proof requirement, and the two ways a hedge could be released
without the short behind it actually being gone.
"""

from __future__ import annotations

import unittest

from backend.options.protection.exit_builder import build_structure_exit_orders


def short(symbol, quantity, **over):
    values = {"tradingsymbol": symbol, "side": "SELL", "quantity": quantity,
              "exchange": "NFO", "product": "NRML", "underlying": "NIFTY",
              "expiry": "2026-10-29"}
    values.update(over)
    return values


def long_leg(symbol, quantity, **over):
    values = {"tradingsymbol": symbol, "side": "BUY", "quantity": quantity,
              "exchange": "NFO", "product": "NRML", "underlying": "NIFTY",
              "expiry": "2026-10-29"}
    values.update(over)
    return values


class OrderingTests(unittest.TestCase):
    def test_shorts_precede_hedges(self):
        """When a call produces both a close and a release, the close is first."""
        legs = [
            short("SHORT-A", -75, structure_leg_id="A"),
            short("SHORT-B", -75, structure_leg_id="B"),
            long_leg("HEDGE", 75, structure_leg_id="A", hedge_for="SHORT-A"),
        ]
        orders, detail = build_structure_exit_orders(
            legs, closed_short_quantities={"SHORT-A": 75, "SHORT-B": 0}
        )
        # The list order IS the contract: closes first, releases after.
        self.assertEqual([order["tradingsymbol"] for order in orders], ["SHORT-B", "HEDGE"])
        self.assertEqual(orders[0]["transaction_type"], "BUY")  # buying back a short
        self.assertEqual(orders[1]["transaction_type"], "SELL")  # selling the hedge
        self.assertGreater(detail["naked_short_quantity"], 0)

    def test_an_entirely_flat_short_releases_its_hedge(self):
        legs = [short("SHORT", -75), long_leg("HEDGE", 75)]
        orders, _ = build_structure_exit_orders(legs, closed_short_quantities={"SHORT": 75})
        self.assertEqual([order["tradingsymbol"] for order in orders], ["HEDGE"])


class ProofTests(unittest.TestCase):
    def test_a_submitted_short_releases_no_hedge(self):
        """Submission is not closure, so the protection stays where it is."""
        legs = [short("SHORT", -75), long_leg("HEDGE", 75)]
        orders, detail = build_structure_exit_orders(legs, closed_short_quantities={})
        self.assertEqual([order["tradingsymbol"] for order in orders], ["SHORT"])
        self.assertEqual(detail["released_hedges"], 0)
        self.assertEqual(
            [row["reason"] for row in detail["withheld_hedges"]], ["short_not_proven_closed"]
        )
        self.assertEqual(detail["naked_short_quantity"], 75)

    def test_a_partially_closed_short_releases_only_the_covered_hedge(self):
        legs = [short("SHORT", -75), long_leg("HEDGE", 75)]
        orders, detail = build_structure_exit_orders(
            legs, closed_short_quantities={"SHORT": 30}
        )
        by_symbol = {order["tradingsymbol"]: order for order in orders}
        # The short closes the remaining 45; the hedge releases only the 30 proven.
        self.assertEqual(by_symbol["SHORT"]["quantity"], 45)
        self.assertEqual(by_symbol["HEDGE"]["quantity"], 30)
        self.assertEqual(
            [row["reason"] for row in detail["withheld_hedges"]], ["short_only_partially_closed"]
        )
        # Remaining hedges are preserved while any short exposure remains.
        self.assertEqual(detail["withheld_hedges"][0]["quantity"], 45)
        self.assertGreater(detail["naked_short_quantity"], 0)

    def test_a_proven_flat_short_is_not_re_closed(self):
        legs = [short("SHORT", -75), long_leg("HEDGE", 75)]
        orders, _ = build_structure_exit_orders(legs, closed_short_quantities={"SHORT": 75})
        self.assertNotIn("SHORT", [order["tradingsymbol"] for order in orders])

    def test_an_over_reported_closure_cannot_release_more_than_the_hedge(self):
        legs = [short("SHORT", -75), long_leg("HEDGE", 40)]
        orders, _ = build_structure_exit_orders(
            legs, closed_short_quantities={"SHORT": 999}
        )
        by_symbol = {order["tradingsymbol"]: order for order in orders}
        self.assertEqual(by_symbol["HEDGE"]["quantity"], 40)

    def test_a_hedge_for_another_expiry_covers_nothing(self):
        legs = [
            short("SHORT", -75, expiry="2026-10-29"),
            long_leg("HEDGE", 75, expiry="2026-11-26"),
        ]
        orders, detail = build_structure_exit_orders(
            legs, closed_short_quantities={"SHORT": 75}
        )
        # The short is already proven flat so there is nothing to close — and the
        # far-expiry hedge answers to nothing here, so it is NOT released either.
        self.assertEqual(orders, [])
        self.assertEqual(detail["released_hedges"], 0)
        self.assertEqual(
            [row["reason"] for row in detail["withheld_hedges"]], ["short_not_proven_closed"]
        )

    def test_a_targeted_hedge_answers_only_for_its_own_short(self):
        legs = [
            short("SHORT-A", -75, structure_leg_id="A"),
            short("SHORT-B", -75, structure_leg_id="B"),
            long_leg("HEDGE-A", 75, structure_leg_id="A", hedge_for="SHORT-A"),
        ]
        orders, detail = build_structure_exit_orders(
            legs, closed_short_quantities={"SHORT-A": 75, "SHORT-B": 0}
        )
        symbols = [order["tradingsymbol"] for order in orders]
        # A is closed and its hedge released; B is untouched and keeps its own.
        self.assertIn("HEDGE-A", symbols)
        self.assertEqual(detail["naked_short_quantity"], 75)


class ShapeTests(unittest.TestCase):
    def test_orders_carry_the_shape_the_runtime_submits(self):
        legs = [short("SHORT", -75)]
        orders, _ = build_structure_exit_orders(legs, closed_short_quantities={})
        order = orders[0]
        for field in ("exchange", "tradingsymbol", "transaction_type", "variety",
                      "product", "order_type", "quantity"):
            self.assertIn(field, order)
        self.assertEqual(order["order_type"], "MARKET")
        self.assertEqual(order["quantity"], 75)

    def test_a_limit_exit_carries_its_price(self):
        legs = [short("SHORT", -75, exit_order_type="LIMIT", exit_price=123.5)]
        orders, _ = build_structure_exit_orders(legs, closed_short_quantities={})
        self.assertEqual(orders[0]["price"], 123.5)

    def test_the_product_override_applies(self):
        legs = [short("SHORT", -75)]
        orders, _ = build_structure_exit_orders(
            legs, closed_short_quantities={}, product_override="MIS"
        )
        self.assertEqual(orders[0]["product"], "MIS")

    def test_flat_legs_are_skipped(self):
        orders, _ = build_structure_exit_orders(
            [short("SHORT", 0), long_leg("HEDGE", 0)], closed_short_quantities={}
        )
        self.assertEqual(orders, [])

    def test_an_empty_structure_is_not_an_error(self):
        orders, detail = build_structure_exit_orders([], closed_short_quantities={})
        self.assertEqual(orders, [])
        self.assertEqual(detail["naked_short_quantity"], 0)


if __name__ == "__main__":
    unittest.main()
