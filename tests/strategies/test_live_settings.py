"""The hosted live deployment switch is opt-in, never "not false".

A safety switch that treats an unrecognised value as ENABLED fails in the one
way that matters: a typo arms live trading. These tests pin the explicit truthy
allowlist and the unknown-value default.
"""

from __future__ import annotations

import unittest


class HostedLiveEnabledTests(unittest.TestCase):
    def _enabled(self, raw):
        from backend.strategies.live_settings import hosted_live_enabled

        return hosted_live_enabled({"HOSTED_LIVE_ENABLED": raw})

    def test_only_the_explicit_truthy_spellings_enable_live(self):
        for raw in ("1", "true", "TRUE", "True", "yes", "YES", "on", "ON", " true "):
            with self.subTest(raw=raw):
                self.assertTrue(self._enabled(raw))

    def test_everything_else_is_disabled_including_typos(self):
        for raw in (
            "",
            "   ",
            "0",
            "false",
            "False",
            "no",
            "off",
            "disabled",
            "flase",
            "ture",
            "tru",
            "enabled",
            "y",
            "2",
            "live",
        ):
            with self.subTest(raw=raw):
                self.assertFalse(self._enabled(raw))

    def test_unset_is_disabled(self):
        from backend.strategies.live_settings import hosted_live_enabled

        self.assertFalse(hosted_live_enabled({}))

    def test_the_disabled_detail_names_the_setting_and_surface(self):
        from backend.strategies.live_settings import hosted_live_disabled_detail

        detail = hosted_live_disabled_detail(plan_id="plan-1", surface="plan_execute")
        self.assertEqual(detail["setting"], "HOSTED_LIVE_ENABLED")
        self.assertEqual(detail["plan_id"], "plan-1")
        self.assertEqual(detail["surface"], "plan_execute")


if __name__ == "__main__":
    unittest.main()
