import pandas as pd

from fundamentals.features import compute_features_from_rows


def _rows():
    return pd.DataFrame([
        # Four trailing quarters -> TTM is the sum of the latest four periods.
        {"dataset": "quarterly", "period_end": "2025-06-30", "metric_key": "sales", "metric_name": "Sales", "numeric_value": 120.0},
        {"dataset": "quarterly", "period_end": "2025-03-31", "metric_key": "sales", "metric_name": "Sales", "numeric_value": 100.0},
        {"dataset": "quarterly", "period_end": "2024-12-31", "metric_key": "sales", "metric_name": "Sales", "numeric_value": 110.0},
        {"dataset": "quarterly", "period_end": "2024-09-30", "metric_key": "sales", "metric_name": "Sales", "numeric_value": 90.0},
        {"dataset": "quarterly", "period_end": "2024-06-30", "metric_key": "sales", "metric_name": "Sales", "numeric_value": 80.0},
        {"dataset": "quarterly", "period_end": "2025-06-30", "metric_key": "net profit", "metric_name": "Net Profit", "numeric_value": 15.0},
        {"dataset": "quarterly", "period_end": "2024-06-30", "metric_key": "net profit", "metric_name": "Net Profit", "numeric_value": 10.0},
        {"dataset": "quarterly", "period_end": "2025-06-30", "metric_key": "eps in rs", "metric_name": "EPS in Rs", "numeric_value": 2.5},
        {"dataset": "ratios", "period_end": "2025-06-30", "metric_key": "roce %", "metric_name": "ROCE %", "numeric_value": 18.0},
        {"dataset": "shareholding_quarterly", "period_end": "2025-06-30", "metric_key": "promoters", "metric_name": "Promoters", "numeric_value": 55.0},
        {"dataset": "shareholding_quarterly", "period_end": "2024-06-30", "metric_key": "promoters", "metric_name": "Promoters", "numeric_value": 52.0},
        {"dataset": "shareholding_quarterly", "period_end": "2025-06-30", "metric_key": "fiis", "metric_name": "FIIs", "numeric_value": 20.0},
        # Summary metrics carry no period_end; they feed market cap / price / P&E fields.
        {"dataset": "summary_metrics", "period_end": None, "metric_key": "market cap", "metric_name": "Market Cap", "numeric_value": 1234.0},
        {"dataset": "summary_metrics", "period_end": None, "metric_key": "current price", "metric_name": "Current Price", "numeric_value": 456.0},
        {"dataset": "summary_metrics", "period_end": None, "metric_key": "stock p/e", "metric_name": "Stock P/E", "numeric_value": 28.4},
        {"dataset": "summary_metrics", "period_end": None, "metric_key": "book value", "metric_name": "Book Value", "numeric_value": 210.0},
        {"dataset": "summary_metrics", "period_end": None, "metric_key": "dividend yield", "metric_name": "Dividend Yield", "numeric_value": 1.25},
        {"dataset": "summary_metrics", "period_end": None, "metric_key": "roce", "metric_name": "ROCE", "numeric_value": 14.5},
    ])


def test_ttm_is_sum_of_latest_four_quarters():
    features = compute_features_from_rows(_rows(), scraped_at="2026-09-05T00:00:00+00:00")
    # Latest four sales quarters: 90 + 110 + 100 + 120.
    assert features["ttm_revenue"] == 420.0


def test_quarterly_yoy_uses_same_period_prior_year():
    features = compute_features_from_rows(_rows(), scraped_at="2026-09-05T00:00:00+00:00")
    assert features["quarterly_revenue_yoy_pct"] == 50.0  # (120-80)/80
    assert features["quarterly_profit_yoy_pct"] == 50.0  # (15-10)/10


def test_missing_prior_year_quarter_yields_none_yoy():
    rows = _rows()
    profit_rows = rows.loc[~((rows["dataset"] == "quarterly") & (rows["metric_key"] == "net profit") & (rows["period_end"] == "2024-06-30"))]
    features = compute_features_from_rows(profit_rows, scraped_at="2026-09-05T00:00:00+00:00")
    assert features["quarterly_profit_yoy_pct"] is None


def test_ttm_requires_exactly_four_quarters():
    rows = _rows()
    three_quarters = rows.loc[~((rows["dataset"] == "quarterly") & (rows["metric_key"] == "sales") & (rows["period_end"].isin(["2024-06-30", "2024-09-30"])))]
    features = compute_features_from_rows(three_quarters, scraped_at="2026-09-05T00:00:00+00:00")
    assert features["ttm_revenue"] is None


def test_summary_ratio_fallback_when_ratios_table_lacks_metric():
    rows = _rows()
    ratios = rows.loc[~((rows["dataset"] == "ratios") & (rows["metric_key"] == "roce %"))]
    features = compute_features_from_rows(ratios, scraped_at="2026-09-05T00:00:00+00:00")
    assert features["latest_roce_pct"] == 14.5


def test_summary_metrics_map_to_company_level_fields():
    features = compute_features_from_rows(_rows(), scraped_at="2026-09-05T00:00:00+00:00", company_name="Example Ltd")
    assert features["company_name"] == "Example Ltd"
    assert features["market_cap_cr"] == 1234.0
    assert features["current_price"] == 456.0
    assert features["stock_pe"] == 28.4
    assert features["book_value"] == 210.0
    assert features["dividend_yield_pct"] == 1.25


def test_holdings_and_one_year_changes():
    features = compute_features_from_rows(_rows(), scraped_at="2026-09-05T00:00:00+00:00")
    assert features["promoter_holding_pct"] == 55.0
    assert features["fii_holding_pct"] == 20.0
    assert features["dii_holding_pct"] is None
    assert features["promoter_holding_change_1y_pct"] == 3.0
    assert features["fii_holding_change_1y_pct"] is None


def test_as_of_date_is_latest_period_end():
    features = compute_features_from_rows(_rows(), scraped_at="2026-09-05T00:00:00+00:00")
    assert features["as_of_date"] == "2025-06-30"
    assert features["scraped_at"] == "2026-09-05T00:00:00+00:00"


def test_empty_rows_yield_all_none_features():
    empty = pd.DataFrame(columns=["dataset", "period_end", "metric_key", "metric_name", "numeric_value"])
    features = compute_features_from_rows(empty, scraped_at="2026-09-05T00:00:00+00:00")
    assert features["ttm_revenue"] is None
    assert features["market_cap_cr"] is None
    assert features["as_of_date"] is None
    assert features["promoter_holding_pct"] is None
