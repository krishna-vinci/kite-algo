from fundamentals.screener_client import ScreenerFetchResult
from fundamentals.screener_parser import _parse_numeric_value, ensure_screener_parser_ready, parse_screener_company_page


def _fake_result(html: str, symbol: str = "EXAMPLE") -> ScreenerFetchResult:
    return ScreenerFetchResult(
        requested_symbol=symbol,
        company_slug=symbol,
        statement_scope="consolidated",
        source_url=f"https://www.screener.in/company/{symbol}/consolidated/",
        fetched_at="2026-09-05T00:00:00+00:00",
        etag=None,
        last_modified=None,
        not_modified=False,
        html=html,
    )


FULL_PAGE_HTML = """
<html>
  <body>
    <h1>Example Ltd</h1>
    <a href="https://www.nseindia.com/get-quotes/equity?symbol=EXAMPLE">NSE: EXAMPLE</a>
    <a href="https://www.bseindia.com/stock-share-price/example/123456/">BSE: 123456</a>
    <ul>
      <li class="flex flex-space-between" data-source="default"><span class="name">Market Cap</span><span class="nowrap value">₹ <span class="number">1,234</span> Cr.</span></li>
      <li class="flex flex-space-between" data-source="default"><span class="name">Current Price</span><span class="nowrap value">₹ <span class="number">456</span></span></li>
      <li class="flex flex-space-between" data-source="default"><span class="name">Stock P/E</span><span class="nowrap value"><span class="number">28.4</span></span></li>
      <li class="flex flex-space-between" data-source="default"><span class="name">Book Value</span><span class="nowrap value"><span class="number">210</span></span></li>
      <li class="flex flex-space-between" data-source="default"><span class="name">Dividend Yield</span><span class="nowrap value"><span class="number">1.25</span> %</span></li>
      <li class="flex flex-space-between" data-source="default"><span class="name">ROCE</span><span class="nowrap value"><span class="number">14.5</span> %</span></li>
    </ul>
    <table>
      <tr><th></th><th>Mar 2025</th><th>Jun 2025</th><th>Sep 2025</th><th>Dec 2025</th><th>Mar 2026</th></tr>
      <tr><td>Sales +</td><td>100</td><td>110</td><td>120</td><td>130</td><td>140</td></tr>
      <tr><td>Net Profit +</td><td>10</td><td>11</td><td>12</td><td>13</td><td>14</td></tr>
      <tr><td>EPS in Rs</td><td>1</td><td>1.1</td><td>1.2</td><td>1.3</td><td>1.4</td></tr>
    </table>
    <table>
      <tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr>
      <tr><td>Sales +</td><td>400</td><td>500</td></tr>
      <tr><td>Net Profit +</td><td>40</td><td>50</td></tr>
      <tr><td>Dividend Payout %</td><td>20</td><td>22</td></tr>
    </table>
    <table>
      <tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr>
      <tr><td>Equity Capital</td><td>10</td><td>10</td></tr>
      <tr><td>Total Liabilities</td><td>300</td><td>350</td></tr>
      <tr><td>Total Assets</td><td>300</td><td>350</td></tr>
    </table>
    <table>
      <tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr>
      <tr><td>Cash from Operating Activity +</td><td>60</td><td>65</td></tr>
      <tr><td>Free Cash Flow</td><td>55</td><td>60</td></tr>
    </table>
    <table>
      <tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr>
      <tr><td>Debtor Days</td><td>32</td><td>30</td></tr>
      <tr><td>ROCE %</td><td>12</td><td>15</td></tr>
    </table>
    <table>
      <tr><th></th><th>Jun 2025</th><th>Mar 2026</th></tr>
      <tr><td>Promoters +</td><td>55.0%</td><td>54.0%</td></tr>
      <tr><td>FIIs +</td><td>20.0%</td><td>22.0%</td></tr>
      <tr><td>DIIs +</td><td>15.0%</td><td>16.0%</td></tr>
    </table>
    <table>
      <tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr>
      <tr><td>Promoters +</td><td>55.0%</td><td>54.0%</td></tr>
      <tr><td>FIIs +</td><td>20.0%</td><td>22.0%</td></tr>
      <tr><td>DIIs +</td><td>15.0%</td><td>16.0%</td></tr>
    </table>
  </body>
</html>
"""


def test_parser_ready():
    ensure_screener_parser_ready()


def test_parse_extracts_all_nine_datasets():
    parsed = parse_screener_company_page(_fake_result(FULL_PAGE_HTML))
    assert set(parsed) == {
        "company_pages",
        "summary_metrics",
        "quarterly",
        "profit_loss",
        "balance_sheet",
        "cash_flow",
        "ratios",
        "shareholding_quarterly",
        "shareholding_yearly",
    }


def test_parse_extracts_company_identity_and_summary_metrics():
    parsed = parse_screener_company_page(_fake_result(FULL_PAGE_HTML))
    page = parsed["company_pages"].iloc[0]
    assert page["company_name"] == "Example Ltd"
    assert page["nse_symbol"] == "EXAMPLE"
    assert page["bse_code"] == "123456"

    summary = parsed["summary_metrics"]
    assert summary.loc[summary["metric_key"] == "market cap", "numeric_value"].iloc[0] == 1234.0
    assert summary.loc[summary["metric_key"] == "current price", "numeric_value"].iloc[0] == 456.0
    assert summary.loc[summary["metric_key"] == "stock p/e", "numeric_value"].iloc[0] == 28.4
    assert summary.loc[summary["metric_key"] == "book value", "numeric_value"].iloc[0] == 210.0
    assert summary.loc[summary["metric_key"] == "dividend yield", "numeric_value"].iloc[0] == 1.25


def test_parse_extracts_quarterly_rows_with_period_end():
    parsed = parse_screener_company_page(_fake_result(FULL_PAGE_HTML))
    quarterly = parsed["quarterly"]
    latest_sales = quarterly.loc[(quarterly["metric_key"] == "sales") & (quarterly["period_label"] == "Mar 2026")]
    assert latest_sales.iloc[0]["numeric_value"] == 140.0
    # "Mar 2026" labels normalize to the first of the month (month-granular periods).
    assert latest_sales.iloc[0]["period_end"] == "2026-03-01"
    assert set(quarterly["metric_key"]) >= {"sales", "net profit", "eps in rs"}


def test_parse_tolerates_missing_optional_tables():
    partial_html = """
    <html><body><h1>Partial Ltd</h1>
    <table>
      <tr><th></th><th>Mar 2026</th></tr>
      <tr><td>Sales +</td><td>100</td></tr>
      <tr><td>Net Profit +</td><td>10</td></tr>
      <tr><td>EPS in Rs</td><td>1.0</td></tr>
    </table>
    </body></html>
    """
    parsed = parse_screener_company_page(_fake_result(partial_html, symbol="PARTIAL"))
    assert parsed["quarterly"].empty is False
    assert parsed["shareholding_yearly"].empty
    assert parsed["company_pages"].iloc[0]["company_name"] == "Partial Ltd"


def test_parse_numeric_value_rules():
    assert _parse_numeric_value("(1,234.5)") == -1234.5
    assert _parse_numeric_value("₹ 1,234 Cr.") == 1234.0
    assert _parse_numeric_value("12.5%") == 12.5
    assert _parse_numeric_value("22/25") is None  # ratios like 22/25 are not numbers
    assert _parse_numeric_value("") is None
    assert _parse_numeric_value("N/A") is None
