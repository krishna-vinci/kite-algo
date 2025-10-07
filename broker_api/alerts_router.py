import asyncio
import json
from fastapi import APIRouter, HTTPException, Request, Query, Path
from starlette.responses import StreamingResponse
from typing import Optional, Literal, List, Dict, Any, Tuple
from pydantic import BaseModel
from database import database as async_db
from broker_api.redis_events import pubsub_iter
import logging

router = APIRouter()

async def _engine_refresh(request: Request) -> None:
    """
    Hint the in-process alerts engine to refresh active set and subscriptions immediately.
    Best-effort; failures are logged and ignored.
    """
    try:
        eng = getattr(request.app.state, "alerts_engine", None)
        if eng:
            await eng.refresh_now()
    except Exception as e:
        logging.error("[ALERTS] engine refresh failed: %s", e)

Comparator = Literal["gt", "lt"]
TargetType = Literal["absolute", "percent"]

class AlertCreate(BaseModel):
    instrument_token: int
    comparator: Comparator
    target_type: TargetType
    absolute_target: Optional[float] = None
    percent: Optional[float] = None
    name: Optional[str] = None
    notes: Optional[str] = None
    one_time: Optional[bool] = True
    baseline_price: Optional[float] = None

class AlertPatch(BaseModel):
    comparator: Optional[Comparator] = None
    target_type: Optional[TargetType] = None
    absolute_target: Optional[float] = None
    percent: Optional[float] = None
    name: Optional[str] = None
    notes: Optional[str] = None
    one_time: Optional[bool] = None

def _get_ws_baseline(request: Request, instrument_token: int) -> Optional[float]:
    try:
        ws_mgr = getattr(request.app.state, "ws_manager", None)
        if ws_mgr and getattr(ws_mgr, "latest_ticks", None):
            tick = ws_mgr.latest_ticks.get(int(instrument_token))
            if isinstance(tick, dict):
                lp = tick.get("last_price")
                if lp is not None:
                    return float(lp)
    except Exception:
        pass
    return None

def _compute_absolute_from_percent(baseline: float, percent: float, comparator: str) -> float:
    p = float(percent)
    b = float(baseline)
    if comparator == "gt":
        return b * (1.0 + p / 100.0)
    else:
        return b * (1.0 - p / 100.0)

# POST /alerts
@router.post("")
async def create_alert(req: Request, body: AlertCreate):
    # Validation of field combinations
    if body.target_type == "absolute":
        if body.absolute_target is None:
            raise HTTPException(status_code=400, detail="absolute_target is required when target_type='absolute'")
        if body.percent is not None:
            raise HTTPException(status_code=400, detail="percent must be omitted when target_type='absolute'")
        baseline_price = body.baseline_price  # optional for absolute
        absolute_target = float(body.absolute_target)
        percent = None
    else:
        if body.percent is None:
            raise HTTPException(status_code=400, detail="percent is required when target_type='percent'")
        # Resolve baseline
        baseline_price = body.baseline_price
        if baseline_price is None:
            baseline_price = _get_ws_baseline(req, body.instrument_token)
        if baseline_price is None:
            # No baseline available and not provided explicitly -> 424
            raise HTTPException(
                status_code=424,
                detail="Baseline price unavailable. Provide 'baseline_price' explicitly or retry after LTP helper is available."
            )
        absolute_target = _compute_absolute_from_percent(float(baseline_price), float(body.percent), body.comparator)
        percent = float(body.percent)

    sql = """
    INSERT INTO public.alerts (
        instrument_token, comparator, target_type, absolute_target, percent, baseline_price,
        one_time, name, notes, last_evaluated_price
    ) VALUES (
        :instrument_token, :comparator, :target_type, :absolute_target, :percent, :baseline_price,
        :one_time, :name, :notes, :last_evaluated_price
    )
    RETURNING *;
    """
    values = {
        "instrument_token": int(body.instrument_token),
        "comparator": body.comparator,
        "target_type": body.target_type,
        "absolute_target": float(absolute_target) if absolute_target is not None else None,
        "percent": percent,
        "baseline_price": float(baseline_price) if baseline_price is not None else None,
        "one_time": True if body.one_time is None else bool(body.one_time),
        "name": body.name,
        "notes": body.notes,
    }
    # Seed last_evaluated_price with best-known price at creation
    initial_ltp = _get_ws_baseline(req, body.instrument_token)
    if initial_ltp is None:
        initial_ltp = baseline_price

    values["last_evaluated_price"] = float(initial_ltp) if initial_ltp is not None else None

    row = await async_db.fetch_one(sql, values)
    try:
        await _engine_refresh(req)
    except Exception:
        pass
    return dict(row) if row else {}

# GET /alerts
@router.get("")
async def list_alerts(
    status: Optional[str] = Query(default=None),
    instrument_token: Optional[int] = Query(default=None),
    instrument_name: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="-created_at")
):
    where: List[str] = []
    params: Dict[str, Any] = {"limit": min(max(limit, 1), 200), "offset": max(offset, 0)}
    if status:
        where.append("status = :status")
        params["status"] = status

    instrument_token_filter: Optional[Tuple[str, int]] = None
    if instrument_name is not None:
        instrument_sql = "SELECT instrument_token FROM public.kite_instruments WHERE tradingsymbol = :instrument_name"
        instrument_row = await async_db.fetch_one(instrument_sql, {"instrument_name": instrument_name})
        if instrument_row:
            instrument_token_filter = ("instrument_token = :instrument_token", int(instrument_row['instrument_token']))
        else:
            # No instrument found with that name, return empty list
            return {"items": [], "total": 0, "limit": params["limit"], "offset": params["offset"]}

    if instrument_token is not None:
        where.append("instrument_token = :instrument_token")
        params["instrument_token"] = int(instrument_token)
    elif instrument_token_filter is not None:
        where.append(instrument_token_filter[0])
        params["instrument_token"] = instrument_token_filter[1]

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    order_map = {
        "created_at": "created_at ASC",
        "-created_at": "created_at DESC",
        "updated_at": "updated_at ASC",
        "-updated_at": "updated_at DESC",
    }
    order_by = order_map.get(sort, "created_at DESC")
    items_sql = f"SELECT a.*, i.tradingsymbol FROM public.alerts a LEFT JOIN public.kite_instruments i ON a.instrument_token = i.instrument_token {where_clause} ORDER BY {order_by} LIMIT :limit OFFSET :offset"
    count_sql = f"SELECT COUNT(*) AS c FROM public.alerts a LEFT JOIN public.kite_instruments i ON a.instrument_token = i.instrument_token {where_clause}"
    rows = await async_db.fetch_all(items_sql, params)
    count_params = {k: v for k, v in params.items() if k in ("status", "instrument_token", "instrument_name")}
    cnt_row = await async_db.fetch_one(count_sql, count_params)
    total = int(cnt_row["c"]) if cnt_row and "c" in cnt_row else 0
    items = [dict(r) for r in rows]
    for item in items:
        item['tradingsymbol'] = item.pop('tradingsymbol', None)
    return {"items": items, "total": total, "limit": params["limit"], "offset": params["offset"]}

# GET /alerts/{id}
@router.get("/{id}")
async def get_alert(id: str = Path(..., description="Alert UUID")):
    row = await async_db.fetch_one("SELECT * FROM public.alerts WHERE id = :id", {"id": id})
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    return dict(row)

# PATCH /alerts/{id}
@router.patch("/{id}")
async def update_alert(id: str, request: Request):
    body = await request.json()
    if "status" in body:
        raise HTTPException(status_code=400, detail="Direct status updates are not allowed. Use state action endpoints.")
    patch = AlertPatch(**body)

    # Load existing
    existing = await async_db.fetch_one("SELECT * FROM public.alerts WHERE id = :id", {"id": id})
    if not existing:
        raise HTTPException(status_code=404, detail="Alert not found")

    new_comparator = patch.comparator or existing["comparator"]
    new_target_type = patch.target_type or existing["target_type"]
    updates: Dict[str, Any] = {}

    # Name/notes/one_time simple updates
    if patch.name is not None:
        updates["name"] = patch.name
    if patch.notes is not None:
        updates["notes"] = patch.notes
    if patch.one_time is not None:
        updates["one_time"] = bool(patch.one_time)
    if patch.comparator is not None:
        updates["comparator"] = new_comparator

    # Target logic
    if new_target_type == "absolute":
        # Require absolute_target present when switching to absolute
        if patch.absolute_target is None:
            if existing["target_type"] != "absolute":
                raise HTTPException(status_code=400, detail="absolute_target is required when target_type='absolute'")
        else:
            updates["absolute_target"] = float(patch.absolute_target)
        updates["target_type"] = "absolute"
        updates["percent"] = None
    else:
        # target_type percent path
        pct = patch.percent if patch.percent is not None else existing["percent"]
        if pct is None:
            raise HTTPException(status_code=400, detail="percent is required when target_type='percent'")
        baseline = existing["baseline_price"]
        if baseline is None:
            # Without baseline, require explicit absolute_target
            if patch.absolute_target is None:
                raise HTTPException(status_code=409, detail="Cannot compute absolute_target without baseline_price. Provide absolute_target or re-anchor later.")
            abs_target = float(patch.absolute_target)
        else:
            abs_target = _compute_absolute_from_percent(float(baseline), float(pct), new_comparator)
        updates["target_type"] = "percent"
        updates["percent"] = float(pct)
        updates["absolute_target"] = float(abs_target)

    if not updates:
        # Nothing to update
        return dict(existing)

    # Build dynamic SQL
    set_parts: List[str] = []
    params: Dict[str, Any] = {"id": id}
    for k, v in updates.items():
        set_parts.append(f"{k} = :{k}")
        params[k] = v
    set_parts.append("updated_at = NOW()")
    sql = f"UPDATE public.alerts SET {', '.join(set_parts)} WHERE id = :id RETURNING *"
    row = await async_db.fetch_one(sql, params)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    try:
        await _engine_refresh(request)
    except Exception:
        pass
    return dict(row)

# DELETE /alerts/{id}
@router.delete("/{id}")
async def delete_alert(id: str, request: Request, hard: bool = Query(default=False)):
    if hard:
        row = await async_db.fetch_one("DELETE FROM public.alerts WHERE id = :id RETURNING *", {"id": id})
        if not row:
            raise HTTPException(status_code=404, detail="Alert not found")
        try:
            # Adjust engine active set/subscriptions immediately
            await _engine_refresh(request)
        except Exception:
            pass
        return dict(row)
    else:
        row = await async_db.fetch_one(
            "UPDATE public.alerts SET status = 'canceled', updated_at = NOW() WHERE id = :id RETURNING *",
            {"id": id}
        )
        if not row:
            raise HTTPException(status_code=404, detail="Alert not found")
        try:
            await _engine_refresh(request)
        except Exception:
            pass
        return dict(row)

# POST /alerts/{id}/duplicate
@router.post("/{id}/duplicate")
async def duplicate_alert(id: str):
    src = await async_db.fetch_one("SELECT * FROM public.alerts WHERE id = :id", {"id": id})
    if not src:
        raise HTTPException(status_code=404, detail="Alert not found")
    # Do not re-anchor; copy baseline as-is for now
    # TODO: In future, re-anchor baseline_price using LTP helper or WS cache
    sql = """
    INSERT INTO public.alerts (
        instrument_token, comparator, target_type, absolute_target, percent, baseline_price,
        one_time, name, notes, status
    ) VALUES (
        :instrument_token, :comparator, :target_type, :absolute_target, :percent, :baseline_price,
        :one_time, :name, :notes, 'active'
    )
    RETURNING *;
    """
    values = {
        "instrument_token": int(src["instrument_token"]),
        "comparator": src["comparator"],
        "target_type": src["target_type"],
        "absolute_target": float(src["absolute_target"]) if src["absolute_target"] is not None else None,
        "percent": float(src["percent"]) if src["percent"] is not None else None,
        "baseline_price": float(src["baseline_price"]) if src["baseline_price"] is not None else None,
        "one_time": bool(src["one_time"]) if src["one_time"] is not None else True,
        "name": src["name"],
        "notes": src["notes"],
    }
    row = await async_db.fetch_one(sql, values)
    return dict(row) if row else {}

# ───────── State action helpers ─────────

async def _insert_alert_event(request: Request, alert_row: Any, event_type: str) -> None:
    """
    Best-effort event insert; failures are logged and ignored.
    """
    try:
        price = _get_ws_baseline(request, int(alert_row["instrument_token"]))
    except Exception:
        price = None

    sql = """
    INSERT INTO public.alert_events (
        alert_id, instrument_token, event_type, price_at_event, direction, reason, meta
    ) VALUES (
        :alert_id, :instrument_token, :event_type, :price_at_event, NULL, NULL, NULL
    )
    """
    try:
        await async_db.execute(sql, {
            "alert_id": alert_row["id"],
            "instrument_token": int(alert_row["instrument_token"]),
            "event_type": event_type,
            "price_at_event": float(price) if price is not None else None,
        })
    except Exception as e:
        logging.error(f"[ALERTS] event insert failed for id={alert_row.get('id')}: {e}", exc_info=True)

# POST /alerts/{id}/pause
@router.post("/{id}/pause")
async def pause_alert(id: str, request: Request):
    existing = await async_db.fetch_one("SELECT * FROM public.alerts WHERE id = :id", {"id": id})
    if not existing:
        raise HTTPException(status_code=404, detail="Alert not found")

    status = existing["status"]
    if status == "paused":
        # Idempotent: already paused
        return dict(existing)
    if status != "active":
        raise HTTPException(status_code=409, detail=f"Invalid transition: {status} -> paused")

    # State update
    row = await async_db.fetch_one(
        "UPDATE public.alerts SET status = 'paused', updated_at = NOW() WHERE id = :id RETURNING *",
        {"id": id}
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    # Best-effort event logging
    await _insert_alert_event(request, row, "paused")
    logging.info("[ALERTS] pause: id=%s token=%s active->paused", id, row["instrument_token"])
    try:
        await _engine_refresh(request)
    except Exception:
        pass
    return dict(row)

# POST /alerts/{id}/resume
@router.post("/{id}/resume")
async def resume_alert(id: str, request: Request):
    existing = await async_db.fetch_one("SELECT * FROM public.alerts WHERE id = :id", {"id": id})
    if not existing:
        raise HTTPException(status_code=404, detail="Alert not found")

    status = existing["status"]
    if status == "active":
        # Idempotent: already active
        return dict(existing)
    if status != "paused":
        raise HTTPException(status_code=409, detail=f"Invalid transition: {status} -> active")

    row = await async_db.fetch_one(
        "UPDATE public.alerts SET status = 'active', updated_at = NOW() WHERE id = :id RETURNING *",
        {"id": id}
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    await _insert_alert_event(request, row, "resumed")
    logging.info("[ALERTS] resume: id=%s token=%s paused->active", id, row["instrument_token"])
    try:
        await _engine_refresh(request)
    except Exception:
        pass
    return dict(row)

# POST /alerts/{id}/cancel
@router.post("/{id}/cancel")
async def cancel_alert(id: str, request: Request):
    existing = await async_db.fetch_one("SELECT * FROM public.alerts WHERE id = :id", {"id": id})
    if not existing:
        raise HTTPException(status_code=404, detail="Alert not found")

    status = existing["status"]
    if status == "canceled":
        # Idempotent
        return dict(existing)
    if status not in {"active", "paused"}:
        raise HTTPException(status_code=409, detail=f"Invalid transition: {status} -> canceled")

    row = await async_db.fetch_one(
        "UPDATE public.alerts SET status = 'canceled', updated_at = NOW() WHERE id = :id RETURNING *",
        {"id": id}
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    await _insert_alert_event(request, row, "canceled")
    logging.info("[ALERTS] cancel: id=%s token=%s %s->canceled", id, row["instrument_token"], status)
    try:
        await _engine_refresh(request)
    except Exception:
        pass
    return dict(row)


# GET /alerts/events (SSE)
@router.get("/events")
async def sse_alerts_events(request: Request):
    """
    Server-Sent Events endpoint for real-time alert notifications.
    Subscribes to the 'alerts.events' Redis channel and streams messages.
    """

    async def event_stream():
        logging.info("[ALERTS-SSE] Client connected to alerts event stream.")
        try:
            async for message in pubsub_iter("alerts.events"):
                if await request.is_disconnected():
                    logging.info("[ALERTS-SSE] Client disconnected.")
                    break

                if message.get("event") == "heartbeat":
                    # Send a comment to keep the connection alive
                    yield ": heartbeat\n\n"
                else:
                    yield f"data: {json.dumps(message)}\n\n"
        except asyncio.CancelledError:
            logging.info("[ALERTS-SSE] Event stream cancelled.")
        finally:
            logging.info("[ALERTS-SSE] Closing alerts event stream.")

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# POST /alerts/{id}/reactivate
@router.post("/{id}/reactivate")
async def reactivate_alert(id: str, request: Request):
    existing = await async_db.fetch_one("SELECT * FROM public.alerts WHERE id = :id", {"id": id})
    if not existing:
        raise HTTPException(status_code=404, detail="Alert not found")

    status = existing["status"]
    if status == "active":
        # Idempotent
        return dict(existing)
    if status not in {"triggered", "canceled", "paused"}:
        raise HTTPException(status_code=409, detail=f"Invalid transition: {status} -> active")

    row = await async_db.fetch_one(
        "UPDATE public.alerts SET status = 'active', triggered_at = NULL, updated_at = NOW() WHERE id = :id RETURNING *",
        {"id": id}
    )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    await _insert_alert_event(request, row, "reactivated")
    logging.info("[ALERTS] reactivate: id=%s token=%s %s->active", id, row["instrument_token"], status)
    try:
        await _engine_refresh(request)
    except Exception:
        pass
    return dict(row)