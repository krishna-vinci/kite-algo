"""The per-strategy risk policy: the declaration and its arithmetic (B2.5).

Three things have to be true before admission can rely on this module. The
effective policy must take the MINIMUM of every level that states a limit and
the intersection of every allow-list, because a ceiling that could loosen a
declaration is not a ceiling. The family classifier must read the frozen legs
alone and name shapes by their equality structure, because an allow-list that
admits by accident is not an allow-list. And the worst-case loss must include
the frozen entry premium.
"""

from __future__ import annotations

import unittest

from backend.strategies.risk_policy import (
    RiskPolicyError,
    classify_structure_family,
    effective_risk_policy,
    frozen_protection_stop,
    operator_risk_policy,
    platform_risk_ceiling,
    validate_risk_policy,
    worst_case_loss_inr,
)


def leg(side, option_type, strike, quantity, ratio=1, price=None, expiry="2026-10-29"):
    result = {
        "option_type": option_type,
        "side": side,
        "strike": float(strike),
        "expiry": expiry,
        "ratio": ratio,
        "quantity": int(quantity),
        "signed_quantity": int(quantity) if side == "BUY" else -int(quantity),
    }
    if price is not None:
        result["reference_price"] = float(price)
    return result


class ValidateTests(unittest.TestCase):
    def test_none_is_no_declaration_and_empty_is_a_permissive_one(self):
        self.assertIsNone(validate_risk_policy(None))
        self.assertEqual(validate_risk_policy({}), {})

    def test_a_malformed_field_is_refused_by_name(self):
        with self.assertRaises(RiskPolicyError) as ctx:
            validate_risk_policy({"max_loss_inr": -1})
        self.assertIn("risk_policy.max_loss_inr", str(ctx.exception))

        with self.assertRaises(RiskPolicyError) as ctx:
            validate_risk_policy({"allowed_structure_families": ["not_a_family"]})
        self.assertIn("allowed_structure_families", str(ctx.exception))

        with self.assertRaises(RiskPolicyError) as ctx:
            validate_risk_policy({"naked_permitted": "yes"})
        self.assertIn("naked_permitted", str(ctx.exception))

        with self.assertRaises(RiskPolicyError) as ctx:
            validate_risk_policy({"notional_limit_inr": 100, "surprise": 1})
        self.assertIn("surprise", str(ctx.exception))

    def test_a_valid_declaration_is_normalised(self):
        declared = validate_risk_policy(
            {
                "max_loss_inr": 25000,
                "notional_limit_inr": 500000,
                "protection": {"stop_required": True},
                "expiry_policy": "exit_before_cutoff",
                "allowed_structure_families": ["vertical_spread", "custom"],
                "naked_permitted": False,
            }
        )
        self.assertEqual(declared["max_loss_inr"], 25000.0)
        self.assertEqual(declared["expiry_policy"], ["exit_before_cutoff"])
        self.assertEqual(
            declared["allowed_structure_families"], ["vertical_spread", "custom"]
        )


class EffectivePolicyTests(unittest.TestCase):
    def test_every_numeric_limit_takes_the_minimum_and_sets_intersect(self):
        declared = {
            "max_loss_inr": 100.0,
            "notional_limit_inr": 500.0,
            "allowed_structure_families": ["vertical_spread", "straddle"],
            "expiry_policy": ["exit_before_cutoff", "allow_cash_settlement"],
            "naked_permitted": True,
        }
        operator = {"max_loss_inr": 60.0, "notional_limit_inr": 900.0}
        ceiling = {
            "notional_limit_inr": 400.0,
            "allowed_structure_families": ["vertical_spread"],
            "naked_permitted": False,
        }

        effective = effective_risk_policy(declared, operator, ceiling)

        self.assertEqual(effective["max_loss_inr"], 60.0)
        self.assertEqual(effective["notional_limit_inr"], 400.0)
        self.assertEqual(effective["allowed_structure_families"], ["vertical_spread"])
        # An intersection is reported in a canonical order, not the caller's.
        self.assertEqual(
            effective["expiry_policy"], ["allow_cash_settlement", "exit_before_cutoff"]
        )
        # A permission is granted only where every stating level grants it.
        self.assertFalse(effective["naked_permitted"])

    def test_an_absent_level_never_widens_a_stated_one(self):
        declared = {"naked_permitted": True, "max_loss_inr": 100.0}
        effective = effective_risk_policy(declared, None, {})
        self.assertTrue(effective["naked_permitted"])
        self.assertEqual(effective["max_loss_inr"], 100.0)
        self.assertIsNone(effective["notional_limit_inr"])

        # An unset policy is distinguished from an explicit deny.
        self.assertIsNone(effective_risk_policy({}, {}, {})["naked_permitted"])

    def test_a_stop_requirement_tightens_when_any_level_demands_it(self):
        declared = {"protection": {"stop_required": False}}
        ceiling = {"protection": {"stop_required": True}}
        effective = effective_risk_policy(declared, None, ceiling)
        self.assertTrue(effective["protection"]["stop_required"])

    def test_the_operator_axes_only_carry_real_structure_ceilings(self):
        operator = operator_risk_policy(
            {
                "allocation_inr": 1000.0,
                "per_instrument_notional_inr": 400.0,
                "gross_notional_inr": 800.0,
                "daily_loss_budget_inr": 250.0,
            }
        )
        self.assertEqual(operator["notional_limit_inr"], 800.0)
        self.assertEqual(operator["max_loss_inr"], 250.0)
        self.assertNotIn("allocation_inr", operator)


class PlatformCeilingTests(unittest.TestCase):
    def test_absent_ceiling_is_no_ceiling_and_a_typo_does_not_ground_everything(self):
        self.assertEqual(platform_risk_ceiling({}), {})
        self.assertEqual(
            platform_risk_ceiling({"ADMISSION_RISK_MAX_LOSS_INR": "not-a-number"}), {}
        )

    def test_a_configured_ceiling_is_read_and_narrowed_to_known_values(self):
        ceiling = platform_risk_ceiling(
            {
                "ADMISSION_RISK_MAX_LOSS_INR": "5000",
                "ADMISSION_RISK_NOTIONAL_LIMIT_INR": "1000000",
                "ADMISSION_RISK_ALLOWED_STRUCTURE_FAMILIES": "vertical_spread,diagonal",
                "ADMISSION_RISK_NAKED_PERMITTED": "false",
            }
        )
        self.assertEqual(ceiling["max_loss_inr"], 5000.0)
        self.assertEqual(ceiling["notional_limit_inr"], 1000000.0)
        self.assertEqual(
            ceiling["allowed_structure_families"], ["vertical_spread", "diagonal"]
        )
        self.assertFalse(ceiling["naked_permitted"])


class ClassifierTests(unittest.TestCase):
    def test_single_legs_are_named_by_side(self):
        self.assertEqual(
            classify_structure_family([leg("BUY", "CE", 25000, 75)]), "long_single"
        )
        self.assertEqual(
            classify_structure_family([leg("SELL", "PE", 25000, 75)]), "short_single"
        )

    def test_straddles_strangles_and_verticals_are_separated_by_equality(self):
        straddle = [leg("SELL", "CE", 25000, 75), leg("SELL", "PE", 25000, 75)]
        strangle = [leg("SELL", "CE", 25200, 75), leg("SELL", "PE", 24800, 75)]
        vertical = [leg("BUY", "CE", 25000, 75), leg("SELL", "CE", 26000, 75)]
        self.assertEqual(classify_structure_family(straddle), "straddle")
        self.assertEqual(classify_structure_family(strangle), "strangle")
        self.assertEqual(classify_structure_family(vertical), "vertical_spread")

    def test_condor_and_butterfly_are_separated_by_the_short_strikes(self):
        condor = [
            leg("SELL", "PE", 24500, 75),
            leg("SELL", "CE", 25500, 75),
            leg("BUY", "PE", 24400, 75),
            leg("BUY", "CE", 25600, 75),
        ]
        butterfly = [
            leg("SELL", "PE", 25000, 75),
            leg("SELL", "CE", 25000, 75),
            leg("BUY", "PE", 24900, 75),
            leg("BUY", "CE", 25100, 75),
        ]
        self.assertEqual(classify_structure_family(condor), "iron_condor")
        self.assertEqual(classify_structure_family(butterfly), "iron_butterfly")

    def test_butterfly_calendar_diagonal_and_ratio_families_are_named(self):
        butterfly = [
            leg("BUY", "CE", 25000, 75),
            leg("SELL", "CE", 25500, 150),
            leg("BUY", "CE", 26000, 75),
        ]
        calendar = [
            leg("BUY", "CE", 25000, 75, expiry="2026-10-29"),
            leg("SELL", "CE", 25000, 75, expiry="2026-11-26"),
        ]
        diagonal = [
            leg("BUY", "CE", 25000, 75, expiry="2026-10-29"),
            leg("SELL", "CE", 26000, 75, expiry="2026-11-26"),
        ]
        ratio = [
            leg("BUY", "CE", 25000, 75),
            leg("SELL", "CE", 26000, 150),
        ]
        self.assertEqual(classify_structure_family(butterfly), "butterfly")
        self.assertEqual(classify_structure_family(calendar), "calendar")
        self.assertEqual(classify_structure_family(diagonal), "diagonal")
        self.assertEqual(classify_structure_family(ratio), "ratio_spread")

    def test_anything_unrecognised_is_custom_not_the_nearest_shape(self):
        ratio = [leg("BUY", "CE", 25000, 75), leg("SELL", "CE", 26000, 150)]
        self.assertEqual(classify_structure_family(ratio), "ratio_spread")
        three = [
            leg("SELL", "CE", 25000, 75),
            leg("BUY", "CE", 25500, 75),
            leg("BUY", "PE", 24500, 75),
        ]
        self.assertEqual(classify_structure_family(three), "custom")
        self.assertEqual(classify_structure_family([]), "custom")


class WorstCaseLossTests(unittest.TestCase):
    def test_a_long_call_can_lose_only_its_entry_premium(self):
        self.assertEqual(
            worst_case_loss_inr([leg("BUY", "CE", 25000, 75, price=100)]), 7500.0
        )

    def test_a_credit_spread_loses_width_minus_credit(self):
        spread = [
            leg("SELL", "CE", 25000, 75, price=100),
            leg("BUY", "CE", 26000, 75, price=80),
        ]
        self.assertEqual(worst_case_loss_inr(spread), 73500.0)

    def test_a_debit_spread_loses_the_net_debit(self):
        spread = [
            leg("BUY", "CE", 25000, 75, price=100),
            leg("SELL", "CE", 26000, 75, price=80),
        ]
        self.assertEqual(worst_case_loss_inr(spread), 1500.0)

    def test_a_short_put_is_bounded_by_its_strike(self):
        # A naked PUT loses at most strike x quantity, so it is bounded and the
        # numeric ceiling still applies.
        self.assertEqual(
            worst_case_loss_inr([leg("SELL", "PE", 25000, 75, price=100)]), 1867500.0
        )

    def test_an_iron_condor_is_bounded_by_its_wings(self):
        condor = [
            leg("SELL", "PE", 24500, 75, price=60),
            leg("SELL", "CE", 25500, 75, price=60),
            leg("BUY", "PE", 24400, 75, price=50),
            leg("BUY", "CE", 25600, 75, price=50),
        ]
        self.assertEqual(worst_case_loss_inr(condor), 6000.0)

    def test_a_butterfly_loses_the_net_debit(self):
        butterfly = [
            leg("BUY", "CE", 25000, 75, price=100),
            leg("SELL", "CE", 25500, 150, price=50),
            leg("BUY", "CE", 26000, 75, price=20),
        ]
        self.assertEqual(worst_case_loss_inr(butterfly), 1500.0)

    def test_a_ratio_spread_with_a_net_short_call_is_unbounded(self):
        ratio = [
            leg("BUY", "CE", 25000, 75, price=100),
            leg("SELL", "CE", 26000, 150, price=80),
        ]
        self.assertIsNone(worst_case_loss_inr(ratio))

    def test_a_net_short_call_is_unbounded(self):
        self.assertIsNone(worst_case_loss_inr([leg("SELL", "CE", 25000, 75, price=100)]))

    def test_an_unreadable_leg_is_never_proof_of_a_bound(self):
        broken = [{"side": "BUY", "quantity": 75}]  # no option type or strike
        self.assertIsNone(worst_case_loss_inr(broken))


class FrozenProtectionStopTests(unittest.TestCase):
    def test_a_declared_stop_is_recognised_and_an_absent_one_is_not(self):
        self.assertFalse(frozen_protection_stop(None))
        self.assertFalse(frozen_protection_stop({"naked": True}))
        self.assertFalse(
            frozen_protection_stop(
                {"rules": [{"metric": "index_ltp", "action": "notify"}]}
            )
        )
        self.assertTrue(
            frozen_protection_stop(
                {
                    "rules": [
                        {
                            "metric": "index_ltp",
                            "operator": "lte",
                            "threshold": 22000,
                            "action": "exit",
                        }
                    ]
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
