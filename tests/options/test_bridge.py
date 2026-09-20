"""The vocabulary bridge, tested in both directions (D-3).

One adapter, and the tests exist to keep it one: the compiler's vocabulary must
appear outside ``options/strategy/`` only through this module, and every metric must
survive a round trip. A mapping that worked one way and not the other would produce
rules that fire on paper and not in a report — or, worse, a rule the runtime never
fires at all.
"""

from __future__ import annotations

import pathlib
import unittest

from backend.options.protection.bridge import (
    BridgeRefusal,
    bridge_rule,
    bridge_rules,
    compiler_metric_for,
    report_trigger,
    runtime_metric_for,
)
from backend.options.strategy.models import MetricKind, RuleRole


class ForwardMappingTests(unittest.TestCase):
    def test_every_compiler_metric_has_a_runtime_carrier(self):
        for kind in MetricKind:
            with self.subTest(metric=kind):
                self.assertTrue(runtime_metric_for(kind))

    def test_the_three_known_metrics_map_where_expected(self):
        self.assertEqual(runtime_metric_for(MetricKind.INDEX_PRICE), "index_ltp")
        self.assertEqual(
            runtime_metric_for(MetricKind.COMBINED_PREMIUM_POINTS), "combined_premium"
        )
        self.assertEqual(
            runtime_metric_for(MetricKind.BASKET_MTM_RUPEES), "basket_mtm_rupees"
        )

    def test_an_unknown_metric_refuses_rather_than_mapping_to_nothing(self):
        with self.assertRaises(BridgeRefusal) as ctx:
            runtime_metric_for("invented_metric")
        self.assertEqual(ctx.exception.reason_code, "RULE_NOT_BRIDGEABLE")
        self.assertIn("invented_metric", ctx.exception.detail["compiler_metric"])


class ReverseMappingTests(unittest.TestCase):
    def test_every_mapping_survives_the_round_trip(self):
        """The bidirectionality is the point: a rule and its report must agree."""
        for kind in MetricKind:
            runtime = runtime_metric_for(kind)
            self.assertEqual(compiler_metric_for(runtime), kind.value)

    def test_an_unknown_runtime_metric_refuses(self):
        with self.assertRaises(BridgeRefusal):
            compiler_metric_for("invented_runtime_metric")


class RuleTranslationTests(unittest.TestCase):
    def test_a_rule_translates_into_the_runtime_shape(self):
        bridged = bridge_rule(
            {
                "metric": MetricKind.INDEX_PRICE,
                "direction": "below",
                "threshold": 24500,
                "role": RuleRole.EMERGENCY_GUARD,
            }
        )
        self.assertEqual(bridged.runtime_metric, "index_ltp")
        self.assertEqual(bridged.compiler_metric, "index_price")
        self.assertEqual(bridged.direction, "below")
        self.assertEqual(bridged.threshold, 24500.0)
        self.assertEqual(bridged.role, "emergency_guard")
        # The runtime gets the shape it evaluates, and the author's vocabulary
        # rides along for the report.
        self.assertEqual(bridged.as_dict()["metric"], "index_ltp")

    def test_the_compiler_metric_accepts_a_raw_string_too(self):
        bridged = bridge_rule(
            {"metric": "combined_premium_points", "direction": "above",
             "threshold": 120.5, "role": "profit_target"}
        )
        self.assertEqual(bridged.runtime_metric, "combined_premium")

    def test_an_unknown_direction_refuses(self):
        with self.assertRaises(BridgeRefusal) as ctx:
            bridge_rule(
                {"metric": MetricKind.INDEX_PRICE, "direction": "sideways",
                 "threshold": 1, "role": "hard_stop"}
            )
        self.assertIn("sideways", str(ctx.exception.detail))

    def test_an_unknown_role_refuses(self):
        with self.assertRaises(BridgeRefusal):
            bridge_rule(
                {"metric": MetricKind.INDEX_PRICE, "direction": "above",
                 "threshold": 1, "role": "invented_role"}
            )

    def test_an_unreadable_threshold_refuses(self):
        with self.assertRaises(BridgeRefusal):
            bridge_rule(
                {"metric": MetricKind.INDEX_PRICE, "direction": "above",
                 "threshold": "not-a-number", "role": "hard_stop"}
            )

    def test_a_non_object_rule_refuses(self):
        with self.assertRaises(BridgeRefusal):
            bridge_rule("metric=index_price")

    def test_a_list_of_rules_translates_together(self):
        bridged = bridge_rules([
            {"metric": MetricKind.INDEX_PRICE, "direction": "below", "threshold": 1,
             "role": "hard_stop"},
            {"metric": MetricKind.BASKET_MTM_RUPEES, "direction": "below", "threshold": -5000,
             "role": "hard_stop"},
        ])
        self.assertEqual([row.runtime_metric for row in bridged],
                         ["index_ltp", "basket_mtm_rupees"])

    def test_a_non_list_refuses(self):
        with self.assertRaises(BridgeRefusal):
            bridge_rules({"metric": MetricKind.INDEX_PRICE})


class ReportTests(unittest.TestCase):
    def test_a_runtime_trigger_reports_back_in_compiler_vocabulary(self):
        reported = report_trigger({"metric": "index_ltp", "rule": "structure_guard"})
        self.assertEqual(reported["compiler_metric"], "index_price")
        # The runtime fields are preserved: the report ADDS a vocabulary, it does
        # not replace the runtime's own.
        self.assertEqual(reported["metric"], "index_ltp")
        self.assertEqual(reported["rule"], "structure_guard")

    def test_an_untranslatable_trigger_says_so_rather_than_dropping_the_field(self):
        reported = report_trigger({"metric": "something_else"})
        self.assertIsNone(reported["compiler_metric"])
        self.assertEqual(reported["metric"], "something_else")


class SingleAdapterTests(unittest.TestCase):
    def test_the_compiler_vocabulary_appears_only_through_this_bridge(self):
        """The bridge is the FIRST place MetricKind leaves options/strategy/."""
        root = pathlib.Path("backend")
        offenders = []
        for path in root.rglob("*.py"):
            parts = path.parts
            if "strategy" in parts and "options" in parts:
                continue  # the vocabulary's home
            if path.name == "bridge.py" and "options" in parts and "protection" in parts:
                continue  # the adapter itself
            if path.name == "__pycache__":
                continue
            text = path.read_text()
            for token in ("MetricKind", "combined_premium_points", "basket_mtm_rupees"):
                if token in text and "runtime_metric" not in text:
                    offenders.append(f"{path}: {token}")
        self.assertEqual(
            offenders, [],
            "the compiler vocabulary must only cross into the runtime through bridge.py",
        )


if __name__ == "__main__":
    unittest.main()
