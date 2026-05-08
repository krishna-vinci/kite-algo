import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from broker_api.instruments.index_ingestion import (
    NIFTY50_MANUAL_BASELINES,
    NIFTYBANK_MANUAL_BASELINES,
    SOURCE_LIST_NIFTY50,
    SOURCE_LIST_NIFTYBANK,
    compute_baseline_ff_factor,
    compute_live_weight,
    compute_points_contribution,
    normalize_source_list,
    parse_constituent_csv,
    parse_top_holdings_csv,
)


class IndexIngestionHelpersTests(unittest.TestCase):
    def test_parse_constituent_csv_extracts_official_fields(self):
        rows = parse_constituent_csv(
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n"
            "ICICI Bank Ltd.,Financial Services,ICICIBANK,EQ,INE090A01021\n"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["symbol"], "HDFCBANK")
        self.assertEqual(rows[0]["series"], "EQ")
        self.assertEqual(rows[1]["isin_code"], "INE090A01021")

    def test_parse_top_holdings_csv(self):
        weights = parse_top_holdings_csv(
            "SYMBOL,SECURITY,WEIGHTAGE(%)\n"
            "HDFCBANK,HDFC BANK LTD.,11.25\n"
            "ICICIBANK,ICICI BANK LTD.,8.57\n"
        )
        self.assertEqual(weights, {"HDFCBANK": 11.25, "ICICIBANK": 8.57})

    def test_compute_baseline_ff_factor(self):
        self.assertAlmostEqual(compute_baseline_ff_factor(1245.26, 797.7), 1.5610630563, places=10)

    def test_compute_live_weight(self):
        self.assertAlmostEqual(compute_live_weight(11000.0, 1232.0), 11.2, places=4)

    def test_points_contribution_uses_index_previous_close(self):
        self.assertAlmostEqual(compute_points_contribution(24050.6, 0.18), 43.2911, places=4)

    def test_normalize_source_list_accepts_aliases(self):
        self.assertEqual(normalize_source_list("nifty50"), SOURCE_LIST_NIFTY50)
        self.assertEqual(normalize_source_list("banknifty"), SOURCE_LIST_NIFTYBANK)

    def test_default_nifty50_manual_baseline_contains_hdfcbank(self):
        self.assertEqual(NIFTY50_MANUAL_BASELINES["HDFCBANK"]["weight"], 11.52)
        self.assertEqual(NIFTY50_MANUAL_BASELINES["HDFCBANK"]["freefloat_marketcap"], 1245.26)

    def test_default_niftybank_manual_baseline_contains_hdfcbank(self):
        self.assertEqual(NIFTYBANK_MANUAL_BASELINES["HDFCBANK"]["weight"], 25.77)


if __name__ == "__main__":
    unittest.main()
