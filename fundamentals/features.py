"""Per-symbol derived fundamental features computed from stored metric rows.

One row of rows comes from ``public.fundamentals_metrics`` for a single
(symbol, statement_scope): long-format records with columns
``dataset, period_end, metric_key, metric_name, numeric_value``. Derived
features are stored, never computed at read time.
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

SUMMARY_METRIC_FEATURES = {
    "market_cap_cr": "market cap",
    "current_price": "current price",
    "stock_pe": "stock p/e",
    "book_value": "book value",
    "dividend_yield_pct": "dividend yield",
}


def compute_features_from_rows(rows: pd.DataFrame, *, scraped_at: str, company_name: str | None = None) -> dict:
    """Compute the derived feature dict for one symbol's stored metric rows."""
    if rows is None or rows.empty:
        features: dict = {
            "company_name": company_name,
            "as_of_date": None,
            "scraped_at": scraped_at,
        }
        for column in SUMMARY_METRIC_FEATURES:
            features[column] = None
        for column in (
            "latest_quarter_revenue", "latest_quarter_net_profit", "latest_quarter_eps",
            "ttm_revenue", "ttm_net_profit", "quarterly_revenue_yoy_pct", "quarterly_profit_yoy_pct",
            "latest_roce_pct", "latest_roe_pct",
            "promoter_holding_pct", "fii_holding_pct", "dii_holding_pct",
            "promoter_holding_change_1y_pct", "fii_holding_change_1y_pct", "dii_holding_change_1y_pct",
        ):
            features[column] = None
        return features

    rows = rows.copy()
    rows["period_end"] = pd.to_datetime(rows["period_end"], errors="coerce")

    def dataset(name: str) -> pd.DataFrame:
        subset = rows.loc[rows["dataset"] == name]
        return subset

    def latest(ds: pd.DataFrame, prefixes: Iterable[str]) -> float | None:
        row = _latest_metric_row(ds, prefixes)
        if row is None:
            return None
        _record_as_of(row["period_end"])
        value = row["numeric_value"]
        return None if pd.isna(value) else float(value)

    def ttm(ds: pd.DataFrame, prefixes: Iterable[str]) -> float | None:
        if ds.empty:
            return None
        subset = _prefix_filter(ds, prefixes).sort_values("period_end").tail(4)
        if len(subset) < 4:
            return None
        _record_as_of(subset.iloc[-1]["period_end"])
        values = subset["numeric_value"].dropna()
        if len(values) < 4:
            return None
        return float(values.sum())

    def yoy(ds: pd.DataFrame, prefixes: Iterable[str]) -> float | None:
        row = _latest_metric_row(ds, prefixes)
        if row is None or pd.isna(row["period_end"]):
            return None
        prior_period = row["period_end"] - pd.DateOffset(years=1)
        prior = _prefix_filter(ds, prefixes).loc[_prefix_filter(ds, prefixes)["period_end"] == prior_period]
        if prior.empty:
            return None
        prior_value = prior.sort_values("period_end").iloc[-1]["numeric_value"]
        latest_value = row["numeric_value"]
        if pd.isna(prior_value) or pd.isna(latest_value) or float(prior_value) == 0.0:
            return None
        return round(((float(latest_value) - float(prior_value)) / abs(float(prior_value))) * 100.0, 4)

    def one_year_change(ds: pd.DataFrame, prefix: str) -> float | None:
        row = _latest_metric_row(ds, (prefix,))
        if row is None or pd.isna(row["period_end"]):
            return None
        prior_period = row["period_end"] - pd.DateOffset(years=1)
        subset = _prefix_filter(ds, (prefix,))
        prior = subset.loc[subset["period_end"] == prior_period]
        if prior.empty:
            return None
        prior_value = prior.sort_values("period_end").iloc[-1]["numeric_value"]
        if pd.isna(prior_value) or pd.isna(row["numeric_value"]):
            return None
        return float(row["numeric_value"]) - float(prior_value)

    features = {"company_name": company_name, "as_of_date": None, "scraped_at": scraped_at}

    def _record_as_of(period_end) -> None:
        if pd.isna(period_end):
            return
        candidate = period_end.date().isoformat()
        if features["as_of_date"] is None or candidate > features["as_of_date"]:
            features["as_of_date"] = candidate

    def _prefix_filter(ds: pd.DataFrame, prefixes: Iterable[str]) -> pd.DataFrame:
        prefixes = tuple(prefixes)
        return ds.loc[ds["metric_key"].apply(lambda key: any(key.startswith(prefix) for prefix in prefixes))].copy()

    def _latest_metric_row(ds: pd.DataFrame, prefixes: Iterable[str]) -> pd.Series | None:
        if ds.empty:
            return None
        subset = _prefix_filter(ds, prefixes).sort_values("period_end")
        if subset.empty:
            return None
        return subset.iloc[-1]

    def _summary_metric(prefix: str) -> float | None:
        summary = dataset("summary_metrics")
        if summary.empty:
            return None
        subset = summary.loc[summary["metric_key"].str.startswith(prefix)]
        if subset.empty:
            return None
        value = subset.iloc[-1]["numeric_value"]
        return None if pd.isna(value) else float(value)

    for column, prefix in SUMMARY_METRIC_FEATURES.items():
        features[column] = _summary_metric(prefix)

    quarterly = dataset("quarterly")
    ratios = dataset("ratios")
    shareholding = dataset("shareholding_quarterly")

    features["latest_quarter_revenue"] = latest(quarterly, ("sales", "revenue"))
    features["latest_quarter_net_profit"] = latest(quarterly, ("net profit",))
    features["latest_quarter_eps"] = latest(quarterly, ("eps in rs",))
    features["ttm_revenue"] = ttm(quarterly, ("sales", "revenue"))
    features["ttm_net_profit"] = ttm(quarterly, ("net profit",))
    features["quarterly_revenue_yoy_pct"] = yoy(quarterly, ("sales", "revenue"))
    features["quarterly_profit_yoy_pct"] = yoy(quarterly, ("net profit",))
    # Ratios fall back to the page summary when the ratios table has no row.
    features["latest_roce_pct"] = latest(ratios, ("roce %",))
    if features["latest_roce_pct"] is None:
        features["latest_roce_pct"] = _summary_metric("roce")
    features["latest_roe_pct"] = latest(ratios, ("roe %",))
    if features["latest_roe_pct"] is None:
        features["latest_roe_pct"] = _summary_metric("roe")
    features["promoter_holding_pct"] = latest(shareholding, ("promoters",))
    features["fii_holding_pct"] = latest(shareholding, ("fiis",))
    features["dii_holding_pct"] = latest(shareholding, ("diis",))
    features["promoter_holding_change_1y_pct"] = one_year_change(shareholding, "promoters")
    features["fii_holding_change_1y_pct"] = one_year_change(shareholding, "fiis")
    features["dii_holding_change_1y_pct"] = one_year_change(shareholding, "diis")
    return features
