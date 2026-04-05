import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import psycopg2.extras
import redis.asyncio as redis
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from kiteconnect import KiteConnect
from pydantic import BaseModel

from broker_api.kite_auth import API_KEY
from broker_api.redis_events import get_redis
from broker_api.broker_api import get_system_access_token
from database import SessionLocal, get_db_connection, get_user_settings


router = APIRouter(tags=["Marketwatch"])

VALID_MODES = {"ltp", "quote", "full"}


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


def _extract_tokens_from_subs(_subs: dict, token_set: set[int]) -> int:
    if not _subs or not isinstance(_subs, dict):
        return 0
    token_count = 0
    for group in _subs.get("groups", []) or []:
        if not group or not isinstance(group, dict):
            continue
        if isinstance(group.get("tokens"), list):
            for t in group["tokens"]:
                try:
                    token_set.add(int(t))
                    token_count += 1
                except (ValueError, TypeError):
                    pass
        elif isinstance(group.get("instruments"), list):
            for inst in group["instruments"]:
                token = (inst or {}).get("instrument_token") or (inst or {}).get("token")
                if token is None:
                    continue
                try:
                    token_set.add(int(token))
                    token_count += 1
                except (ValueError, TypeError):
                    pass

    if isinstance(_subs.get("layouts"), dict):
        for tokens in _subs["layouts"].values():
            if isinstance(tokens, list):
                for t in tokens:
                    if t is not None:
                        try:
                            token_set.add(int(t))
                            token_count += 1
                        except (ValueError, TypeError):
                            pass
    return token_count


def _build_initial_subscriptions_from_settings(settings: dict) -> Dict[int, str]:
    token_set: set[int] = set()
    legacy = settings.get("subscriptions")
    sb = settings.get("subscriptions_sidebar")
    mw = settings.get("subscriptions_marketwatch")
    nfo = settings.get("subscriptions_nfo-charts")
    nfo_layouts = settings.get("subscriptions_nfo-charts-layouts")

    for candidate in (legacy, sb, mw, nfo, nfo_layouts):
        if isinstance(candidate, dict):
            _extract_tokens_from_subs(candidate, token_set)

    mode = None
    for candidate in (nfo, nfo_layouts, legacy, sb, mw):
        if isinstance(candidate, dict):
            candidate_mode = candidate.get("mode")
            if candidate_mode in VALID_MODES:
                mode = candidate_mode
                if mode == "full":
                    break

    return {int(token): (mode or "quote") for token in token_set}


@router.get("/nifty50")
async def get_nifty50_data():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM kite_ticker_tickers WHERE source_list = 'Nifty50' ORDER BY sector")
            sectors = {}
            for row in cur.fetchall():
                sectors.setdefault(row["sector"], []).append(dict(row))
            return sectors
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router.get(
    "/marketwatch/nifty50/overlay-snapshot",
    response_model=OverlaySnapshotResponse,
    summary="Get a snapshot of the latest live ticks from the Redis overlay cache.",
)
async def get_overlay_snapshot(token: Optional[List[int]] = Query(None, description="List of instrument tokens to fetch.")):
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
                cur.execute("SELECT instrument_token FROM kite_ticker_tickers WHERE source_list = 'Nifty50'")
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


@router.post("/marketwatch/nifty50/finalize-baseline")
async def finalize_nifty50_baseline(dry_run: bool = False):
    conn = None
    debug_info = {"errors": [], "warnings": []}
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT instrument_token, tradingsymbol, exchange, freefloat_marketcap, index_weight FROM kite_ticker_tickers WHERE source_list = 'Nifty50'"
            )
            instruments = cur.fetchall()

        db = SessionLocal()
        try:
            access_token = get_system_access_token(db)
            if not access_token:
                raise HTTPException(status_code=500, detail="System access token not available.")
            kite = KiteConnect(api_key=API_KEY)
            kite.set_access_token(access_token)
        finally:
            db.close()

        instrument_keys = [f"{inst['exchange']}:{inst['tradingsymbol']}" for inst in instruments]
        try:
            ohlc_data = kite.quote(instrument_keys)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch OHLC data: {str(e)}")

        updates = []
        total_new_marketcap = 0
        for inst in instruments:
            try:
                key = f"{inst['exchange']}:{inst['tradingsymbol']}"
                data = ohlc_data.get(key)
                if not data or "ohlc" not in data or "last_price" not in data:
                    continue

                ltp = data["last_price"]
                previous_close = data["ohlc"]["close"]
                open_price = data["ohlc"]["open"]
                high_price = data["ohlc"]["high"]
                low_price = data["ohlc"]["low"]
                net_change = ltp - previous_close
                net_change_percent = (net_change / previous_close * 100) if previous_close > 0 else 0
                return_ratio = net_change_percent / 100
                freefloat_mc = float(inst["freefloat_marketcap"] or 0)
                idx_weight = float(inst["index_weight"] or 0)
                new_freefloat_marketcap = freefloat_mc * (1 + return_ratio)
                return_attribution = idx_weight * return_ratio
                total_new_marketcap += new_freefloat_marketcap
                updates.append(
                    {
                        "instrument_token": inst["instrument_token"],
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "close": previous_close,
                        "ltp": ltp,
                        "net_change": net_change,
                        "net_change_percent": net_change_percent,
                        "freefloat_marketcap": new_freefloat_marketcap,
                        "return_attribution": return_attribution,
                        "index_weight": 0,
                    }
                )
            except Exception as e:
                debug_info["errors"].append(str(e))

        if total_new_marketcap > 0:
            for update in updates:
                update["index_weight"] = (update["freefloat_marketcap"] / total_new_marketcap) * 100

        if dry_run:
            return {"status": "success", "preview": updates, "debug": debug_info}

        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                """
                UPDATE kite_ticker_tickers
                SET
                    open = %(open)s,
                    high = %(high)s,
                    low = %(low)s,
                    close = %(close)s,
                    ltp = %(ltp)s,
                    net_change = %(net_change)s,
                    net_change_percent = %(net_change_percent)s,
                    freefloat_marketcap = %(freefloat_marketcap)s,
                    return_attribution = %(return_attribution)s,
                    index_weight = %(index_weight)s,
                    last_updated = NOW()
                WHERE instrument_token = %(instrument_token)s AND source_list = 'Nifty50';
                """,
                updates,
            )
        conn.commit()

        return {"status": "success", "data": {"updated": len(updates), "errors": debug_info["errors"]}}
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail={"error": str(e), "debug": debug_info})
    finally:
        if conn:
            conn.close()


@router.websocket("/ws/marketwatch")
async def websocket_endpoint(websocket: WebSocket):
    manager = getattr(websocket.app.state, "ws_manager", None)
    if manager is None:
        logging.error("WebSocket connection rejected: ws_manager is None")
        await websocket.close(code=1011, reason="WebSocketManager not initialized")
        return

    await manager.connect(websocket)
    db = SessionLocal()
    try:
        settings = get_user_settings(db) or {}
        initial_subscriptions = _build_initial_subscriptions_from_settings(settings)
        if initial_subscriptions:
            all_tokens = list(initial_subscriptions.keys())
            initial_mode = next(iter(initial_subscriptions.values()), "quote")
            await manager.subscribe(websocket, all_tokens, initial_mode)
    finally:
        db.close()

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if not action:
                await websocket.send_text(json.dumps({"type": "error", "message": "Missing action"}))
                continue

            if action == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            tokens = data.get("tokens") or []
            mode = data.get("mode")
            if tokens and isinstance(tokens, list):
                try:
                    tokens = [int(t) for t in tokens]
                except Exception:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Invalid tokens"}))
                    continue

            if action == "subscribe" and tokens:
                await manager.subscribe(websocket, tokens, mode)
            elif action == "unsubscribe" and tokens:
                await manager.unsubscribe(websocket, tokens)
            elif action == "set_mode" and tokens and mode:
                await manager.set_mode(websocket, tokens, mode)
            else:
                await websocket.send_text(json.dumps({"type": "error", "message": "Invalid action"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        logging.info("Client disconnected.")
    except Exception as e:
        logging.error("Error in websocket endpoint: %s", e, exc_info=True)
        manager.disconnect(websocket)
