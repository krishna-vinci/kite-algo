"""Fundamentals sync engine: scope -> fetch -> parse -> per-symbol SQL upsert -> state.

Single engine serving both the nightly scheduler and on-demand API syncs.
Per-symbol failures are isolated; the 15-second politeness delay between
requests is never reduced; on-demand runs are capped and single-flighted.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from backend.app.database import get_db_connection
from fundamentals.features import compute_features_from_rows
from fundamentals.index_scopes import resolve_index_symbols
from fundamentals.screener_client import ScreenerClient
from fundamentals.screener_parser import ensure_screener_parser_ready, parse_screener_company_page

DATASETS = [
    "summary_metrics", "quarterly", "profit_loss", "balance_sheet",
    "cash_flow", "ratios", "shareholding_quarterly", "shareholding_yearly",
]
MAX_ON_DEMAND_SYMBOLS = 50
DEFAULT_FRESHNESS_HOURS = 24.0
DEFAULT_MIN_DELAY_SECONDS = 15.0

_sync_lock = asyncio.Lock()


@dataclass(slots=True)
class SyncScope:
    scope_type: str  # "symbols" | "index"
    scope_value: str  # comma-joined symbols or index key


@dataclass(slots=True)
class SyncConfig:
    scope: SyncScope
    mode: str = "incremental"  # "incremental" | "full"
    freshness_hours: float = DEFAULT_FRESHNESS_HOURS
    min_delay_seconds: float = DEFAULT_MIN_DELAY_SECONDS
    statement_scope: str = "consolidated"
    on_demand: bool = False


def resolve_scope_symbols(scope: SyncScope) -> list[str]:
    if scope.scope_type == "symbols":
        return [s.strip().upper() for s in scope.scope_value.split(",") if s.strip()]
    if scope.scope_type != "index":
        raise ValueError(f"unsupported scope type '{scope.scope_type}'")
    # Index scope: the adapter resolves membership from the same constituent
    # store the 0.7.6 snapshot route serves, and validates the scope key.
    return resolve_index_symbols(scope.scope_value)


def _load_symbol_state(symbol: str, statement_scope: str) -> dict[str, Any]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, etag, last_modified, content_fingerprint, last_checked_at, last_error, source_url "
                "FROM public.fundamentals_symbol_state WHERE symbol=%s AND statement_scope=%s",
                (symbol, statement_scope),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    keys = ["status", "etag", "last_modified", "content_fingerprint", "last_checked_at", "last_error", "source_url"]
    return dict(zip(keys, row))


def _should_fetch(state: dict[str, Any], config: SyncConfig) -> bool:
    if config.mode == "full" or not state:
        return True
    if state.get("status") == "failed":
        return True
    last = pd.to_datetime(state.get("last_checked_at"), utc=True, errors="coerce")
    if pd.isna(last):
        return True
    return (pd.Timestamp.now(tz="UTC") - last).total_seconds() / 3600.0 >= config.freshness_hours


def _fingerprint(parsed: dict[str, pd.DataFrame]) -> str:
    payload: dict[str, Any] = {}
    for name, frame in parsed.items():
        stable = frame.copy()
        for col in ("scraped_at", "source_url", "page_html"):
            if col in stable.columns:
                stable = stable.drop(columns=[col])
        stable = stable.sort_index(axis=1)
        payload[name] = json.loads(stable.to_json(orient="records", date_format="iso"))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_parsed_datasets(parsed: dict[str, pd.DataFrame]) -> None:
    """Reject block/error pages before they can replace stored features."""
    if any(
        isinstance(parsed.get(name), pd.DataFrame) and not parsed[name].empty
        for name in DATASETS
    ):
        return
    raise ValueError("Screener page contained no recognized fundamentals datasets")


def _upsert_symbol(symbol: str, statement_scope: str, parsed: dict[str, pd.DataFrame], scraped_at: str) -> None:
    import psycopg2.extras

    company_name = None
    company_pages = parsed.get("company_pages")
    if company_pages is not None and not company_pages.empty and "company_name" in company_pages.columns:
        raw_name = company_pages.iloc[0].get("company_name")
        company_name = None if pd.isna(raw_name) else str(raw_name)

    metric_rows: list[tuple] = []
    for parsed_name in DATASETS:
        frame = parsed.get(parsed_name)
        if frame is None or frame.empty:
            continue
        for _, row in frame.iterrows():
            period_end = row.get("period_end")
            metric_rows.append((
                symbol, statement_scope, parsed_name,
                None if period_end is None or pd.isna(period_end) else period_end,
                row.get("metric_key"), row.get("metric_name"),
                None if pd.isna(row.get("value_text")) else str(row.get("value_text")),
                None if pd.isna(row.get("numeric_value")) else float(row["numeric_value"]),
                scraped_at,
            ))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if metric_rows:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO public.fundamentals_metrics
                        (symbol, statement_scope, dataset, period_end, metric_key, metric_name,
                         value_text, numeric_value, scraped_at)
                    VALUES %s
                    ON CONFLICT (symbol, statement_scope, dataset, period_end, metric_key)
                    DO UPDATE SET metric_name = EXCLUDED.metric_name,
                                  value_text = EXCLUDED.value_text,
                                  numeric_value = EXCLUDED.numeric_value,
                                  scraped_at = EXCLUDED.scraped_at
                    """,
                    metric_rows,
                )
            # Stale-period cleanup: drop stored periods the page no longer reports.
            cur.execute(
                """
                DELETE FROM public.fundamentals_metrics
                WHERE symbol=%s AND statement_scope=%s
                  AND period_end IS NOT NULL
                  AND period_end < (SELECT MIN(period_end) FROM public.fundamentals_metrics
                                    WHERE symbol=%s AND statement_scope=%s AND scraped_at=%s
                                      AND period_end IS NOT NULL)
                """,
                (symbol, statement_scope, symbol, statement_scope, scraped_at),
            )
            cur.execute(
                "SELECT dataset, period_end, metric_key, metric_name, numeric_value "
                "FROM public.fundamentals_metrics WHERE symbol=%s AND statement_scope=%s",
                (symbol, statement_scope),
            )
            rows = pd.DataFrame(
                cur.fetchall(),
                columns=["dataset", "period_end", "metric_key", "metric_name", "numeric_value"],
            )
        features = compute_features_from_rows(rows, scraped_at=scraped_at, company_name=company_name)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fundamentals_features (
                    symbol, statement_scope, company_name, market_cap_cr, current_price, stock_pe,
                    book_value, dividend_yield_pct, latest_quarter_revenue, latest_quarter_net_profit,
                    latest_quarter_eps, ttm_revenue, ttm_net_profit, quarterly_revenue_yoy_pct,
                    quarterly_profit_yoy_pct, latest_roce_pct, latest_roe_pct, promoter_holding_pct,
                    fii_holding_pct, dii_holding_pct, promoter_holding_change_1y_pct,
                    fii_holding_change_1y_pct, dii_holding_change_1y_pct, as_of_date, scraped_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, statement_scope) DO UPDATE SET
                    company_name=EXCLUDED.company_name, market_cap_cr=EXCLUDED.market_cap_cr,
                    current_price=EXCLUDED.current_price, stock_pe=EXCLUDED.stock_pe,
                    book_value=EXCLUDED.book_value, dividend_yield_pct=EXCLUDED.dividend_yield_pct,
                    latest_quarter_revenue=EXCLUDED.latest_quarter_revenue,
                    latest_quarter_net_profit=EXCLUDED.latest_quarter_net_profit,
                    latest_quarter_eps=EXCLUDED.latest_quarter_eps,
                    ttm_revenue=EXCLUDED.ttm_revenue, ttm_net_profit=EXCLUDED.ttm_net_profit,
                    quarterly_revenue_yoy_pct=EXCLUDED.quarterly_revenue_yoy_pct,
                    quarterly_profit_yoy_pct=EXCLUDED.quarterly_profit_yoy_pct,
                    latest_roce_pct=EXCLUDED.latest_roce_pct, latest_roe_pct=EXCLUDED.latest_roe_pct,
                    promoter_holding_pct=EXCLUDED.promoter_holding_pct, fii_holding_pct=EXCLUDED.fii_holding_pct,
                    dii_holding_pct=EXCLUDED.dii_holding_pct,
                    promoter_holding_change_1y_pct=EXCLUDED.promoter_holding_change_1y_pct,
                    fii_holding_change_1y_pct=EXCLUDED.fii_holding_change_1y_pct,
                    dii_holding_change_1y_pct=EXCLUDED.dii_holding_change_1y_pct,
                    as_of_date=EXCLUDED.as_of_date, scraped_at=EXCLUDED.scraped_at
                """,
                (
                    symbol, statement_scope, features.get("company_name"), features.get("market_cap_cr"),
                    features.get("current_price"), features.get("stock_pe"), features.get("book_value"),
                    features.get("dividend_yield_pct"), features.get("latest_quarter_revenue"),
                    features.get("latest_quarter_net_profit"), features.get("latest_quarter_eps"),
                    features.get("ttm_revenue"), features.get("ttm_net_profit"),
                    features.get("quarterly_revenue_yoy_pct"), features.get("quarterly_profit_yoy_pct"),
                    features.get("latest_roce_pct"), features.get("latest_roe_pct"),
                    features.get("promoter_holding_pct"), features.get("fii_holding_pct"),
                    features.get("dii_holding_pct"), features.get("promoter_holding_change_1y_pct"),
                    features.get("fii_holding_change_1y_pct"), features.get("dii_holding_change_1y_pct"),
                    features.get("as_of_date"), features.get("scraped_at"),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_symbol_state(symbol: str, statement_scope: str, *, status: str, fetched_at: str,
                        etag: str | None, last_modified: str | None, fingerprint: str | None,
                        source_url: str | None, error: str | None) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fundamentals_symbol_state
                    (symbol, statement_scope, status, etag, last_modified, content_fingerprint,
                     last_checked_at, last_success_at, last_error, source_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, statement_scope) DO UPDATE SET
                    status=EXCLUDED.status, etag=EXCLUDED.etag, last_modified=EXCLUDED.last_modified,
                    content_fingerprint=EXCLUDED.content_fingerprint, last_checked_at=EXCLUDED.last_checked_at,
                    last_success_at=COALESCE(EXCLUDED.last_success_at, public.fundamentals_symbol_state.last_success_at),
                    last_error=EXCLUDED.last_error, source_url=COALESCE(EXCLUDED.source_url, public.fundamentals_symbol_state.source_url)
                """,
                (symbol, statement_scope, status, etag, last_modified, fingerprint, fetched_at,
                 fetched_at if status in {"success", "unchanged"} else None, error, source_url),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _start_run(config: SyncConfig, requested: int) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.fundamentals_sync_runs (run_id, scope_type, scope_value, mode, symbols_requested) "
                "VALUES (%s::uuid,%s,%s,%s,%s)",
                (str(run_id), config.scope.scope_type, config.scope.scope_value, config.mode, requested),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return run_id


def _finish_run(run_id: uuid.UUID, *, changed: int, unchanged: int, failed: int, skipped: int,
                status: str, error: str | None = None) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.fundamentals_sync_runs SET symbols_changed=%s, symbols_unchanged=%s, "
                "symbols_failed=%s, symbols_skipped=%s, finished_at=now(), status=%s, error=%s WHERE run_id=%s::uuid",
                (changed, unchanged, failed, skipped, status, error, str(run_id)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def run_fundamentals_sync(config: SyncConfig) -> dict[str, Any]:
    if _sync_lock.locked():
        raise RuntimeError("fundamentals sync already in progress")
    async with _sync_lock:
        symbols = resolve_scope_symbols(config.scope)
        if config.on_demand and len(symbols) > MAX_ON_DEMAND_SYMBOLS:
            raise ValueError(f"on-demand sync limited to {MAX_ON_DEMAND_SYMBOLS} symbols per request")
        run_id = _start_run(config, len(symbols))
        screener = ScreenerClient()
        changed = unchanged = failed = skipped = 0
        errors: list[dict[str, str]] = []
        try:
            ensure_screener_parser_ready()
            for offset, symbol in enumerate(symbols, start=1):
                state = _load_symbol_state(symbol, config.statement_scope)
                if not _should_fetch(state, config):
                    skipped += 1
                    continue
                try:
                    result = await screener.fetch_company_page(
                        symbol,
                        statement_scope=config.statement_scope,
                        if_none_match=state.get("etag"),
                        if_modified_since=state.get("last_modified"),
                    )
                    if result.not_modified:
                        unchanged += 1
                        _write_symbol_state(symbol, config.statement_scope, status="unchanged",
                                            fetched_at=result.fetched_at, etag=result.etag,
                                            last_modified=result.last_modified,
                                            fingerprint=state.get("content_fingerprint"),
                                            source_url=result.source_url, error=None)
                    else:
                        parsed = parse_screener_company_page(result)
                        _validate_parsed_datasets(parsed)
                        fingerprint = _fingerprint(parsed)
                        if fingerprint == state.get("content_fingerprint"):
                            unchanged += 1
                            _write_symbol_state(symbol, config.statement_scope, status="unchanged",
                                                fetched_at=result.fetched_at, etag=result.etag,
                                                last_modified=result.last_modified, fingerprint=fingerprint,
                                                source_url=result.source_url, error=None)
                        else:
                            _upsert_symbol(symbol, config.statement_scope, parsed, result.fetched_at)
                            changed += 1
                            _write_symbol_state(symbol, config.statement_scope, status="success",
                                                fetched_at=result.fetched_at, etag=result.etag,
                                                last_modified=result.last_modified, fingerprint=fingerprint,
                                                source_url=result.source_url, error=None)
                except (ImportError, ModuleNotFoundError) as exc:
                    # Fatal setup error (missing HTML parser dependency): abort loudly.
                    raise
                except Exception as exc:  # per-symbol isolation
                    failed += 1
                    message = f"{type(exc).__name__}: {exc}"
                    errors.append({"symbol": symbol, "error": message})
                    _write_symbol_state(symbol, config.statement_scope, status="failed",
                                        fetched_at=datetime.now(UTC).isoformat(),
                                        etag=state.get("etag"), last_modified=state.get("last_modified"),
                                        fingerprint=state.get("content_fingerprint"),
                                        source_url=state.get("source_url"), error=message)
                if offset < len(symbols) and config.min_delay_seconds > 0:
                    await asyncio.sleep(config.min_delay_seconds)
            _finish_run(run_id, changed=changed, unchanged=unchanged, failed=failed,
                        skipped=skipped, status="completed")
        except Exception as exc:
            _finish_run(run_id, changed=changed, unchanged=unchanged, failed=failed,
                        skipped=skipped, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        return {"run_id": str(run_id), "scope": {"scope_type": config.scope.scope_type, "scope_value": config.scope.scope_value},
                "mode": config.mode, "symbols_requested": len(symbols), "symbols_changed": changed,
                "symbols_unchanged": unchanged, "symbols_failed": failed,
                "symbols_skipped": skipped, "failed_symbols": errors}
