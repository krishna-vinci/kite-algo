import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.account_scope import ParsedAccountScope, parse_account_scope  # noqa: E402


class AccountScopePolicyTests(unittest.TestCase):
    def test_parse_paper_scope_marks_mode_and_key(self):
        parsed = parse_account_scope("kite:paper-a")
        self.assertEqual(
            parsed,
            ParsedAccountScope(
                raw="kite:paper-a",
                normalized="kite:paper-a",
                mode="paper",
                paper_key="kite:paper-a",
                live_account_ref=None,
                broker_user_id=None,
            ),
        )

    def test_parse_legacy_paper_alias_is_supported(self):
        parsed = parse_account_scope("kite:test-paper")
        self.assertEqual(parsed.mode, "paper")
        self.assertEqual(parsed.paper_key, "kite:test-paper")

    def test_parse_live_scope_marks_live_account(self):
        parsed = parse_account_scope("kite:AB1234")
        self.assertEqual(parsed.mode, "live")
        self.assertIsNone(parsed.paper_key)
        self.assertEqual(parsed.live_account_ref, "kite:AB1234")
        self.assertEqual(parsed.broker_user_id, "AB1234")

    def test_live_scope_is_not_misclassified_for_substring_match(self):
        parsed = parse_account_scope("kite:newspaper01")
        self.assertEqual(parsed.mode, "live")
        self.assertEqual(parsed.broker_user_id, "newspaper01")

    def test_parse_scope_rejects_blank_value(self):
        with self.assertRaises(ValueError) as ctx:
            parse_account_scope("   ")
        self.assertIn("account_scope", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
