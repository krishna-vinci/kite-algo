import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import psycopg2.extras
import redis.asyncio as redis
from fastapi import APIRouter, HTTPException, Query, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from broker_api.index_ingestion import (
    ensure_fresh_live_metrics,
    get_index_refresh_state,
    normalize_source_list,
    refresh_live_metrics,
)
from broker_api.redis_events import get_redis
from database import get_db_connection


router = APIRouter(tags=["Marketwatch"])


def _validated_source_list(source_list: str) -> str:
    try:
        return normalize_source_list(source_list)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class OverlaySnapshotTick(BaseModel):
    instrument_token: int
    last_price: float
    change_percent: Optional[float] = None
    tick_timestamp: int
    server_timestamp: int
    age_ms: Optional[int] = None
    source: str


class OverlaySnapshotResponse(BaseModel):
    status: str
    data: Dict[str, OverlaySnapshotTick]


async def _get_index_data(source_list: str):
    conn = None
    try:
        if source_list in {"Nifty50", "NiftyBank"}:
            try:
                await asyncio.to_thread(ensure_fresh_live_metrics, source_list)
            except Exception:
                logging.warning("Failed to auto-refresh live metrics for %s", source_list, exc_info=True)
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM kite_ticker_tickers WHERE source_list = %s ORDER BY sector, index_weight DESC NULLS LAST, tradingsymbol",
                (source_list,),
            )
            sectors = {}
            for row in cur.fetchall():
                sectors.setdefault(row["sector"], []).append(dict(row))
            return sectors
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.get("/nifty50")
async def get_nifty50_data():
    return await _get_index_data("Nifty50")


@router.get("/indices/{source_list}")
async def get_index_data(source_list: str):
    return await _get_index_data(_validated_source_list(source_list))


@router.get("/indices/{source_list}/status")
async def get_index_status(source_list: str):
    return get_index_refresh_state(_validated_source_list(source_list))


async def _get_overlay_snapshot(source_list: str, token: Optional[List[int]] = Query(None, description="List of instrument tokens to fetch.")):
    if token and (len(token) == 0 or len(token) > 2000):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid token count"})

    conn = None
    try:
        redis_client = get_redis()
        today_iso = datetime.utcnow().strftime("%Y-%m-%d")

        target_tokens = token
        if not target_tokens:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT instrument_token FROM kite_ticker_tickers WHERE source_list = %s", (source_list,))
                target_tokens = [row[0] for row in cur.fetchall()]

        keys = [f"marketwatch:overlay:{today_iso}:{t}" for t in target_tokens]

        try:
            overlay_data_raw = await redis_client.mget(keys)
        except redis.exceptions.ConnectionError:
            logging.warning("Redis connection error in overlay snapshot; returning empty data.")
            return {"status": "success", "data": {}}

        results: Dict[str, OverlaySnapshotTick] = {}
        tokens_missing_change = []
        server_ts = int(datetime.utcnow().timestamp() * 1000)

        for i, raw_val in enumerate(overlay_data_raw):
            if raw_val is None:
                continue
            try:
                overlay_tick = json.loads(raw_val)
                instrument_token = overlay_tick["instrument_token"]
                tick_ts = overlay_tick.get("tick_timestamp")
                snapshot = OverlaySnapshotTick(
                    instrument_token=instrument_token,
                    last_price=overlay_tick["last_price"],
                    change_percent=overlay_tick.get("change_percent"),
                    tick_timestamp=tick_ts,
                    server_timestamp=server_ts,
                    age_ms=server_ts - tick_ts if tick_ts else None,
                    source="ws",
                )
                results[str(instrument_token)] = snapshot
                if snapshot.change_percent is None:
                    tokens_missing_change.append(instrument_token)
            except (json.JSONDecodeError, KeyError) as e:
                logging.warning("Failed to parse overlay data for key %s: %s", keys[i], e)

        if tokens_missing_change:
            if conn is None:
                conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT instrument_token, last_close FROM kite_instruments WHERE instrument_token = ANY(%s)",
                    (tokens_missing_change,),
                )
                baseline_closes = {row["instrument_token"]: row["last_close"] for row in cur.fetchall()}

            for token_to_update in tokens_missing_change:
                baseline_close = baseline_closes.get(token_to_update)
                if baseline_close and baseline_close > 0:
                    snapshot_to_update = results[str(token_to_update)]
                    snapshot_to_update.change_percent = 100 * (snapshot_to_update.last_price / baseline_close - 1)

        return {"status": "success", "data": results}
    except Exception as e:
        logging.error("Error in overlay snapshot endpoint: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": "Internal server error"})
    finally:
        if conn:
            conn.close()


@router.get(
    "/marketwatch/{source_list}/overlay-snapshot",
    response_model=OverlaySnapshotResponse,
    summary="Get a snapshot of the latest live ticks from the Redis overlay cache for an index.",
)
async def get_index_overlay_snapshot(
    source_list: str,
    token: Optional[List[int]] = Query(None, description="List of instrument tokens to fetch."),
):
    return await _get_overlay_snapshot(_validated_source_list(source_list), token)


@router.get(
    "/marketwatch/nifty50/overlay-snapshot",
    response_model=OverlaySnapshotResponse,
    summary="Get a snapshot of the latest live ticks from the Redis overlay cache.",
)
async def get_overlay_snapshot(token: Optional[List[int]] = Query(None, description="List of instrument tokens to fetch.")):
    return await _get_overlay_snapshot("Nifty50", token)


@router.post("/marketwatch/{source_list}/finalize-baseline")
async def finalize_index_baseline(source_list: str, dry_run: bool = False):
    if dry_run:
        return {
            "status": "success",
            "message": "Dry-run is not supported for the live metric refresh flow.",
            "source_list": _validated_source_list(source_list),
        }
    result = refresh_live_metrics(_validated_source_list(source_list))
    status_code = 200 if result["status"] != "error" else 500
    return JSONResponse(status_code=status_code, content=result)


@router.post("/marketwatch/nifty50/finalize-baseline")
async def finalize_nifty50_baseline(dry_run: bool = False):
    return await finalize_index_baseline("Nifty50", dry_run=dry_run)


@router.websocket("/ws/marketwatch")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "error",
            "message": "Legacy Python marketwatch websocket has been removed. Connect directly to the Go market-runtime /ws/marketwatch endpoint.",
        }
    )
    await websocket.close(code=1013, reason="Use Go market-runtime websocket")
