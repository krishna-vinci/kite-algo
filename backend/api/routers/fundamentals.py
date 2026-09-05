"""Company fundamentals read/refresh routes (LAN-internal, no per-route auth).

All read responses carry a versioned envelope: ``schema_version: 1``,
``source: "screener"``, and per-row ``as_of_date``/``scraped_at`` freshness.
``POST /fundamentals/sync`` is the only mutating route and shares the single
sync engine with the nightly scheduler.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator

from backend.app.database import get_db_connection
from fundamentals.index_scopes import canonical_index_key, is_supported_index, supported_index_scopes
from fundamentals.ingestion import (
    DATASETS,
    MAX_ON_DEMAND_SYMBOLS,
    SyncConfig,
    SyncScope,
    resolve_scope_symbols,
    run_fundamentals_sync,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fundamentals"])


class SyncRequest(BaseModel):
    symbols: Optional[List[str]] = None
    index: Optional[str] = None
    mode: str = "incremental"

    @model_validator(mode="after")
    def _exclusive_scope(self):
        if bool(self.symbols) == bool(self.index):
            raise ValueError("provide exactly one of 'symbols' or 'index'")
        if self.mode not in {"incremental", "full"}:
            raise ValueError("mode must be 'incremental' or 'full'")
        return self


def _scope_from_request(symbols: Optional[List[str]], index: Optional[str]) -> SyncScope:
    if symbols:
        if len(symbols) > MAX_ON_DEMAND_SYMBOLS:
            raise HTTPException(400, f"symbols limited to {MAX_ON_DEMAND_SYMBOLS} per request")
        return SyncScope(scope_type="symbols", scope_value=",".join(symbols))
    if not is_supported_index(index):
        raise HTTPException(400, f"index must be one of {supported_index_scopes()}")
    return SyncScope(scope_type="index", scope_value=canonical_index_key(index))


@router.post("/fundamentals/sync")
async def trigger_sync(payload: SyncRequest):
    scope = _scope_from_request(payload.symbols, payload.index)
    try:
        return await run_fundamentals_sync(SyncConfig(scope=scope, mode=payload.mode, on_demand=True))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _resolve_scope_filter(symbols: Optional[List[str]], index: Optional[str]) -> List[str]:
    if bool(symbols) == bool(index):
        raise HTTPException(400, "provide exactly one of 'symbols' or 'index'")
    if symbols:
        wanted = [s.strip().upper() for s in symbols if s.strip()]
        if not wanted:
            raise HTTPException(400, "symbols must not be empty")
        return wanted
    if not is_supported_index(index):
        raise HTTPException(400, f"index must be one of {supported_index_scopes()}")
    try:
        return resolve_scope_symbols(SyncScope(scope_type="index", scope_value=canonical_index_key(index)))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/fundamentals/status")
def sync_status(symbols: Optional[List[str]] = Query(None), index: Optional[str] = Query(None)):
    wanted = _resolve_scope_filter(symbols, index)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, statement_scope, status, last_checked_at, last_success_at, last_error "
                "FROM public.fundamentals_symbol_state WHERE symbol = ANY(%s) ORDER BY symbol",
                (wanted,),
            )
            rows = [
                {"symbol": r[0], "statement_scope": r[1], "status": r[2],
                 "last_checked_at": r[3].isoformat() if r[3] else None,
                 "last_success_at": r[4].isoformat() if r[4] else None,
                 "last_error": r[5]}
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT scope_type, scope_value, mode, symbols_requested, symbols_changed, symbols_unchanged, "
                "symbols_failed, symbols_skipped, started_at, finished_at, status FROM public.fundamentals_sync_runs "
                "ORDER BY started_at DESC LIMIT 10"
            )
            runs = [
                {"scope_type": r[0], "scope_value": r[1], "mode": r[2], "symbols_requested": r[3],
                 "symbols_changed": r[4], "symbols_unchanged": r[5], "symbols_failed": r[6],
                 "symbols_skipped": r[7], "started_at": r[8].isoformat() if r[8] else None,
                 "finished_at": r[9].isoformat() if r[9] else None, "status": r[10]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()
    found = {row["symbol"] for row in rows}
    return {"schema_version": 1, "source": "screener",
            "symbols": rows,
            "missing_symbols": [s for s in wanted if s not in found],
            "recent_runs": runs}


@router.get("/fundamentals/features")
def features(symbols: Optional[List[str]] = Query(None), index: Optional[str] = Query(None)):
    wanted = _resolve_scope_filter(symbols, index)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.fundamentals_features WHERE symbol = ANY(%s) ORDER BY symbol", (wanted,))
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for row in rows:
        if row.get("as_of_date"):
            row["as_of_date"] = row["as_of_date"].isoformat()
        if row.get("scraped_at"):
            row["scraped_at"] = row["scraped_at"].isoformat()
    found = {row["symbol"] for row in rows}
    return {"schema_version": 1, "source": "screener", "features": rows,
            "missing_symbols": [s for s in wanted if s not in found]}


@router.get("/fundamentals/statements")
def statements(symbol: str, dataset: str, statement_scope: str = "consolidated"):
    symbol = symbol.strip().upper()
    if dataset not in DATASETS:
        raise HTTPException(400, f"dataset must be one of {DATASETS}")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dataset, period_end, metric_key, metric_name, value_text, numeric_value, scraped_at "
                "FROM public.fundamentals_metrics WHERE symbol=%s AND statement_scope=%s AND dataset=%s "
                "ORDER BY period_end, metric_key",
                (symbol, statement_scope, dataset),
            )
            rows = [
                {"dataset": r[0], "period_end": r[1].isoformat() if r[1] else None, "metric_key": r[2],
                 "metric_name": r[3], "value_text": r[4], "numeric_value": r[5],
                 "scraped_at": r[6].isoformat() if r[6] else None}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()
    if not rows:
        raise HTTPException(404, f"no {dataset} rows stored for {symbol} ({statement_scope})")
    return {"schema_version": 1, "source": "screener", "symbol": symbol,
            "statement_scope": statement_scope, "dataset": dataset, "rows": rows}


@router.get("/fundamentals/export.csv")
def export_csv(symbols: Optional[List[str]] = Query(None), index: Optional[str] = Query(None),
               dataset: str = "fundamentals_features"):
    wanted = _resolve_scope_filter(symbols, index)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if dataset == "fundamentals_features":
                cur.execute("SELECT * FROM public.fundamentals_features WHERE symbol = ANY(%s) ORDER BY symbol", (wanted,))
            elif dataset in DATASETS:
                cur.execute(
                    "SELECT symbol, statement_scope, dataset, period_end, metric_key, metric_name, value_text, numeric_value "
                    "FROM public.fundamentals_metrics WHERE dataset=%s AND symbol = ANY(%s) ORDER BY symbol, period_end",
                    (dataset, wanted),
                )
            else:
                raise HTTPException(400, f"dataset must be 'fundamentals_features' or one of {DATASETS}")
            columns = [desc[0] for desc in cur.description]
            writer.writerow(columns)
            for row in cur.fetchall():
                writer.writerow([v.isoformat() if hasattr(v, "isoformat") else v for v in row])
    finally:
        conn.close()
    buffer.seek(0)
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=fundamentals_{dataset}.csv"})
