from __future__ import annotations

from html import unescape
from io import StringIO
import re
from typing import Any

import pandas as pd

SUMMARY_METRIC_PATTERN = re.compile(
    r'<li class="flex flex-space-between" data-source="default">.*?<span class="name">(.*?)</span>.*?<span class="nowrap value">(.*?)</span>',
    re.DOTALL,
)

SECTION_MATCHERS: dict[str, tuple[str, ...]] = {
    "quarterly": ("net profit", "eps in rs"),
    "profit_loss": ("dividend payout", "net profit"),
    "balance_sheet": ("total assets", "total liabilities"),
    "cash_flow": ("free cash flow", "cash from operating activity"),
    "ratios": ("debtor days",),
    "shareholding_quarterly": ("promoters", "fiis", "diis"),
    "shareholding_yearly": ("promoters", "fiis", "diis"),
}


def parse_screener_company_page(fetch_result: Any) -> dict[str, pd.DataFrame]:
    html = fetch_result.html
    company_name = _extract_company_name(html) or fetch_result.company_slug
    nse_symbol = _extract_code(html, "NSE")
    bse_code = _extract_code(html, "BSE")
    tables = _read_tables(html)

    pages = pd.DataFrame(
        [
            {
                "tradingsymbol": fetch_result.requested_symbol,
                "company_name": company_name,
                "company_slug": fetch_result.company_slug,
                "statement_scope": fetch_result.statement_scope,
                "source_url": fetch_result.source_url,
                "scraped_at": fetch_result.fetched_at,
                "nse_symbol": nse_symbol,
                "bse_code": bse_code,
                "page_html": fetch_result.html,
            }
        ]
    )

    summary_metrics = _parse_summary_metrics(
        html,
        tradingsymbol=fetch_result.requested_symbol,
        company_name=company_name,
        company_slug=fetch_result.company_slug,
        statement_scope=fetch_result.statement_scope,
        source_url=fetch_result.source_url,
        scraped_at=fetch_result.fetched_at,
    )

    table_frames = _parse_statement_tables(
        tables=tables,
        tradingsymbol=fetch_result.requested_symbol,
        company_name=company_name,
        company_slug=fetch_result.company_slug,
        statement_scope=fetch_result.statement_scope,
        source_url=fetch_result.source_url,
        scraped_at=fetch_result.fetched_at,
    )
    table_frames["company_pages"] = pages
    table_frames["summary_metrics"] = summary_metrics
    return table_frames


def ensure_screener_parser_ready() -> None:
    try:
        _read_tables("<table><tr><th>Metric</th><th>Mar 2026</th></tr><tr><td>Sales</td><td>10</td></tr></table>")
    except Exception as exc:  # pragma: no cover - environment specific
        raise RuntimeError(
            "Screener HTML table parsing is not available. Install a supported HTML parser dependency such as lxml."
        ) from exc


def _parse_statement_tables(
    *,
    tables: list[pd.DataFrame],
    tradingsymbol: str,
    company_name: str,
    company_slug: str,
    statement_scope: str,
    source_url: str,
    scraped_at: str,
) -> dict[str, pd.DataFrame]:
    used_indexes: set[int] = set()
    frames: dict[str, pd.DataFrame] = {}

    for dataset_name, required_terms in SECTION_MATCHERS.items():
        index = _find_table_index(tables, required_terms=required_terms, used_indexes=used_indexes)
        if index is None:
            frames[dataset_name] = _empty_statement_frame()
            continue
        used_indexes.add(index)
        section = dataset_name.replace("_quarterly", "").replace("_yearly", "")
        frequency = "quarterly" if "quarterly" in dataset_name else "yearly"
        if dataset_name in {"quarterly", "profit_loss", "balance_sheet", "cash_flow", "ratios"}:
            frequency = "quarterly" if dataset_name == "quarterly" else "annual"
        frames[dataset_name] = _table_to_long_frame(
            tables[index],
            tradingsymbol=tradingsymbol,
            company_name=company_name,
            company_slug=company_slug,
            statement_scope=statement_scope,
            statement_section=section,
            statement_frequency=frequency,
            source_url=source_url,
            scraped_at=scraped_at,
        )
    return frames


def _find_table_index(tables: list[pd.DataFrame], *, required_terms: tuple[str, ...], used_indexes: set[int]) -> int | None:
    best_index: int | None = None
    best_score = 0
    for index, table in enumerate(tables):
        if index in used_indexes or table.empty:
            continue
        row_labels = {_normalize_metric_name(value) for value in table.iloc[:, 0].tolist()}
        score = sum(any(term in label for label in row_labels) for term in required_terms)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index


def _table_to_long_frame(
    table: pd.DataFrame,
    *,
    tradingsymbol: str,
    company_name: str,
    company_slug: str,
    statement_scope: str,
    statement_section: str,
    statement_frequency: str,
    source_url: str,
    scraped_at: str,
) -> pd.DataFrame:
    frame = table.copy()
    frame.columns = [_clean_text(column) for column in frame.columns]
    metric_column = frame.columns[0]
    records: list[dict[str, Any]] = []

    for _, row in frame.iterrows():
        metric_name = _clean_text(row[metric_column])
        metric_key = _normalize_metric_name(metric_name)
        for period_label in frame.columns[1:]:
            value_text = _clean_text(row[period_label])
            records.append(
                {
                    "tradingsymbol": tradingsymbol,
                    "company_name": company_name,
                    "company_slug": company_slug,
                    "statement_scope": statement_scope,
                    "statement_section": statement_section,
                    "statement_frequency": statement_frequency,
                    "period_label": period_label,
                    "period_end": _parse_period_label(period_label),
                    "metric_name": metric_name,
                    "metric_key": metric_key,
                    "value_text": value_text,
                    "numeric_value": _parse_numeric_value(value_text),
                    "source_url": source_url,
                    "scraped_at": scraped_at,
                }
            )

    return pd.DataFrame(records)


def _parse_summary_metrics(
    html: str,
    *,
    tradingsymbol: str,
    company_name: str,
    company_slug: str,
    statement_scope: str,
    source_url: str,
    scraped_at: str,
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for metric_name_html, value_html in SUMMARY_METRIC_PATTERN.findall(html):
        metric_name = _clean_text(metric_name_html)
        value_text = _clean_text(value_html)
        records.append(
            {
                "tradingsymbol": tradingsymbol,
                "company_name": company_name,
                "company_slug": company_slug,
                "statement_scope": statement_scope,
                "metric_name": metric_name,
                "metric_key": _normalize_metric_name(metric_name),
                "value_text": value_text,
                "numeric_value": _parse_numeric_value(value_text),
                "source_url": source_url,
                "scraped_at": scraped_at,
            }
        )
    return pd.DataFrame(records)


def _read_tables(html: str) -> list[pd.DataFrame]:
    errors: list[Exception] = []
    for flavor in ("lxml", "bs4"):
        try:
            return pd.read_html(StringIO(html), displayed_only=False, flavor=flavor)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[-1]
    return []


def _extract_company_name(html: str) -> str | None:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    if not match:
        return None
    return _clean_text(match.group(1))


def _extract_code(html: str, prefix: str) -> str | None:
    match = re.search(fr">\s*{prefix}:\s*([^<]+)<", html, re.IGNORECASE)
    if not match:
        return None
    return _clean_text(match.group(1))


def _normalize_metric_name(value: Any) -> str:
    normalized = _clean_text(value).lower().replace("+", "")
    return re.sub(r"\s+", " ", normalized).strip()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_numeric_value(value_text: str) -> float | None:
    text = value_text.strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    if "/" in text:
        return None
    is_parentheses_negative = text.startswith("(") and text.endswith(")")
    normalized = (
        text.replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("₹", "")
        .replace("Cr.", "")
        .replace("Cr", "")
        .replace("%", "")
        .replace("+", "")
        .replace("−", "-")
    ).strip()
    if not normalized:
        return None
    try:
        value = float(normalized)
        return -value if is_parentheses_negative else value
    except ValueError:
        return None


def _parse_period_label(label: str) -> str | None:
    parsed = pd.to_datetime(label, format="%b %Y", errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _empty_statement_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "tradingsymbol",
            "company_name",
            "company_slug",
            "statement_scope",
            "statement_section",
            "statement_frequency",
            "period_label",
            "period_end",
            "metric_name",
            "metric_key",
            "value_text",
            "numeric_value",
            "source_url",
            "scraped_at",
        ]
    )
