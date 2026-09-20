"""Hedge fill gating: the confirmed-fill rule and its proportional ceiling (D-5).

The gap between "the hedge order was accepted" and "the hedge actually filled" is
where a bounded structure becomes unbounded: it looks hedged on paper because an
order exists, and is naked in reality because no position does. Every test below
pins one edge of that gap.
"""

from __future__ import annotations

import os
import unittest

from backend.options.protection.hedge_gate import (
    entry_blocked_by_protection,
    hedge_fill_gate,
    hedge_fill_timeout_seconds,
)


class ConfirmedFillTests(unittest.TestCase):
    def test_a_full_hedge_fill_releases_the_dependent_short(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=75,
            dependent_short_quantity=75, outcome="filled",
        )
        self.assertEqual(decision.released_quantity, 75)
        self.assertTrue(decision.fully_released)
        self.assertFalse(decision.action_required)

    def test_submission_alone_releases_nothing(self):
        """Accepted is not filled: no confirmed quantity, no release."""
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=0,
            dependent_short_quantity=75, outcome="pending",
        )
        self.assertEqual(decision.released_quantity, 0)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "no_confirmed_fill")
        # Holding is not an alarm: nothing has gone wrong yet.
        self.assertFalse(decision.action_required)

    def test_a_partial_fill_releases_at_most_the_proportion(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=50,
            dependent_short_quantity=75, outcome="partially_filled",
        )
        # 50/75 of 75 is exactly 50.
        self.assertEqual(decision.released_quantity, 50)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.detail["unreleased_quantity"], 25)

    def test_the_proportional_release_is_floored_never_rounded_up(self):
        """Rounding up would leave more short exposed than the hedge covers."""
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=50,
            dependent_short_quantity=100, outcome="partially_filled",
        )
        # 100 x 2/3 = 66.67 -> 66, not 67.
        self.assertEqual(decision.released_quantity, 66)
        self.assertLessEqual(
            decision.released_quantity * 75, 100 * 50
        )

    def test_the_release_can_never_exceed_the_hedge_that_exists(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=10,
            dependent_short_quantity=75, outcome="partially_filled",
        )
        self.assertLessEqual(decision.released_quantity, 10)

    def test_a_one_lot_hedge_does_not_release_a_fractional_short(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=4, confirmed_filled_quantity=1,
            dependent_short_quantity=1, outcome="partially_filled",
        )
        # 1 x 1/4 = 0.25 -> 0. Releasing a unit the hedge does not cover is naked.
        self.assertEqual(decision.released_quantity, 0)


class BlockingOutcomeTests(unittest.TestCase):
    def test_a_rejected_hedge_releases_nothing_and_needs_an_operator(self):
        for outcome in ("rejected", "cancelled", "timeout"):
            with self.subTest(outcome=outcome):
                decision = hedge_fill_gate(
                    required_hedge_quantity=75, confirmed_filled_quantity=0,
                    dependent_short_quantity=75, outcome=outcome,
                )
                self.assertEqual(decision.released_quantity, 0)
                self.assertTrue(decision.action_required)
                self.assertTrue(decision.blocked)
                self.assertIn(outcome, decision.reason)

    def test_a_rejection_after_a_partial_fill_still_releases_nothing(self):
        """The outcome governs: a hedge that died mid-fill is not a hedge."""
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=50,
            dependent_short_quantity=75, outcome="rejected",
        )
        self.assertEqual(decision.released_quantity, 0)
        self.assertTrue(decision.action_required)

    def test_an_explicit_timeout_releases_nothing(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=50,
            dependent_short_quantity=75, outcome="pending",
            elapsed_seconds=31, timeout_seconds=30,
        )
        self.assertEqual(decision.released_quantity, 0)
        self.assertTrue(decision.action_required)
        self.assertEqual(decision.reason, "hedge_fill_timeout")

    def test_within_the_timeout_a_partial_fill_still_releases_its_share(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=50,
            dependent_short_quantity=75, outcome="partially_filled",
            elapsed_seconds=10, timeout_seconds=30,
        )
        self.assertEqual(decision.released_quantity, 50)
        self.assertFalse(decision.action_required)

    def test_the_timeout_defaults_to_thirty_seconds_and_is_configurable(self):
        os.environ.pop("OPTION_HEDGE_FILL_TIMEOUT_SECONDS", None)
        self.assertEqual(hedge_fill_timeout_seconds(), 30)
        os.environ["OPTION_HEDGE_FILL_TIMEOUT_SECONDS"] = "5"
        try:
            self.assertEqual(hedge_fill_timeout_seconds(), 5)
            decision = hedge_fill_gate(
                required_hedge_quantity=75, confirmed_filled_quantity=0,
                dependent_short_quantity=75, outcome="pending",
                elapsed_seconds=6,
            )
            self.assertTrue(decision.action_required)
        finally:
            os.environ.pop("OPTION_HEDGE_FILL_TIMEOUT_SECONDS", None)

    def test_a_nonsense_timeout_falls_back_to_the_default(self):
        for value in ("0", "-1", "not-a-number"):
            os.environ["OPTION_HEDGE_FILL_TIMEOUT_SECONDS"] = value
            try:
                self.assertEqual(hedge_fill_timeout_seconds(), 30, value)
            finally:
                os.environ.pop("OPTION_HEDGE_FILL_TIMEOUT_SECONDS", None)

    def test_a_full_fill_is_never_timed_out(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=75,
            dependent_short_quantity=75, outcome="filled",
            elapsed_seconds=300, timeout_seconds=30,
        )
        # Once the hedge is complete, waiting longer is irrelevant.
        self.assertEqual(decision.released_quantity, 75)
        self.assertFalse(decision.action_required)


class DegenerateInputTests(unittest.TestCase):
    def test_nothing_required_releases_nothing(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=0, confirmed_filled_quantity=0,
            dependent_short_quantity=75,
        )
        self.assertEqual(decision.released_quantity, 0)
        self.assertEqual(decision.reason, "nothing_to_release")

    def test_no_dependent_short_releases_nothing(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=75,
            dependent_short_quantity=0,
        )
        self.assertEqual(decision.released_quantity, 0)

    def test_negative_inputs_are_clamped_rather_than_trusted(self):
        decision = hedge_fill_gate(
            required_hedge_quantity=-75, confirmed_filled_quantity=-10,
            dependent_short_quantity=-5,
        )
        self.assertEqual(decision.released_quantity, 0)


class PartialEntryProtectionTests(unittest.TestCase):
    def test_a_triggered_guard_blocks_new_legs(self):
        self.assertTrue(entry_blocked_by_protection({"status": "triggered"}))
        self.assertFalse(entry_blocked_by_protection({"status": "monitoring"}))
        self.assertFalse(entry_blocked_by_protection({}))

    def test_blocking_new_legs_is_not_blocking_exits(self):
        """A guard that stopped exits would trap the half-built structure."""
        decision = hedge_fill_gate(
            required_hedge_quantity=75, confirmed_filled_quantity=75,
            dependent_short_quantity=75, outcome="filled",
        )
        # The gate is unaware of protection state on purpose: exits reduce risk and
        # must remain available whatever the guard decided.
        self.assertEqual(decision.released_quantity, 75)


if __name__ == "__main__":
    unittest.main()
