import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import psycopg2.extras
import requests

from database import SessionLocal, get_db_connection
from broker_api.kite_session import build_kite_client, get_system_access_token


logger = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
}

SOURCE_LIST_NIFTY50 = "Nifty50"
SOURCE_LIST_NIFTY500 = "Nifty500"
SOURCE_LIST_NIFTYBANK = "NiftyBank"


@dataclass(frozen=True)
class IndexConfig:
    source_list: str
    tracker_name: Optional[str]
    constituent_csv_url: str


SUPPORTED_INDEXES: Dict[str, IndexConfig] = {
    SOURCE_LIST_NIFTY50: IndexConfig(
        source_list=SOURCE_LIST_NIFTY50,
        tracker_name="NIFTY 50",
        constituent_csv_url="https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    ),
    SOURCE_LIST_NIFTY500: IndexConfig(
        source_list=SOURCE_LIST_NIFTY500,
        tracker_name="NIFTY 500",
        constituent_csv_url="https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    ),
    SOURCE_LIST_NIFTYBANK: IndexConfig(
        source_list=SOURCE_LIST_NIFTYBANK,
        tracker_name="NIFTY BANK",
        constituent_csv_url="https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
    ),
}


NIFTY50_MANUAL_BASELINES: Dict[str, Dict[str, float]] = {
    "ADANIENT": {"weight": 0.50, "freefloat_marketcap": 54.25},
    "ADANIPORTS": {"weight": 1.00, "freefloat_marketcap": 108.59},
    "APOLLOHOSP": {"weight": 0.72, "freefloat_marketcap": 77.35},
    "ASIANPAINT": {"weight": 0.98, "freefloat_marketcap": 105.91},
    "AXISBANK": {"weight": 3.78, "freefloat_marketcap": 408.74},
    "BAJAJ-AUTO": {"weight": 1.01, "freefloat_marketcap": 109.69},
    "BAJAJFINSV": {"weight": 0.97, "freefloat_marketcap": 104.64},
    "BAJFINANCE": {"weight": 2.21, "freefloat_marketcap": 238.48},
    "BEL": {"weight": 1.40, "freefloat_marketcap": 151.37},
    "BHARTIARTL": {"weight": 5.47, "freefloat_marketcap": 591.94},
    "CIPLA": {"weight": 0.63, "freefloat_marketcap": 68.57},
    "COALINDIA": {"weight": 0.91, "freefloat_marketcap": 98.62},
    "DRREDDY": {"weight": 0.67, "freefloat_marketcap": 72.37},
    "EICHERMOT": {"weight": 0.92, "freefloat_marketcap": 99.45},
    "ETERNAL": {"weight": 1.46, "freefloat_marketcap": 158.16},
    "GRASIM": {"weight": 0.87, "freefloat_marketcap": 94.56},
    "HCLTECH": {"weight": 1.35, "freefloat_marketcap": 146.51},
    "HDFCBANK": {"weight": 11.52, "freefloat_marketcap": 1245.26},
    "HDFCLIFE": {"weight": 0.53, "freefloat_marketcap": 57.39},
    "HINDALCO": {"weight": 1.30, "freefloat_marketcap": 140.27},
    "HINDUNILVR": {"weight": 1.77, "freefloat_marketcap": 191.09},
    "ICICIBANK": {"weight": 8.49, "freefloat_marketcap": 917.81},
    "INDIGO": {"weight": 0.95, "freefloat_marketcap": 102.87},
    "INFY": {"weight": 4.06, "freefloat_marketcap": 438.96},
    "ITC": {"weight": 2.56, "freefloat_marketcap": 277.08},
    "JIOFIN": {"weight": 0.74, "freefloat_marketcap": 79.93},
    "JSWSTEEL": {"weight": 0.97, "freefloat_marketcap": 104.97},
    "KOTAKBANK": {"weight": 2.45, "freefloat_marketcap": 265.37},
    "LT": {"weight": 4.20, "freefloat_marketcap": 454.25},
    "M&M": {"weight": 2.71, "freefloat_marketcap": 292.52},
    "MARUTI": {"weight": 1.62, "freefloat_marketcap": 175.10},
    "MAXHEALTH": {"weight": 0.63, "freefloat_marketcap": 68.18},
    "NESTLEIND": {"weight": 0.83, "freefloat_marketcap": 89.33},
    "NTPC": {"weight": 1.59, "freefloat_marketcap": 172.16},
    "ONGC": {"weight": 1.03, "freefloat_marketcap": 110.98},
    "POWERGRID": {"weight": 1.23, "freefloat_marketcap": 133.43},
    "RELIANCE": {"weight": 8.27, "freefloat_marketcap": 894.42},
    "SBILIFE": {"weight": 0.74, "freefloat_marketcap": 79.93},
    "SBIN": {"weight": 3.92, "freefloat_marketcap": 424.18},
    "SHRIRAMFIN": {"weight": 1.30, "freefloat_marketcap": 140.06},
    "SUNPHARMA": {"weight": 1.63, "freefloat_marketcap": 176.24},
    "TATACONSUM": {"weight": 0.66, "freefloat_marketcap": 71.01},
    "TATASTEEL": {"weight": 1.54, "freefloat_marketcap": 166.29},
    "TCS": {"weight": 2.38, "freefloat_marketcap": 257.65},
    "TECHM": {"weight": 0.83, "freefloat_marketcap": 89.45},
    "TITAN": {"weight": 1.52, "freefloat_marketcap": 164.59},
    "TMPV": {"weight": 0.65, "freefloat_marketcap": 70.46},
    "TRENT": {"weight": 0.75, "freefloat_marketcap": 80.89},
    "ULTRACEMCO": {"weight": 1.24, "freefloat_marketcap": 134.31},
    "WIPRO": {"weight": 0.54, "freefloat_marketcap": 58.41},
}

NIFTYBANK_MANUAL_BASELINES: Dict[str, Dict[str, float]] = {
    "HDFCBANK": {"weight": 25.77},
    "SBIN": {"weight": 20.34},
    "ICICIBANK": {"weight": 19.56},
    "AXISBANK": {"weight": 8.68},
    "KOTAKBANK": {"weight": 7.70},
    "UNIONBANK": {"weight": 2.98},
    "BANKBARODA": {"weight": 2.95},
    "PNB": {"weight": 2.65},
    "CANBK": {"weight": 2.63},
    "AUBANK": {"weight": 1.52},
    "FEDERALBNK": {"weight": 1.49},
    "INDUSINDBK": {"weight": 1.34},
    "YESBANK": {"weight": 1.24},
    "IDFCFIRSTB": {"weight": 1.18},
}

NORMALIZED_BASELINE_TOTAL = 100.0


def list_supported_index_source_lists() -> List[str]:
    return list(SUPPORTED_INDEXES.keys())


def normalize_source_list(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "nifty50": SOURCE_LIST_NIFTY50,
        "nifty": SOURCE_LIST_NIFTY50,
        "nifty500": SOURCE_LIST_NIFTY500,
        "niftybank": SOURCE_LIST_NIFTYBANK,
        "banknifty": SOURCE_LIST_NIFTYBANK,
    }
    if raw in aliases:
        return aliases[raw]
    for name in SUPPORTED_INDEXES:
        if raw == name.lower():
            return name
    raise ValueError(f"Unsupported index source_list: {value}")


def parse_constituent_csv(text: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    rows: List[Dict[str, str]] = []
    for row in reader:
        symbol = str(row.get("Symbol") or "").strip()
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "company_name": str(row.get("Company Name") or "").strip(),
                "industry": str(row.get("Industry") or "").strip(),
                "series": str(row.get("Series") or "").strip(),
                "isin_code": str(row.get("ISIN Code") or "").strip(),
            }
        )
    return rows


def parse_top_holdings_csv(text: str) -> Dict[str, float]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    weights: Dict[str, float] = {}
    for row in reader:
        symbol = str(row.get("SYMBOL") or "").strip()
        value = str(row.get("WEIGHTAGE(%)") or "").strip()
        if not symbol or not value:
            continue
        try:
            weights[symbol] = float(value)
        except ValueError:
            continue
    return weights


def compute_points_contribution(index_previous_close: Optional[float], return_attribution: Optional[float]) -> Optional[float]:
    if index_previous_close in (None, 0) or return_attribution is None:
        return None
    return round(float(index_previous_close) * float(return_attribution) / 100.0, 4)


def compute_live_weight(total_live_freefloat: float, live_freefloat_marketcap: Optional[float]) -> Optional[float]:
    if total_live_freefloat <= 0 or live_freefloat_marketcap is None:
        return None
    return round(float(live_freefloat_marketcap) * 100.0 / float(total_live_freefloat), 4)


def compute_baseline_ff_factor(baseline_freefloat_marketcap: Optional[float], baseline_close: Optional[float]) -> Optional[float]:
    if baseline_freefloat_marketcap in (None, 0) or baseline_close in (None, 0):
        return None
    return round(float(baseline_freefloat_marketcap) / float(baseline_close), 10)


class NseDataClient:
    def __init__(self) -> None:
        self._nse = requests.Session()
        self._nifty = requests.Session()
        self._nse.headers.update(NSE_HEADERS)
        self._nifty.headers.update(NSE_HEADERS)
        self._nse_bootstrapped = False
        self._nifty_bootstrapped = False

    def _bootstrap_nse(self) -> None:
        if self._nse_bootstrapped:
            return
        self._nse.get("https://www.nseindia.com", timeout=20)
        self._nse_bootstrapped = True

    def _bootstrap_nifty(self) -> None:
        if self._nifty_bootstrapped:
            return
        self._nifty_bootstrapped = True

    def fetch_text(self, url: str, *, referer: Optional[str] = None, use_nse: bool = False) -> str:
        if use_nse:
            self._bootstrap_nse()
        else:
            self._bootstrap_nifty()
        session = self._nse if use_nse else self._nifty
        headers = {"Referer": referer or ("https://www.nseindia.com" if use_nse else "https://www.niftyindices.com")}
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text

    def fetch_json(self, url: str, *, referer: Optional[str] = None) -> Any:
        self._bootstrap_nse()
        headers = {"Referer": referer or "https://www.nseindia.com"}
        response = self._nse.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def fetch_index_overview(self, tracker_name: str) -> Dict[str, Any]:
        payload = self.fetch_json(
            f"https://www.nseindia.com/api/NextApi/apiClient/indexTrackerApi?functionName=getIndexData&&index={requests.utils.quote(tracker_name)}",
            referer=f"https://www.nseindia.com/index-tracker/{requests.utils.quote(tracker_name)}",
        )
        rows = payload.get("data") or []
        return dict(rows[0]) if rows else {}

    def fetch_equity_quote(self, symbol: str) -> Dict[str, Any]:
        payload = self.fetch_json(
            f"https://www.nseindia.com/api/quote-equity?symbol={requests.utils.quote(symbol)}",
            referer=f"https://www.nseindia.com/get-quotes/equity?symbol={requests.utils.quote(symbol)}",
        )
        return dict(payload or {})


def ensure_index_ingestion_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS isin_code VARCHAR(32)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS series VARCHAR(32)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS source_url TEXT")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS weight_source VARCHAR(128)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS points_contribution NUMERIC(18, 4)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMP WITH TIME ZONE")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_close NUMERIC(18, 6)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_index_weight NUMERIC(10, 4)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_freefloat_marketcap NUMERIC(20, 2)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_ff_factor NUMERIC(24, 10)")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_as_of_date DATE")
        cur.execute("ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS needs_weight_review BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.index_refresh_state (
                source_list VARCHAR(255) PRIMARY KEY,
                last_constituent_refresh_at TIMESTAMP WITH TIME ZONE,
                last_live_refresh_at TIMESTAMP WITH TIME ZONE,
                added_symbols_json TEXT,
                removed_symbols_json TEXT,
                needs_review BOOLEAN NOT NULL DEFAULT FALSE,
                last_error TEXT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
            """
        )
    conn.commit()


def _load_instrument_map(conn, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tradingsymbol, instrument_token, exchange
            FROM kite_instruments
            WHERE exchange = 'NSE' AND instrument_type = 'EQ' AND tradingsymbol = ANY(%s)
            """,
            (list(symbols),),
        )
        return {
            row[0]: {"tradingsymbol": row[0], "instrument_token": row[1], "exchange": row[2]}
            for row in cur.fetchall()
        }


def _load_existing_rows(conn, source_list: str) -> Dict[str, Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM kite_ticker_tickers WHERE source_list = %s", (source_list,))
        return {str(row["tradingsymbol"]): dict(row) for row in cur.fetchall()}


def _upsert_refresh_state(
    conn,
    *,
    source_list: str,
    added_symbols: Sequence[str],
    removed_symbols: Sequence[str],
    needs_review: bool,
    last_error: Optional[str] = None,
    constituent_refresh_at: Optional[datetime] = None,
    live_refresh_at: Optional[datetime] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.index_refresh_state (
                source_list, last_constituent_refresh_at, last_live_refresh_at,
                added_symbols_json, removed_symbols_json, needs_review, last_error, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (source_list) DO UPDATE SET
                last_constituent_refresh_at = COALESCE(EXCLUDED.last_constituent_refresh_at, public.index_refresh_state.last_constituent_refresh_at),
                last_live_refresh_at = COALESCE(EXCLUDED.last_live_refresh_at, public.index_refresh_state.last_live_refresh_at),
                added_symbols_json = EXCLUDED.added_symbols_json,
                removed_symbols_json = EXCLUDED.removed_symbols_json,
                needs_review = EXCLUDED.needs_review,
                last_error = EXCLUDED.last_error,
                updated_at = NOW()
            """,
            (
                source_list,
                constituent_refresh_at,
                live_refresh_at,
                json.dumps(list(added_symbols)),
                json.dumps(list(removed_symbols)),
                needs_review,
                last_error,
            ),
        )


def _load_refresh_state_row(conn, source_list: str) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM public.index_refresh_state WHERE source_list = %s", (source_list,))
        row = cur.fetchone() or {"source_list": source_list}
        cur.execute(
            "SELECT COUNT(*) AS pending_review_count FROM public.kite_ticker_tickers WHERE source_list = %s AND needs_weight_review = TRUE",
            (source_list,),
        )
        pending = cur.fetchone() or {"pending_review_count": 0}
    state = dict(row)
    for key in ("added_symbols_json", "removed_symbols_json"):
        raw = state.get(key)
        try:
            state[key] = json.loads(raw) if raw else []
        except Exception:
            state[key] = []
    state["added_symbols"] = state.pop("added_symbols_json", [])
    state["removed_symbols"] = state.pop("removed_symbols_json", [])
    state["pending_review_count"] = int(pending["pending_review_count"])
    return state


def get_index_refresh_state(source_list: str) -> Dict[str, Any]:
    normalized = normalize_source_list(source_list)
    conn = get_db_connection()
    try:
        ensure_index_ingestion_schema(conn)
        return _load_refresh_state_row(conn, normalized)
    finally:
        conn.close()


def refresh_single_index_constituents(source_list: str, *, client: Optional[NseDataClient] = None) -> Dict[str, Any]:
    normalized = normalize_source_list(source_list)
    config = SUPPORTED_INDEXES[normalized]
    own_client = client is None
    client = client or NseDataClient()
    conn = get_db_connection()
    try:
        ensure_index_ingestion_schema(conn)
        constituent_csv = client.fetch_text(config.constituent_csv_url, referer="https://www.nseindia.com/all-reports/", use_nse=True)
        constituents = parse_constituent_csv(constituent_csv)
        symbols = [row["symbol"] for row in constituents]
        instrument_map = _load_instrument_map(conn, symbols)
        existing_rows = _load_existing_rows(conn, normalized)
        old_symbols = set(existing_rows.keys())
        new_symbols = {symbol for symbol in symbols if symbol in instrument_map}
        added_symbols = sorted(new_symbols - old_symbols)
        removed_symbols = sorted(old_symbols - new_symbols)
        now_utc = datetime.now(timezone.utc)

        prepared_rows: List[Dict[str, Any]] = []
        unmatched_symbols: List[str] = []
        for item in constituents:
            symbol = item["symbol"]
            instrument = instrument_map.get(symbol)
            if not instrument:
                unmatched_symbols.append(symbol)
                continue
            existing = existing_rows.get(symbol, {})
            needs_review = bool(existing.get("needs_weight_review"))
            if symbol in added_symbols:
                needs_review = True
            if normalized in {SOURCE_LIST_NIFTY50, SOURCE_LIST_NIFTYBANK} and existing.get("baseline_ff_factor") is None:
                needs_review = True
            prepared_rows.append(
                {
                    "instrument_token": instrument["instrument_token"],
                    "tradingsymbol": symbol,
                    "company_name": item["company_name"],
                    "sector": item["industry"] or existing.get("sector"),
                    "exchange": instrument.get("exchange") or existing.get("exchange") or "NSE",
                    "source_list": normalized,
                    "isin_code": item.get("isin_code") or existing.get("isin_code"),
                    "series": item.get("series") or existing.get("series"),
                    "source_url": config.constituent_csv_url,
                    "weight_source": existing.get("weight_source"),
                    "open": existing.get("open"),
                    "high": existing.get("high"),
                    "low": existing.get("low"),
                    "close": existing.get("close"),
                    "ltp": existing.get("ltp"),
                    "change_1d": existing.get("change_1d"),
                    "net_change": existing.get("net_change"),
                    "net_change_percent": existing.get("net_change_percent"),
                    "return_attribution": existing.get("return_attribution"),
                    "index_weight": existing.get("index_weight"),
                    "freefloat_marketcap": existing.get("freefloat_marketcap"),
                    "points_contribution": existing.get("points_contribution"),
                    "last_updated": existing.get("last_updated"),
                    "last_refreshed_at": now_utc,
                    "baseline_close": existing.get("baseline_close"),
                    "baseline_index_weight": existing.get("baseline_index_weight"),
                    "baseline_freefloat_marketcap": existing.get("baseline_freefloat_marketcap"),
                    "baseline_ff_factor": existing.get("baseline_ff_factor"),
                    "baseline_as_of_date": existing.get("baseline_as_of_date"),
                    "needs_weight_review": needs_review,
                }
            )

        if not prepared_rows:
            raise RuntimeError(f"No constituents prepared for {normalized}; keeping previous snapshot")

        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.kite_ticker_tickers WHERE source_list = %s", (normalized,))
            insert_sql = """
                INSERT INTO public.kite_ticker_tickers (
                    instrument_token, tradingsymbol, company_name, sector, exchange, source_list,
                    isin_code, series, source_url, weight_source,
                    open, high, low, close, ltp, change_1d, net_change, net_change_percent,
                    return_attribution, index_weight, freefloat_marketcap, points_contribution,
                    last_updated, last_refreshed_at,
                    baseline_close, baseline_index_weight, baseline_freefloat_marketcap,
                    baseline_ff_factor, baseline_as_of_date, needs_weight_review
                ) VALUES (
                    %(instrument_token)s, %(tradingsymbol)s, %(company_name)s, %(sector)s, %(exchange)s, %(source_list)s,
                    %(isin_code)s, %(series)s, %(source_url)s, %(weight_source)s,
                    %(open)s, %(high)s, %(low)s, %(close)s, %(ltp)s, %(change_1d)s, %(net_change)s, %(net_change_percent)s,
                    %(return_attribution)s, %(index_weight)s, %(freefloat_marketcap)s, %(points_contribution)s,
                    COALESCE(%(last_updated)s, NOW()), %(last_refreshed_at)s,
                    %(baseline_close)s, %(baseline_index_weight)s, %(baseline_freefloat_marketcap)s,
                    %(baseline_ff_factor)s, %(baseline_as_of_date)s, %(needs_weight_review)s
                )
            """
            psycopg2.extras.execute_batch(cur, insert_sql, prepared_rows)
            _upsert_refresh_state(
                conn,
                source_list=normalized,
                added_symbols=added_symbols,
                removed_symbols=removed_symbols,
                needs_review=bool(added_symbols or removed_symbols or any(row["needs_weight_review"] for row in prepared_rows)),
                constituent_refresh_at=now_utc,
            )
        conn.commit()

        if normalized == SOURCE_LIST_NIFTY50:
            apply_manual_baseline_seed(normalized, NIFTY50_MANUAL_BASELINES, client=client)
        elif normalized == SOURCE_LIST_NIFTYBANK:
            apply_manual_baseline_seed(normalized, NIFTYBANK_MANUAL_BASELINES, client=client)

        return {
            "source_list": normalized,
            "status": "success",
            "updated_rows": len(prepared_rows),
            "added_symbols": added_symbols,
            "removed_symbols": removed_symbols,
            "unmatched_symbols": unmatched_symbols,
        }
    except Exception as exc:
        conn.rollback()
        try:
            _upsert_refresh_state(
                conn,
                source_list=normalized,
                added_symbols=[],
                removed_symbols=[],
                needs_review=True,
                last_error=str(exc),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()
        if own_client:
            try:
                client._nse.close()
                client._nifty.close()
            except Exception:
                pass


def apply_manual_baseline_seed(
    source_list: str,
    baselines: Mapping[str, Mapping[str, float]],
    *,
    client: Optional[NseDataClient] = None,
    force: bool = False,
    normalized_total: float = NORMALIZED_BASELINE_TOTAL,
    normalize_freefloat: bool = True,
) -> Dict[str, Any]:
    normalized = normalize_source_list(source_list)
    own_client = client is None
    client = client or NseDataClient()
    conn = get_db_connection()
    try:
        ensure_index_ingestion_schema(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT instrument_token, tradingsymbol, close, baseline_close, baseline_ff_factor FROM public.kite_ticker_tickers WHERE source_list = %s",
                (normalized,),
            )
            rows = cur.fetchall()

        updated = 0
        missing_symbols: List[str] = []
        supplied_weight_total = sum(float(item.get("weight") or 0.0) for item in baselines.values())
        with conn.cursor() as cur:
            for row in rows:
                symbol = str(row["tradingsymbol"])
                baseline = baselines.get(symbol)
                if not baseline:
                    missing_symbols.append(symbol)
                    continue
                if not force and row.get("baseline_ff_factor") not in (None, 0):
                    continue
                baseline_close = row.get("close") or row.get("baseline_close")
                if baseline_close in (None, 0):
                    quote = client.fetch_equity_quote(symbol)
                    price_info = quote.get("priceInfo") or {}
                    baseline_close = price_info.get("previousClose") or price_info.get("close") or row.get("close")
                try:
                    baseline_close = float(baseline_close) if baseline_close is not None else None
                except (TypeError, ValueError):
                    baseline_close = None
                baseline_weight = float(baseline["weight"])
                if normalize_freefloat and supplied_weight_total > 0:
                    baseline_weight = baseline_weight * 100.0 / supplied_weight_total
                if normalize_freefloat or baseline.get("freefloat_marketcap") is None:
                    baseline_ffm = float(normalized_total) * baseline_weight / 100.0
                else:
                    baseline_ffm = float(baseline["freefloat_marketcap"])
                baseline_ff_factor = compute_baseline_ff_factor(baseline_ffm, baseline_close)
                cur.execute(
                    """
                    UPDATE public.kite_ticker_tickers
                    SET baseline_close = %s,
                        baseline_index_weight = %s,
                        baseline_freefloat_marketcap = %s,
                        baseline_ff_factor = %s,
                        baseline_as_of_date = %s,
                        needs_weight_review = FALSE,
                        weight_source = %s,
                        index_weight = COALESCE(index_weight, %s),
                        freefloat_marketcap = COALESCE(freefloat_marketcap, %s),
                        close = COALESCE(close, %s),
                        last_refreshed_at = NOW()
                    WHERE instrument_token = %s AND source_list = %s
                    """,
                    (
                        baseline_close,
                        baseline_weight,
                        baseline_ffm,
                        baseline_ff_factor,
                        date.today(),
                        "manual_normalized_seed" if normalize_freefloat else "manual_user_seed",
                        baseline_weight,
                        baseline_ffm,
                        baseline_close,
                        row["instrument_token"],
                        normalized,
                    ),
                )
                updated += 1
            pending_review = [symbol for symbol in missing_symbols if normalized in {SOURCE_LIST_NIFTY50, SOURCE_LIST_NIFTYBANK}]
            _upsert_refresh_state(
                conn,
                source_list=normalized,
                added_symbols=[],
                removed_symbols=[],
                needs_review=bool(pending_review),
            )
        conn.commit()
        return {
            "source_list": normalized,
            "status": "success",
            "updated": updated,
            "missing_symbols": missing_symbols,
        }
    finally:
        conn.close()
        if own_client:
            try:
                client._nse.close()
                client._nifty.close()
            except Exception:
                pass


def _build_live_metric_payloads(rows: Sequence[Mapping[str, Any]], quotes: Mapping[str, Mapping[str, Any]], index_previous_close: Optional[float]) -> List[Dict[str, Any]]:
    total_live_freefloat = 0.0
    live_freefloat_by_symbol: Dict[str, Optional[float]] = {}
    for row in rows:
        symbol = str(row["tradingsymbol"])
        quote = quotes.get(symbol)
        if not quote:
            live_freefloat_by_symbol[symbol] = None
            continue
        price_info = quote.get("priceInfo") or {}
        last_price = price_info.get("lastPrice")
        try:
            last_price = float(last_price) if last_price is not None else None
        except (TypeError, ValueError):
            last_price = None
        factor = row.get("baseline_ff_factor")
        try:
            factor = float(factor) if factor is not None else None
        except (TypeError, ValueError):
            factor = None
        live_ffm = round(factor * last_price, 2) if factor and last_price else None
        live_freefloat_by_symbol[symbol] = live_ffm
        if live_ffm is not None:
            total_live_freefloat += live_ffm

    payloads: List[Dict[str, Any]] = []
    for row in rows:
        symbol = str(row["tradingsymbol"])
        quote = quotes.get(symbol)
        if not quote:
            continue
        price_info = quote.get("priceInfo") or {}
        intra = price_info.get("intraDayHighLow") or {}
        change_1d = price_info.get("pChange")
        net_change = price_info.get("change")
        previous_close = price_info.get("previousClose") or price_info.get("close")
        last_price = price_info.get("lastPrice")
        open_price = price_info.get("open")
        try:
            change_1d = float(change_1d) if change_1d is not None else None
        except (TypeError, ValueError):
            change_1d = None
        try:
            net_change = float(net_change) if net_change is not None else None
        except (TypeError, ValueError):
            net_change = None
        try:
            previous_close = float(previous_close) if previous_close is not None else None
        except (TypeError, ValueError):
            previous_close = None
        try:
            last_price = float(last_price) if last_price is not None else None
        except (TypeError, ValueError):
            last_price = None
        try:
            open_price = float(open_price) if open_price is not None else None
        except (TypeError, ValueError):
            open_price = None
        live_ffm = live_freefloat_by_symbol.get(symbol)
        live_weight = compute_live_weight(total_live_freefloat, live_ffm)
        return_attribution = round(change_1d * live_weight / 100.0, 4) if change_1d is not None and live_weight is not None else None
        payloads.append(
            {
                "instrument_token": row["instrument_token"],
                "open": open_price,
                "high": intra.get("max"),
                "low": intra.get("min"),
                "close": previous_close,
                "ltp": last_price,
                "change_1d": change_1d,
                "net_change": net_change,
                "net_change_percent": change_1d,
                "freefloat_marketcap": live_ffm,
                "index_weight": live_weight,
                "return_attribution": return_attribution,
                "points_contribution": compute_points_contribution(index_previous_close, return_attribution),
            }
        )
    return payloads


def _normalize_broker_quote_payload(quote: Mapping[str, Any]) -> Dict[str, Any]:
    ohlc = dict(quote.get("ohlc") or {})
    last_price = quote.get("last_price")
    previous_close = ohlc.get("close")
    change = None
    change_percent = None
    try:
        if last_price is not None and previous_close not in (None, 0):
            change = float(last_price) - float(previous_close)
            change_percent = change * 100.0 / float(previous_close)
    except (TypeError, ValueError, ZeroDivisionError):
        change = None
        change_percent = None
    return {
        "priceInfo": {
            "lastPrice": last_price,
            "previousClose": previous_close,
            "close": previous_close,
            "open": ohlc.get("open"),
            "change": change,
            "pChange": change_percent,
            "intraDayHighLow": {"max": ohlc.get("high"), "min": ohlc.get("low")},
        }
    }


def _fetch_quote_snapshots(rows: Sequence[Mapping[str, Any]], client: NseDataClient) -> Dict[str, Dict[str, Any]]:
    quotes: Dict[str, Dict[str, Any]] = {}
    session = SessionLocal()
    try:
        access_token = get_system_access_token(session)
    finally:
        session.close()

    missing_symbols = [str(row["tradingsymbol"]) for row in rows]
    if access_token:
        try:
            kite = build_kite_client(access_token, session_id="system")
            instrument_keys = [f"{(row.get('exchange') or 'NSE')}:{row['tradingsymbol']}" for row in rows]
            broker_quotes = kite.quote(instrument_keys)
            for row in rows:
                symbol = str(row["tradingsymbol"])
                key = f"{(row.get('exchange') or 'NSE')}:{symbol}"
                if key in broker_quotes:
                    quotes[symbol] = _normalize_broker_quote_payload(broker_quotes[key])
            missing_symbols = [str(row["tradingsymbol"]) for row in rows if str(row["tradingsymbol"]) not in quotes]
        except Exception as exc:
            logger.warning("Broker batch quote fetch failed; falling back to NSE quote API: %s", exc)

    for symbol in missing_symbols:
        quotes[symbol] = client.fetch_equity_quote(symbol)
    return quotes


def refresh_live_metrics(source_list: str, *, client: Optional[NseDataClient] = None) -> Dict[str, Any]:
    normalized = normalize_source_list(source_list)
    config = SUPPORTED_INDEXES[normalized]
    own_client = client is None
    client = client or NseDataClient()
    conn = get_db_connection()
    try:
        ensure_index_ingestion_schema(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT instrument_token, tradingsymbol, baseline_ff_factor, baseline_close,
                       baseline_index_weight, baseline_freefloat_marketcap, exchange
                FROM public.kite_ticker_tickers
                WHERE source_list = %s
                ORDER BY tradingsymbol
                """,
                (normalized,),
            )
            rows = cur.fetchall()
        if not rows:
            return {"source_list": normalized, "status": "success", "updated": 0, "skipped": "no_rows"}

        index_previous_close: Optional[float] = None
        if config.tracker_name:
            overview = client.fetch_index_overview(config.tracker_name)
            index_previous_close = overview.get("previousClose")

        failed_symbols: List[str] = []
        quotes: Dict[str, Dict[str, Any]] = {}
        try:
            quotes = _fetch_quote_snapshots(rows, client)
        except Exception as exc:
            logger.warning("Primary quote snapshot fetch failed for %s: %s", normalized, exc)
            for row in rows:
                symbol = str(row["tradingsymbol"])
                try:
                    quotes[symbol] = client.fetch_equity_quote(symbol)
                except Exception as inner_exc:
                    logger.warning("Failed to refresh live quote for %s/%s: %s", normalized, symbol, inner_exc)
                    failed_symbols.append(symbol)

        payloads = _build_live_metric_payloads(rows, quotes, index_previous_close)
        now_utc = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            update_sql = """
                UPDATE public.kite_ticker_tickers
                SET open = %(open)s,
                    high = %(high)s,
                    low = %(low)s,
                    close = %(close)s,
                    ltp = %(ltp)s,
                    change_1d = %(change_1d)s,
                    net_change = %(net_change)s,
                    net_change_percent = %(net_change_percent)s,
                    freefloat_marketcap = %(freefloat_marketcap)s,
                    index_weight = %(index_weight)s,
                    return_attribution = %(return_attribution)s,
                    points_contribution = %(points_contribution)s,
                    last_updated = %(last_updated)s
                WHERE instrument_token = %(instrument_token)s AND source_list = %(source_list)s
            """
            enriched_payloads = [dict(item, last_updated=now_utc, source_list=normalized) for item in payloads]
            psycopg2.extras.execute_batch(cur, update_sql, enriched_payloads, page_size=50)
            state = _load_refresh_state_row(conn, normalized)
            _upsert_refresh_state(
                conn,
                source_list=normalized,
                added_symbols=state.get("added_symbols", []),
                removed_symbols=state.get("removed_symbols", []),
                needs_review=bool(state.get("needs_review") or state.get("pending_review_count")),
                live_refresh_at=now_utc,
            )
        conn.commit()
        return {
            "source_list": normalized,
            "status": "success",
            "updated": len(payloads),
            "failed_symbols": failed_symbols,
            "index_previous_close": index_previous_close,
        }
    except Exception as exc:
        conn.rollback()
        state = _load_refresh_state_row(conn, normalized)
        try:
            _upsert_refresh_state(
                conn,
                source_list=normalized,
                added_symbols=state.get("added_symbols", []),
                removed_symbols=state.get("removed_symbols", []),
                needs_review=bool(state.get("needs_review") or state.get("pending_review_count")),
                last_error=str(exc),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()
        if own_client:
            try:
                client._nse.close()
                client._nifty.close()
            except Exception:
                pass


def ensure_fresh_live_metrics(source_list: str, *, max_age_seconds: int = 900) -> Dict[str, Any]:
    normalized = normalize_source_list(source_list)
    conn = get_db_connection()
    try:
        ensure_index_ingestion_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(last_updated) FROM public.kite_ticker_tickers WHERE source_list = %s", (normalized,))
            row = cur.fetchone()
        last_updated = row[0] if row else None
    finally:
        conn.close()
    if not last_updated:
        return refresh_live_metrics(normalized)
    age_seconds = (datetime.now(timezone.utc) - last_updated).total_seconds()
    if age_seconds >= max_age_seconds:
        return refresh_live_metrics(normalized)
    return {"source_list": normalized, "status": "success", "updated": 0, "skipped": "fresh_enough", "last_updated": last_updated.isoformat()}


def refresh_supported_indices(source_lists: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    normalized_lists = [normalize_source_list(item) for item in (source_lists or SUPPORTED_INDEXES.keys())]
    client = NseDataClient()
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    try:
        for source_list in normalized_lists:
            try:
                results.append(refresh_single_index_constituents(source_list, client=client))
            except Exception as exc:
                logger.error("Index constituent refresh failed for %s: %s", source_list, exc, exc_info=True)
                failures.append({"source_list": source_list, "error": str(exc)})
    finally:
        try:
            client._nse.close()
            client._nifty.close()
        except Exception:
            pass
    return {
        "status": "success" if not failures else ("partial_success" if results else "error"),
        "results": results,
        "failures": failures,
    }


def refresh_live_metrics_for_indices(source_lists: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    normalized_lists = [normalize_source_list(item) for item in (source_lists or [SOURCE_LIST_NIFTY50, SOURCE_LIST_NIFTYBANK])]
    client = NseDataClient()
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    try:
        for source_list in normalized_lists:
            try:
                results.append(refresh_live_metrics(source_list, client=client))
            except Exception as exc:
                logger.error("Live metric refresh failed for %s: %s", source_list, exc, exc_info=True)
                failures.append({"source_list": source_list, "error": str(exc)})
    finally:
        try:
            client._nse.close()
            client._nifty.close()
        except Exception:
            pass
    return {
        "status": "success" if not failures else ("partial_success" if results else "error"),
        "results": results,
        "failures": failures,
    }
