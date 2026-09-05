"""Company fundamentals read/refresh routes for external workers.

Mounted under the authenticated worker prefix (``/algo-workers``) so the SDK
methods authenticate with the worker bearer token exactly like every other
worker surface; reads and the refresh require ``market:read``. The app-wide
auth middleware otherwise 401s all non-worker ``/api`` paths, which would make
unauthenticated LAN-internal routes unusable for SDK consumers.

All read responses carry a versioned envelope: ``schema_version: 1``,
``source: "screener"``, and per-row ``as_of_date``/``scraped_at`` freshness.
``POST .../fundamentals/sync`` is the only mutating route and shares the
single sync engine with the nightly scheduler.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator

from backend.api.routers.worker_shared import _require_action, require_worker_token
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
router = APIRouter(prefix="/algo-workers", tags=["Algo Workers"])


def _envelope() -> dict:
    """Shared read-envelope fields, consistent with the 0.7.6 contracts."""
    return {"schema_version": 1, "source": "screener", "retrieved_at": datetime.now(timezone.utc).isoformat()}


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


async def _authorize(request: Request, schema_version: int = 1) -> None:
    if schema_version != 1:
        raise HTTPException(422, {"rejection_reason": "UNSUPPORTED_SCHEMA_VERSION", "supported": [1]})
    token = await require_worker_token(request)
    _require_action(token, "market:read")


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


@router.post("/worker/fundamentals/sync")
async def trigger_sync(request: Request, payload: SyncRequest):
    await _authorize(request)
    scope = _scope_from_request(payload.symbols, payload.index)
    try:
        return await run_fundamentals_sync(SyncConfig(scope=scope, mode=payload.mode, on_demand=True))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _load_status_rows(wanted: List[str]) -> dict:
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
            return {"rows": rows, "runs": runs}
    finally:
        conn.close()


@router.get("/worker/fundamentals/status")
async def sync_status(request: Request, symbols: Optional[List[str]] = Query(None), index: Optional[str] = Query(None), schema_version: int = Query(1, ge=1)):
    await _authorize(request, schema_version)
    wanted = _resolve_scope_filter(symbols, index)
    result = await asyncio.to_thread(_load_status_rows, wanted)
    rows, runs = result["rows"], result["runs"]
    found = {row["symbol"] for row in rows}
    return {**_envelope(),
            "symbols": rows,
            "missing_symbols": [s for s in wanted if s not in found],
            "recent_runs": runs}


def _load_feature_rows(wanted: List[str]) -> List[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.fundamentals_features WHERE symbol = ANY(%s) ORDER BY symbol", (wanted,))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, r)) for r in cur.fetchall()]
    finally:
        conn.close()


@router.get("/worker/fundamentals/features")
async def features(request: Request, symbols: Optional[List[str]] = Query(None), index: Optional[str] = Query(None), schema_version: int = Query(1, ge=1)):
    await _authorize(request, schema_version)
    wanted = _resolve_scope_filter(symbols, index)
    rows = await asyncio.to_thread(_load_feature_rows, wanted)
    for row in rows:
        if row.get("as_of_date"):
            row["as_of_date"] = row["as_of_date"].isoformat()
        if row.get("scraped_at"):
            row["scraped_at"] = row["scraped_at"].isoformat()
    found = {row["symbol"] for row in rows}
    return {**_envelope(), "features": rows,
            "missing_symbols": [s for s in wanted if s not in found]}


def _load_statement_rows(symbol: str, statement_scope: str, dataset: str) -> List[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dataset, period_end, metric_key, metric_name, value_text, numeric_value, scraped_at "
                "FROM public.fundamentals_metrics WHERE symbol=%s AND statement_scope=%s AND dataset=%s "
                "ORDER BY period_end, metric_key",
                (symbol, statement_scope, dataset),
            )
            return [
                {"dataset": r[0], "period_end": r[1].isoformat() if r[1] else None, "metric_key": r[2],
                 "metric_name": r[3], "value_text": r[4], "numeric_value": r[5],
                 "scraped_at": r[6].isoformat() if r[6] else None}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


@router.get("/worker/fundamentals/statements")
async def statements(request: Request, symbol: str, dataset: str, statement_scope: str = "consolidated", schema_version: int = Query(1, ge=1)):
    await _authorize(request, schema_version)
    symbol = symbol.strip().upper()
    if dataset not in DATASETS:
        raise HTTPException(400, f"dataset must be one of {DATASETS}")
    rows = await asyncio.to_thread(_load_statement_rows, symbol, statement_scope, dataset)
    if not rows:
        raise HTTPException(404, f"no {dataset} rows stored for {symbol} ({statement_scope})")
    return {**_envelope(), "symbol": symbol,
            "statement_scope": statement_scope, "dataset": dataset, "rows": rows}


def _render_export_csv(wanted: List[str], dataset: str) -> str:
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
    return buffer.getvalue()


@router.get("/worker/fundamentals/export.csv")
async def export_csv(request: Request, symbols: Optional[List[str]] = Query(None), index: Optional[str] = Query(None),
                     dataset: str = "fundamentals_features", schema_version: int = Query(1, ge=1)):
    await _authorize(request, schema_version)
    wanted = _resolve_scope_filter(symbols, index)
    content = await asyncio.to_thread(_render_export_csv, wanted, dataset)
    return StreamingResponse(iter([content]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=fundamentals_{dataset}.csv"})
