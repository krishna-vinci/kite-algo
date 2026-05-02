import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from strategies.option_strategy.compiler import compile_option_strategy_preview  # noqa: E402


class OptionStrategyCompilerTests(unittest.TestCase):
    def test_directional_preview_prefers_index_and_separates_optional_mtm(self):
        preview = compile_option_strategy_preview(
            underlying="NIFTY",
            template_id="buy_call",
            strategy_type="single_leg",
            current_spot=22540,
            legs=[
                {
                    "instrument_token": 101,
                    "tradingsymbol": "NIFTY24APR22500CE",
                    "strike": 22500,
                    "option_type": "CE",
                    "transaction_type": "BUY",
                    "ltp": 100.0,
                    "lot_size": 50,
                    "lots": 1,
                }
            ],
        )

        self.assertEqual(preview.inferred_family.value, "directional")
        self.assertEqual(preview.primary_metric.value, "index_price")
        self.assertEqual(preview.inputs["index_lower_boundary"].label, "index stoploss")
        self.assertEqual(preview.inputs["index_upper_boundary"].label, "index target")
        self.assertEqual(preview.inputs["basket_mtm_stoploss"].unit, "₹")
        self.assertEqual(preview.estimated_entry_cost_rupees, 5000.0)
        self.assertTrue(any(rule.metric.value == "index_price" for rule in preview.rules))

    def test_neutral_short_premium_uses_combined_premium_plus_index_emergency(self):
        preview = compile_option_strategy_preview(
            underlying="NIFTY",
            template_id="short_straddle",
            strategy_type="straddle",
            current_spot=22500,
            legs=[
                {
                    "instrument_token": 201,
                    "tradingsymbol": "NIFTY24APR22500CE",
                    "strike": 22500,
                    "option_type": "CE",
                    "transaction_type": "SELL",
                    "ltp": 120.0,
                    "lot_size": 50,
                    "lots": 1,
                },
                {
                    "instrument_token": 202,
                    "tradingsymbol": "NIFTY24APR22500PE",
                    "strike": 22500,
                    "option_type": "PE",
                    "transaction_type": "SELL",
                    "ltp": 115.0,
                    "lot_size": 50,
                    "lots": 1,
                },
            ],
        )

        self.assertEqual(preview.inferred_family.value, "neutral-short-premium")
        self.assertEqual(preview.primary_metric.value, "combined_premium_points")
        self.assertEqual(preview.emergency_metric.value, "index_price")
        self.assertEqual(preview.combined_premium_entry_type, "credit")
        self.assertTrue(preview.inputs["index_lower_boundary"].required)
        self.assertEqual(preview.inputs["combined_premium_target"].value, 82.0)
        self.assertIsNone(preview.inputs["combined_premium_stoploss"].value)
        self.assertTrue(any(rule.role.value == "emergency_guard" for rule in preview.rules))
        self.assertTrue(any(rule.metric.value == "combined_premium_points" for rule in preview.rules))

    def test_long_vol_preview_defaults_to_combined_target_and_stop(self):
        preview = compile_option_strategy_preview(
            underlying="NIFTY",
            template_id="long_straddle",
            strategy_type="straddle",
            current_spot=22500,
            legs=[
                {
                    "instrument_token": 301,
                    "tradingsymbol": "NIFTY24APR22500CE",
                    "strike": 22500,
                    "option_type": "CE",
                    "transaction_type": "BUY",
                    "ltp": 90.0,
                    "lot_size": 50,
                    "lots": 1,
                },
                {
                    "instrument_token": 302,
                    "tradingsymbol": "NIFTY24APR22500PE",
                    "strike": 22500,
                    "option_type": "PE",
                    "transaction_type": "BUY",
                    "ltp": 80.0,
                    "lot_size": 50,
                    "lots": 1,
                },
            ],
        )

        self.assertEqual(preview.inferred_family.value, "long-vol")
        self.assertEqual(preview.combined_premium_entry_type, "debit")
        self.assertEqual(preview.inputs["combined_premium_target"].value, 68.0)
        self.assertEqual(preview.inputs["combined_premium_stoploss"].value, 42.0)
        self.assertTrue(all(rule.metric.value == "combined_premium_points" for rule in preview.rules[:2]))

    def test_mixed_expiry_structure_prefers_mtm(self):
        preview = compile_option_strategy_preview(
            underlying="NIFTY",
            template_id=None,
            strategy_type="manual",
            current_spot=22500,
            legs=[
                {
                    "instrument_token": 401,
                    "tradingsymbol": "NIFTY24APR22500CE",
                    "strike": 22500,
                    "option_type": "CE",
                    "transaction_type": "SELL",
                    "ltp": 120.0,
                    "lot_size": 50,
                    "lots": 1,
                    "expiry_key": "2026-04-30",
                },
                {
                    "instrument_token": 402,
                    "tradingsymbol": "NIFTY24MAY22500CE",
                    "strike": 22500,
                    "option_type": "CE",
                    "transaction_type": "BUY",
                    "ltp": 160.0,
                    "lot_size": 50,
                    "lots": 1,
                    "expiry_key": "2026-05-28",
                },
            ],
        )

        self.assertEqual(preview.inferred_family.value, "premium-managed-structure")
        self.assertEqual(preview.primary_metric.value, "basket_mtm_rupees")
        self.assertTrue(preview.inputs["basket_mtm_stoploss"].required)
        self.assertTrue(any("MTM-led" in warning for warning in preview.warnings))

    def test_iron_condor_hint_stays_iron_condor_instead_of_falling_back_to_short_strangle(self):
        preview = compile_option_strategy_preview(
            underlying="NIFTY",
            template_id=None,
            strategy_type="manual",
            current_spot=22500,
            legs=[
                {"instrument_token": 501, "tradingsymbol": "NIFTY24APR22600CE", "strike": 22600, "option_type": "CE", "transaction_type": "SELL", "ltp": 45.0, "lot_size": 50, "lots": 1},
                {"instrument_token": 502, "tradingsymbol": "NIFTY24APR22700CE", "strike": 22700, "option_type": "CE", "transaction_type": "BUY", "ltp": 20.0, "lot_size": 50, "lots": 1},
                {"instrument_token": 503, "tradingsymbol": "NIFTY24APR22400PE", "strike": 22400, "option_type": "PE", "transaction_type": "SELL", "ltp": 42.0, "lot_size": 50, "lots": 1},
                {"instrument_token": 504, "tradingsymbol": "NIFTY24APR22300PE", "strike": 22300, "option_type": "PE", "transaction_type": "BUY", "ltp": 18.0, "lot_size": 50, "lots": 1},
            ],
        )

        self.assertEqual(preview.inferred_structure, "iron_condor")
        self.assertEqual(preview.inferred_family.value, "neutral-short-premium")


if __name__ == "__main__":
    unittest.main()
