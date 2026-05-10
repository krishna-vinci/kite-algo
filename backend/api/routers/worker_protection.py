from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from backend.api.services.protection import validate_backend_protection_payload
from backend.api.services.safety import build_safety_fingerprint, build_signed_safety_token, option_run_status_blocks_trading, verify_signed_safety_token
from backend.broker_api.core.redis_events import get_redis, publish_event
from backend.broker_api.timeline.worker_timeline import worker_timeline_store
from backend.app.database import SessionLocal
from backend.api.schemas.worker import WorkerDecisionEventRequest, WorkerProtectionPatchRequest, WorkerRiskPatchRequest, WorkerRunPnlLeg, WorkerRunPnlSnapshot, WorkerRunPnlTotals, WorkerFundsSegment, WorkerFundsSnapshot, WorkerExitRequest
from backend.api.routers.worker_shared import *

router = APIRouter(prefix='/algo-workers', tags=['Algo Workers'])

def _load_worker_timeline_events(
    *,
    strategy_run_id: str,
    after_cursor: int,
    limit: int,
    event_kind: Optional[str] = None,
    event_source: Optional[str] = None,
    event_type: Optional[str] = None,
    related_resource_type: Optional[str] = None,
    related_resource_id: Optional[str] = None,
    basket_execution_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        return worker_timeline_store.list_events(
            db=db,
            strategy_run_id=strategy_run_id,
            after_cursor=after_cursor,
            limit=limit,
            event_kind=event_kind,
            event_source=event_source,
            event_type=event_type,
            related_resource_type=related_resource_type,
            related_resource_id=related_resource_id,
            basket_execution_id=basket_execution_id,
        )
    finally:
        db.close()

def _normalize_execution_event_compat_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cursor": _to_int(event.get("cursor")),
        "strategy_run_id": event.get("strategy_run_id"),
        "account_id": event.get("account_id"),
        "basket_execution_id": event.get("basket_execution_id"),
        "event_type": event.get("event_type"),
        "payload": dict(event.get("payload") or {}),
        "created_at": event.get("created_at"),
    }

async def _websocket_is_disconnected(websocket: WebSocket) -> bool:
    state = getattr(websocket, "client_state", None)
    if state is not None and str(getattr(state, "name", state)).upper() == "DISCONNECTED":
        return True
    return False

async def _worker_run_pnl_stream_ws(websocket: WebSocket, run: Dict[str, Any], *, interval_seconds: float) -> AsyncGenerator[tuple[str, Dict[str, Any]], None]:
    strategy_run_id = str(run["strategy_run_id"])
    current_run = dict(run)
    last_signature: Optional[str] = None
    heartbeat_counter = 0
    safe_interval = min(5.0, max(0.25, float(interval_seconds or 1.0)))
    while True:
        if await _websocket_is_disconnected(websocket):
            break
        refreshed_run = await _repo(websocket).get_run(strategy_run_id)
        if refreshed_run is None:
            yield "end", {"detail": "Strategy run not found"}
            break
        current_run = refreshed_run
        try:
            snapshot = await _build_worker_run_pnl_snapshot(websocket, current_run)
        except Exception as exc:
            yield "error", {"detail": str(exc)}
            await asyncio.sleep(safe_interval)
            continue
        signature = _snapshot_signature(snapshot)
        if signature != last_signature:
            yield "snapshot", snapshot
            last_signature = signature
            heartbeat_counter = 0
        else:
            heartbeat_counter += 1
            if heartbeat_counter >= max(1, int(15 / safe_interval)):
                yield "heartbeat", {"strategy_run_id": strategy_run_id}
                heartbeat_counter = 0
        await asyncio.sleep(safe_interval)

def _worker_pnl_side(net_quantity: int) -> str:
    if net_quantity > 0:
        return "LONG"
    if net_quantity < 0:
        return "SHORT"
    return "FLAT"

def _empty_worker_pnl_snapshot(run: Dict[str, Any], *, is_realtime: bool, is_stale: bool = False, updated_at: Optional[str] = None) -> Dict[str, Any]:
    return WorkerRunPnlSnapshot(
        strategy_run_id=str(run["strategy_run_id"]),
        execution_mode=str(run.get("execution_mode") or "dry_run"),
        status=str(run.get("status") or "open"),
        currency="INR",
        totals=WorkerRunPnlTotals(),
        legs=[],
        position_count=0,
        is_realtime=is_realtime,
        is_stale=is_stale,
        updated_at=updated_at or _run_updated_at(run) or "1970-01-01T00:00:00+00:00",
    ).model_dump(mode="json")

def _snapshot_signature(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=_json_default, separators=(",", ":"))

def _numeric_dict_total(payload: Any, *, exclude: Optional[set[str]] = None) -> float:
    if not isinstance(payload, dict):
        return 0.0
    excluded = exclude or set()
    return sum(_to_float(value) for key, value in payload.items() if key not in excluded and isinstance(value, (int, float, str)))

def _worker_margin_segment(payload: Any) -> WorkerFundsSegment:
    segment = dict(payload or {}) if isinstance(payload, dict) else {}
    available = dict(segment.get("available") or {}) if isinstance(segment.get("available"), dict) else {}
    utilised = dict(segment.get("utilised") or {}) if isinstance(segment.get("utilised"), dict) else {}
    available_cash = _to_float(available.get("cash"), default=_to_float(segment.get("net")))
    return WorkerFundsSegment(
        net=_to_float(segment.get("net")),
        available_cash=available_cash,
        opening_balance=_to_float(available.get("opening_balance")),
        live_balance=_to_float(available.get("live_balance")) if "live_balance" in available else None,
        collateral=_to_float(available.get("collateral")) if "collateral" in available else None,
        utilised=_numeric_dict_total(utilised, exclude={"m2m_realised", "m2m_unrealised"}),
        m2m_realised=_to_float(utilised.get("m2m_realised")),
        m2m_unrealised=_to_float(utilised.get("m2m_unrealised")),
    )

async def _paper_worker_funds_snapshot(request: Request, account_scope: str, *, mode: str) -> Dict[str, Any]:
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    account = await paper_runtime_service.get_account_summary(account_scope)
    available_funds = _to_float(account.get("available_funds"))
    blocked_funds = _to_float(account.get("blocked_funds"))
    realized_pnl = _to_float(account.get("realized_pnl"))
    segment = WorkerFundsSegment(
        net=available_funds + blocked_funds,
        available_cash=available_funds,
        opening_balance=_to_float(account.get("starting_balance")),
        utilised=blocked_funds,
        m2m_realised=realized_pnl,
        m2m_unrealised=0.0,
    )
    return WorkerFundsSnapshot(
        account_scope=account_scope,
        mode=mode,
        currency=str(account.get("currency") or "INR"),
        source="paper_runtime",
        segments={"equity": segment},
        allocation={"usable_equity_cash": available_funds, "max_new_position_value": available_funds},
        stale=False,
        updated_at=str(account.get("updated_at") or _utcnow().isoformat()),
    ).model_dump(mode="json")

async def _live_worker_funds_snapshot(account_scope: str, *, mode: str) -> Dict[str, Any]:
    kite = _load_live_kite_for_account(account_scope)
    margins = await asyncio.to_thread(kite.margins)
    segments = {
        name: _worker_margin_segment(payload)
        for name, payload in dict(margins or {}).items()
        if name in {"equity", "commodity"}
    }
    equity = segments.get("equity") or WorkerFundsSegment()
    return WorkerFundsSnapshot(
        account_scope=account_scope,
        mode=mode,
        currency="INR",
        source="broker",
        segments=segments,
        allocation={"usable_equity_cash": equity.available_cash, "max_new_position_value": equity.available_cash},
        stale=False,
        updated_at=_utcnow().isoformat(),
    ).model_dump(mode="json")

async def _build_worker_funds_snapshot(request: Request, *, account_scope: str, mode: str) -> Dict[str, Any]:
    normalized_mode = str(mode or "paper").lower()
    try:
        parsed_scope = parse_account_scope(account_scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if normalized_mode == "paper":
        return await _paper_worker_funds_snapshot(request, account_scope, mode=normalized_mode)
    if normalized_mode == "dry_run" and parsed_scope.mode == "paper":
        return await _paper_worker_funds_snapshot(request, account_scope, mode=normalized_mode)
    if normalized_mode in {"live", "dry_run"}:
        return await _live_worker_funds_snapshot(account_scope, mode=normalized_mode)
    raise HTTPException(status_code=400, detail=f"Unsupported execution mode '{normalized_mode}'")

def _run_allocation_cap(run: Dict[str, Any]) -> Optional[float]:
    metadata = dict(run.get("metadata") or {})
    runtime_state = dict(run.get("runtime_state") or {})
    allocation_state = dict(runtime_state.get("allocation") or {}) if isinstance(runtime_state.get("allocation"), dict) else {}
    for value in (
        metadata.get("allocation_cap"),
        metadata.get("allocation_cap_inr"),
        allocation_state.get("cap"),
        runtime_state.get("allocation_cap"),
    ):
        cap = _to_float(value, default=-1.0)
        if cap >= 0:
            return cap
    return None

def _run_usage_from_pnl(pnl: Dict[str, Any]) -> Dict[str, float]:
    gross_exposure = 0.0
    net_exposure = 0.0
    for leg in pnl.get("legs") or []:
        quantity = _to_int(leg.get("net_quantity"))
        mark = _to_float(leg.get("last_price"), default=0.0) or _to_float(leg.get("average_price"), default=0.0)
        gross_exposure += abs(quantity) * mark
        net_exposure += quantity * mark
    totals = dict(pnl.get("totals") or {})
    return {
        "gross_exposure": gross_exposure,
        "net_exposure": net_exposure,
        "realized_pnl": _to_float(totals.get("realized_pnl")),
        "unrealized_pnl": _to_float(totals.get("unrealized_pnl")),
        "net_pnl": _to_float(totals.get("net_pnl")),
    }

async def _build_worker_run_funds_snapshot(request: Request, run: Dict[str, Any]) -> Dict[str, Any]:
    account_funds = await _build_worker_funds_snapshot(request, account_scope=str(run["account_scope"]), mode=str(run.get("execution_mode") or "paper"))
    pnl = await _build_worker_run_pnl_snapshot(request, run)
    usage = _run_usage_from_pnl(pnl)
    cap = _run_allocation_cap(run)
    used = usage["gross_exposure"]
    allocation = {
        "cap": cap,
        "used": used,
        "remaining": max(0.0, cap - used) if cap is not None else None,
        "basis": "gross_exposure",
    }
    return {
        **account_funds,
        "strategy_run_id": str(run["strategy_run_id"]),
        "strategy": {
            "strategy_run_id": str(run["strategy_run_id"]),
            "status": str(run.get("status") or "open"),
            **usage,
            "estimated_margin_used": None,
            "allocation": allocation,
            "pnl": pnl.get("totals") or {},
            "position_count": _to_int(pnl.get("position_count")),
            "is_stale": bool(pnl.get("is_stale")),
        },
        "allocation": {**dict(account_funds.get("allocation") or {}), "run": allocation},
    }

def _run_updated_at(run: Dict[str, Any]) -> Optional[str]:
    runtime_state = dict(run.get("runtime_state") or {})
    for key in ("updated_at", "created_at"):
        value = run.get(key)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value.strip():
            return value
    for key in ("live_exit_finalized_at", "updated_at"):
        value = runtime_state.get(key)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value.strip():
            return value
    return None

def _accumulate_leg_fact(state: Dict[str, Any], *, side: str, quantity: int, price: float, charges: float) -> None:
    net_quantity = _to_int(state.get("net_quantity"))
    average_price = _to_float(state.get("average_price"))
    signed_quantity = quantity if side == "BUY" else -quantity

    state["charges"] = _to_float(state.get("charges")) + charges

    if net_quantity == 0 or (net_quantity > 0 and signed_quantity > 0) or (net_quantity < 0 and signed_quantity < 0):
        existing_abs = abs(net_quantity)
        incoming_abs = abs(signed_quantity)
        combined = existing_abs + incoming_abs
        state["average_price"] = price if combined == 0 else ((average_price * existing_abs) + (price * incoming_abs)) / combined
        state["net_quantity"] = net_quantity + signed_quantity
        return

    closing_quantity = min(abs(net_quantity), abs(signed_quantity))
    realized_pnl = _to_float(state.get("realized_pnl"))
    if net_quantity > 0 and signed_quantity < 0:
        realized_pnl += (price - average_price) * closing_quantity
    elif net_quantity < 0 and signed_quantity > 0:
        realized_pnl += (average_price - price) * closing_quantity
    state["realized_pnl"] = realized_pnl

    remaining_existing = abs(net_quantity) - closing_quantity
    remaining_incoming = abs(signed_quantity) - closing_quantity
    if remaining_existing > 0:
        state["net_quantity"] = remaining_existing if net_quantity > 0 else -remaining_existing
        state["average_price"] = average_price
        return
    if remaining_incoming > 0:
        state["net_quantity"] = remaining_incoming if signed_quantity > 0 else -remaining_incoming
        state["average_price"] = price
        return
    state["net_quantity"] = 0
    state["average_price"] = 0.0

def _build_live_worker_leg_states(facts: List[Any]) -> Tuple[Dict[Tuple[int, str], Dict[str, Any]], float]:
    legs: Dict[Tuple[int, str], Dict[str, Any]] = {}
    total_charges = 0.0
    ordered_facts = sorted(facts, key=lambda item: (getattr(item, "fill_timestamp", _utcnow()), getattr(item, "id", 0) or 0))
    for fact in ordered_facts:
        payload = dict(getattr(fact, "payload", {}) or {})
        broker_fill = dict(payload.get("broker_fill") or {})
        instrument_token = _to_int(broker_fill.get("instrument_token"))
        product = str(broker_fill.get("product") or "")
        if not instrument_token or not product:
            continue
        key = (instrument_token, product)
        state = legs.setdefault(
            key,
            {
                "instrument_token": instrument_token,
                "exchange": str(broker_fill.get("exchange") or "") or None,
                "tradingsymbol": str(broker_fill.get("tradingsymbol") or "") or None,
                "product": product,
                "net_quantity": 0,
                "average_price": 0.0,
                "realized_pnl": 0.0,
                "charges": 0.0,
                "last_fill_at": getattr(fact, "fill_timestamp", None),
            },
        )
        side = str(getattr(fact, "side", "") or "").upper()
        quantity = _to_int(getattr(fact, "quantity", 0))
        price = _to_float(getattr(fact, "price", 0.0))
        fact_charges = _to_float(getattr(fact, "fees_amount", 0.0)) + _to_float(getattr(fact, "taxes_amount", 0.0)) + _to_float(getattr(fact, "slippage_amount", 0.0))
        total_charges += fact_charges
        _accumulate_leg_fact(state, side=side, quantity=quantity, price=price, charges=fact_charges)
        state["last_fill_at"] = getattr(fact, "fill_timestamp", None) or state.get("last_fill_at")
    return legs, total_charges

async def _paper_worker_run_pnl_snapshot(request: Request, run: Dict[str, Any]) -> Dict[str, Any]:
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    summary = await paper_runtime_service.get_strategy_run_pnl(str(run["account_scope"]), str(run["strategy_run_id"]))
    if summary is None:
        return _empty_worker_pnl_snapshot(run, is_realtime=True)

    strategy = dict(summary.get("strategy") or {})
    legs = [
        WorkerRunPnlLeg(
            instrument_token=position.get("instrument_token"),
            exchange=position.get("exchange"),
            tradingsymbol=position.get("tradingsymbol"),
            product=position.get("product"),
            net_quantity=_to_int(position.get("net_quantity")),
            side=str(position.get("side") or _worker_pnl_side(_to_int(position.get("net_quantity")))),
            average_price=_to_float(position.get("average_price")),
            last_price=_to_float(position.get("last_price")),
            realized_pnl=_to_float(position.get("realized_pnl")),
            unrealized_pnl=_to_float(position.get("unrealized_pnl")),
            gross_pnl=_to_float(position.get("gross_pnl"))
            if "gross_pnl" in position
            else (_to_float(position.get("realized_pnl")) + _to_float(position.get("unrealized_pnl"))),
            charges=_to_float(position.get("charges")),
            net_pnl=_to_float(position.get("net_pnl"))
            if "net_pnl" in position
            else (
                (_to_float(position.get("gross_pnl")) if "gross_pnl" in position else (_to_float(position.get("realized_pnl")) + _to_float(position.get("unrealized_pnl"))))
                - _to_float(position.get("charges"))
            ),
            is_stale=bool(position.get("is_stale")),
        )
        for position in strategy.get("positions", [])
        if _to_int(position.get("net_quantity")) != 0
    ]
    realized = _to_float(strategy.get("realized_pnl"))
    unrealized = _to_float(strategy.get("unrealized_pnl"))
    gross = _to_float(strategy.get("gross_pnl")) if "gross_pnl" in strategy else (realized + unrealized)
    charges = _to_float(strategy.get("charges"))
    if charges == 0.0:
        charges = sum(_to_float(getattr(leg, "charges", 0.0)) for leg in legs)
    net = _to_float(strategy.get("net_pnl")) if "net_pnl" in strategy else (gross - charges)
    updated_at = str(strategy.get("last_updated_at") or strategy.get("last_event_at") or _utcnow().isoformat())
    return WorkerRunPnlSnapshot(
        strategy_run_id=str(run["strategy_run_id"]),
        execution_mode="paper",
        status=str(strategy.get("status") or run.get("status") or "open"),
        currency=str(summary.get("currency") or "INR"),
        totals=WorkerRunPnlTotals(realized_pnl=realized, unrealized_pnl=unrealized, gross_pnl=gross, charges=charges, net_pnl=net),
        legs=legs,
        position_count=len(legs),
        is_realtime=True,
        is_stale=bool(strategy.get("is_stale")),
        updated_at=updated_at,
    ).model_dump(mode="json")

async def _live_worker_run_pnl_snapshot(request: Request, run: Dict[str, Any]) -> Dict[str, Any]:
    strategy_run_id = str(run["strategy_run_id"])
    account_id = str(run["account_scope"])

    realtime_positions = getattr(request.app.state, "algo_worker_realtime_positions_service", None)
    if realtime_positions is None:
        from backend.broker_api.orders.order_runtime import realtime_positions_service as realtime_positions

    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-pnl-{uuid.uuid4()}"
    positions = await realtime_positions.get_positions(account_id, corr_id)
    positions_by_leg: Dict[Tuple[int, str], Any] = {}
    for position in positions.values():
        positions_by_leg[(int(position.instrument_token), str(position.product))] = position

    from backend.journaling.repository import JournalRepository

    journal_repository = getattr(request.app.state, "algo_worker_journal_repository", None) or JournalRepository()
    link = await asyncio.to_thread(journal_repository.find_source_link, source_type="live_order", source_key=strategy_run_id)
    live_facts: List[Any] = []
    if link is not None:
        facts = await asyncio.to_thread(journal_repository.list_execution_facts, str(link.run_id))
        live_facts = [fact for fact in facts if str(getattr(fact, "source_type", "")) == "live_fill"]

    if live_facts:
        legs_by_key, total_charges = _build_live_worker_leg_states(live_facts)
    else:
        attributed_legs = await _repo(request).list_live_strategy_open_legs(strategy_run_id=strategy_run_id, account_id=account_id)
        legs_by_key = {}
        total_charges = 0.0
        for leg in attributed_legs:
            instrument_token = _to_int(leg.get("instrument_token"))
            product = str(leg.get("product") or "")
            if not instrument_token or not product:
                continue
            net_quantity = _to_int(leg.get("net_quantity"))
            position = positions_by_leg.get((instrument_token, product))
            average_price = _to_float(getattr(position, "average_price", 0.0)) if position is not None else 0.0
            legs_by_key[(instrument_token, product)] = {
                "instrument_token": instrument_token,
                "exchange": leg.get("exchange"),
                "tradingsymbol": leg.get("tradingsymbol"),
                "product": product,
                "net_quantity": net_quantity,
                "average_price": average_price,
                "realized_pnl": 0.0,
                "charges": 0.0,
                "last_fill_at": None,
            }

    rendered_legs: List[WorkerRunPnlLeg] = []
    unrealized_total = 0.0
    realized_total = 0.0
    updated_markers: List[str] = []
    stale = False

    for key, state in sorted(legs_by_key.items(), key=lambda item: ((item[1].get("exchange") or ""), (item[1].get("tradingsymbol") or ""), (item[1].get("product") or ""))):
        position = positions_by_leg.get(key)
        net_quantity = _to_int(state.get("net_quantity"))
        if position is not None:
            last_price = _to_float(getattr(position, "last_price", 0.0))
            broker_net_quantity = _to_int(getattr(position, "quantity", 0))
            last_reconciled_at = getattr(position, "last_reconciled_at", None)
            if last_reconciled_at:
                updated_markers.append(str(last_reconciled_at))
        else:
            last_price = 0.0
            broker_net_quantity = None
            last_reconciled_at = None

        realized = _to_float(state.get("realized_pnl"))
        charges = _to_float(state.get("charges"))
        average_price = _to_float(state.get("average_price"))
        unrealized = 0.0
        leg_stale = False
        if net_quantity != 0:
            if last_price > 0:
                unrealized = (last_price - average_price) * net_quantity
            else:
                leg_stale = True
            if broker_net_quantity is None:
                leg_stale = True
            elif broker_net_quantity != net_quantity:
                leg_stale = True

        stale = stale or leg_stale
        realized_total += realized
        unrealized_total += unrealized
        gross = realized + unrealized
        net = gross - charges
        rendered_legs.append(
            WorkerRunPnlLeg(
                instrument_token=state.get("instrument_token"),
                exchange=state.get("exchange"),
                tradingsymbol=state.get("tradingsymbol"),
                product=state.get("product"),
                net_quantity=net_quantity,
                side=_worker_pnl_side(net_quantity),
                average_price=average_price,
                last_price=last_price,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                gross_pnl=gross,
                charges=charges,
                net_pnl=net,
                broker_net_quantity=broker_net_quantity,
                is_stale=leg_stale,
                last_reconciled_at=str(last_reconciled_at) if last_reconciled_at else None,
            )
        )

    gross_total = realized_total + unrealized_total
    net_total = gross_total - total_charges
    if not updated_markers and live_facts:
        updated_markers = [getattr(live_facts[-1], "fill_timestamp", _utcnow()).isoformat()]
    updated_at = max(updated_markers) if updated_markers else (_run_updated_at(run) or _utcnow().isoformat())
    return WorkerRunPnlSnapshot(
        strategy_run_id=strategy_run_id,
        execution_mode="live",
        status=str(run.get("status") or "open"),
        currency="INR",
        totals=WorkerRunPnlTotals(
            realized_pnl=realized_total,
            unrealized_pnl=unrealized_total,
            gross_pnl=gross_total,
            charges=total_charges,
            net_pnl=net_total,
        ),
        legs=[leg for leg in rendered_legs if leg.net_quantity != 0],
        position_count=len([leg for leg in rendered_legs if leg.net_quantity != 0]),
        is_realtime=True,
        is_stale=stale,
        updated_at=updated_at,
    ).model_dump(mode="json")

async def _build_worker_run_pnl_snapshot(request: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(run.get("execution_mode") or "").lower()
    if mode == "dry_run":
        return _empty_worker_pnl_snapshot(run, is_realtime=False)
    if mode == "paper":
        return await _paper_worker_run_pnl_snapshot(request, run)
    if mode == "live":
        return await _live_worker_run_pnl_snapshot(request, run)
    raise HTTPException(status_code=400, detail=f"Unsupported execution mode '{mode}'")

async def _worker_run_pnl_stream(request: Request, run: Dict[str, Any], *, interval_seconds: float) -> AsyncGenerator[str, None]:
    strategy_run_id = str(run["strategy_run_id"])
    current_run = dict(run)
    last_signature: Optional[str] = None
    heartbeat_counter = 0
    safe_interval = min(5.0, max(0.25, float(interval_seconds or 1.0)))
    while True:
        if await request.is_disconnected():
            break
        refreshed_run = await _repo(request).get_run(strategy_run_id)
        if refreshed_run is None:
            yield "event: end\ndata: {\"detail\": \"Strategy run not found\"}\n\n"
            break
        current_run = refreshed_run
        try:
            snapshot = await _build_worker_run_pnl_snapshot(request, current_run)
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
            await asyncio.sleep(safe_interval)
            continue
        signature = _snapshot_signature(snapshot)
        if signature != last_signature:
            yield f"data: {json.dumps(snapshot, default=_json_default)}\n\n"
            last_signature = signature
            heartbeat_counter = 0
        else:
            heartbeat_counter += 1
            if heartbeat_counter >= max(1, int(15 / safe_interval)):
                yield ": heartbeat\n\n"
                heartbeat_counter = 0
        await asyncio.sleep(safe_interval)

async def _option_run_status_for_worker(request: Request, strategy_run_id: str) -> str | None:
    try:
        from backend.options.execution.store import get_option_run_store

        store = get_option_run_store()
        run = await asyncio.to_thread(store.get_run, strategy_run_id)
        return str(run.status or "")
    except KeyError:
        return None
    except Exception:
        return _OPTION_PROTECTION_STATE_UNAVAILABLE

async def _option_run_protection_snapshot_for_worker(
    request: Request,
    strategy_run_id: str,
    *,
    worker_run: Optional[Dict[str, Any]] = None,
) -> dict[str, Any]:
    snapshot, _events = await observe_worker_option_protection_timeline_state(
        request,
        strategy_run_id,
        worker_run=worker_run,
    )
    return snapshot

def _worker_safety_secret(request: Request) -> str:
    _ = request
    secret = os.getenv("WORKER_SAFETY_TOKEN_SECRET") or os.getenv("APP_JWT_SECRET")
    if secret:
        return str(secret)
    if os.getenv("PYTEST_CURRENT_TEST"):
        return "test-worker-safety-secret"
    raise HTTPException(status_code=503, detail="Worker safety token secret is not configured")

def _safety_blocking_reasons(
    *,
    run_status: str,
    generic_status: str,
    generic_exit_submitted: bool,
    option_status: str | None,
) -> list[str]:
    blocking_reasons: list[str] = []
    if run_status != "open":
        blocking_reasons.append("RUN_NOT_OPEN")
    if generic_status == "triggered":
        blocking_reasons.append("GENERIC_PROTECTION_TRIGGERED")
    if generic_exit_submitted:
        blocking_reasons.append("GENERIC_EXIT_IN_PROGRESS")
    if option_status == _OPTION_PROTECTION_STATE_UNAVAILABLE:
        blocking_reasons.append("OPTIONS_PROTECTION_STATE_UNAVAILABLE")
    elif option_status and option_run_status_blocks_trading(option_status):
        blocking_reasons.append("OPTIONS_RUN_NOT_ACTIVE")
    return blocking_reasons

def _compute_option_observation_fingerprint(snapshot: Dict[str, Any]) -> str:
    canonical = {
        "applicable": bool(snapshot.get("applicable")),
        "run_status": snapshot.get("run_status"),
        "triggered": bool(snapshot.get("triggered")),
        "blocking": bool(snapshot.get("blocking")),
        "blocking_reason": snapshot.get("blocking_reason"),
        "matched_rule": dict(snapshot.get("matched_rule") or {}) if isinstance(snapshot.get("matched_rule"), dict) else snapshot.get("matched_rule"),
        "metrics": dict(snapshot.get("metrics") or {}),
        "recommended_exit_orders_count": _to_int(snapshot.get("recommended_exit_orders_count"), default=0),
    }
    return hashlib.sha1(json.dumps(canonical, sort_keys=True, default=_json_default, separators=(",", ":")).encode("utf-8")).hexdigest()

def _build_option_observation_snapshot(run: Any) -> Dict[str, Any]:
    from backend.options.protection.runtime import evaluate_option_protection_state

    run_status = str(getattr(run, "status", "") or "")
    verdict = evaluate_option_protection_state(run=run)
    triggered = bool(verdict.get("triggered"))
    status_blocks = bool(run_status and option_run_status_blocks_trading(run_status))
    if triggered:
        blocking_reason = "OPTIONS_PROTECTION_TRIGGERED"
    elif status_blocks:
        blocking_reason = "OPTIONS_RUN_NOT_ACTIVE"
    else:
        blocking_reason = None
    recommended_exit_orders = list(verdict.get("recommended_exit_orders") or [])
    return {
        "applicable": True,
        "run_status": run_status,
        "evaluation_mode": "run_state",
        "triggered": triggered,
        "blocking": bool(triggered or status_blocks),
        "blocking_reason": blocking_reason,
        "matched_rule": verdict.get("matched_rule"),
        "metrics": dict(verdict.get("metrics") or {}),
        "recommended_exit_orders_count": len(recommended_exit_orders),
    }

def _observe_worker_option_protection_timeline_state_sync(
    *,
    strategy_run_id: str,
    worker_run: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    from backend.options.execution.store import get_option_run_store

    store = get_option_run_store()
    session_factory = getattr(store, "_session_factory", SessionLocal)

    session = session_factory()
    try:
        option_run = store.get_run_in_session(session, strategy_run_id)
    except KeyError:
        try:
            session.rollback()
        except Exception:
            pass
        session.close()
        return (
            {
                "applicable": False,
                "run_status": None,
                "evaluation_mode": "run_state",
                "triggered": False,
                "blocking": False,
                "blocking_reason": None,
                "matched_rule": None,
                "metrics": {},
                "recommended_exit_orders_count": 0,
            },
            [],
        )
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        session.close()
        unavailable = {
            "applicable": True,
            "run_status": None,
            "evaluation_mode": "run_state",
            "triggered": False,
            "blocking": True,
            "blocking_reason": "OPTIONS_PROTECTION_STATE_UNAVAILABLE",
            "matched_rule": None,
            "metrics": {},
            "recommended_exit_orders_count": 0,
        }
        return unavailable, []

    try:
        snapshot = _build_option_observation_snapshot(option_run)
    except Exception:
        snapshot = {
            "applicable": True,
            "run_status": None,
            "evaluation_mode": "run_state",
            "triggered": False,
            "blocking": True,
            "blocking_reason": "OPTIONS_PROTECTION_STATE_UNAVAILABLE",
            "matched_rule": None,
            "metrics": {},
            "recommended_exit_orders_count": 0,
        }

    fingerprint = _compute_option_observation_fingerprint(snapshot)

    timeline_events: List[Dict[str, Any]] = []
    try:
        account_id = str((worker_run or {}).get("account_scope") or "")
        if worker_run is not None:
            session.execute(
                text(
                    """
                    SELECT strategy_run_id
                    FROM public.algo_worker_runs
                    WHERE strategy_run_id = :strategy_run_id
                    FOR UPDATE
                    """
                ),
                {"strategy_run_id": strategy_run_id},
            ).fetchone()

        latest = worker_timeline_store.get_latest_event_for_source(
            db=session,
            strategy_run_id=strategy_run_id,
            event_kind="protection",
            event_source="options_protection",
        )
        latest_fingerprint = None
        if latest:
            latest_payload = dict(latest.get("payload") or {})
            latest_fingerprint = str(latest_payload.get("observation_fingerprint") or "") or None
        if latest_fingerprint != fingerprint and worker_run is not None:
            event_type = "protection.triggered" if snapshot.get("triggered") else "protection.blocking_changed"
            emitted = worker_timeline_store.append_event(
                db=session,
                strategy_run_id=strategy_run_id,
                account_id=account_id,
                basket_execution_id=None,
                event_kind="protection",
                event_source="options_protection",
                event_type=event_type,
                related_resource_type="strategy_run",
                related_resource_id=strategy_run_id,
                summary="Options protection observation state changed",
                payload={
                    "emission_mode": "observation_driven",
                    "observation_fingerprint": fingerprint,
                    "snapshot": snapshot,
                },
            )
            timeline_events.append(emitted)

        if timeline_events:
            session.commit()
        else:
            session.rollback()
    except Exception:
        session.rollback()
    finally:
        session.close()

    return snapshot, timeline_events

async def observe_worker_option_protection_timeline_state(
    request: Request,
    strategy_run_id: str,
    *,
    worker_run: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    snapshot, timeline_events = await asyncio.to_thread(
        _observe_worker_option_protection_timeline_state_sync,
        strategy_run_id=strategy_run_id,
        worker_run=worker_run,
    )
    for event in timeline_events:
        await publish_event(f"worker.execution.events:{strategy_run_id}", event)
    return snapshot, timeline_events

async def validate_worker_run_safety_token(
    request: Request,
    strategy_run_id: str,
    safety_token: str,
    *,
    run: dict[str, Any] | None = None,
) -> None:
    current_run = run if run is not None else await _repo(request).get_run(strategy_run_id)
    if current_run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")

    runtime_state = dict(current_run.get("runtime_state") or {})
    generic = dict(runtime_state.get("backend_protection_state") or {})
    option_snapshot = await _option_run_protection_snapshot_for_worker(request, strategy_run_id)
    option_status = (
        _OPTION_PROTECTION_STATE_UNAVAILABLE
        if option_snapshot.get("blocking_reason") == "OPTIONS_PROTECTION_STATE_UNAVAILABLE"
        else option_snapshot.get("run_status")
    )
    run_status = str(current_run.get("status") or "open")
    generic_status = str(generic.get("status") or "active")
    generic_exit_submitted = bool(generic.get("exit_submitted"))

    current_fingerprint = build_safety_fingerprint(
        run_status=run_status,
        generic_status=generic_status,
        generic_exit_submitted=generic_exit_submitted,
        option_run_status=option_status,
    )
    verified = verify_signed_safety_token(
        safety_token,
        strategy_run_id,
        _worker_safety_secret(request),
        now=_utcnow(),
    )
    if verified is None or verified.get("fingerprint") != current_fingerprint:
        blocking_reasons = _safety_blocking_reasons(
            run_status=run_status,
            generic_status=generic_status,
            generic_exit_submitted=generic_exit_submitted,
            option_status=option_status,
        )
        if option_snapshot.get("triggered"):
            blocking_reasons.append("OPTIONS_PROTECTION_TRIGGERED")
        blocking_reasons = list(dict.fromkeys(blocking_reasons))
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "SAFETY_TOKEN_EXPIRED",
                "blocking_reasons": blocking_reasons or ["SAFETY_STATE_UNKNOWN"],
                "strategy_run_id": strategy_run_id,
            },
        )

def _normalized_backend_protection_runtime_state(payload: Any, *, live: bool) -> Dict[str, Any]:
    return validate_backend_protection_payload(payload, live=live).to_runtime_state()

def _initial_backend_protection_state(protection: Dict[str, Any], *, generation: int = 1, reason: Optional[str] = None) -> Dict[str, Any]:
    return {
        "status": "active" if protection.get("enabled") else "disabled",
        "generation": generation,
        "version": int(protection.get("version") or 1),
        "update_reason": reason,
        "updated_at": _utcnow().isoformat(),
        "last_checked_at": None,
        "triggered_rule": None,
        "action": None,
        "exit_submitted": False,
        "errors": [],
    }

def _next_backend_protection_for_patch(protection: Dict[str, Any], previous_runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    previous_config = dict(previous_runtime_state.get("backend_protection") or {})
    previous_version = _to_int(previous_config.get("version"), default=0)
    next_protection = dict(protection)
    next_protection["version"] = max(_to_int(next_protection.get("version"), default=1), previous_version + 1)
    return next_protection

def _preserve_backend_trailing_state(next_state: Dict[str, Any], previous_state: Dict[str, Any]) -> Dict[str, Any]:
    preserved = dict(next_state)
    if "best_basket_pnl_pct" in previous_state:
        preserved["best_basket_pnl_pct"] = previous_state.get("best_basket_pnl_pct")
    previous_positions = previous_state.get("position_states")
    if isinstance(previous_positions, dict):
        preserved["position_states"] = dict(previous_positions)
    return preserved

def _protection_reset_timeline_event(
    *,
    strategy_run_id: str,
    previous_state: Dict[str, Any],
    next_state: Dict[str, Any],
    reason: Optional[str],
) -> Optional[Dict[str, Any]]:
    previous_status = str(previous_state.get("status") or "")
    if previous_status not in {"triggered", "error"}:
        return None
    if str(next_state.get("status") or "") != "active":
        return None
    return {
        "event_kind": "protection",
        "event_source": "backend_protection",
        "event_type": "protection.reset",
        "related_resource_type": "strategy_run",
        "related_resource_id": strategy_run_id,
        "summary": "Backend protection reset to active generation",
        "payload": {
            "emission_mode": "mutation_driven",
            "previous_status": previous_status,
            "status": str(next_state.get("status") or "active"),
            "previous_generation": _to_int(previous_state.get("generation"), default=0),
            "generation": _to_int(next_state.get("generation"), default=0),
            "reason": reason,
        },
    }

async def get_worker_run_safety_check(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    runtime_state = dict(run.get("runtime_state") or {})
    generic = dict(runtime_state.get("backend_protection_state") or {})
    option_snapshot = await _option_run_protection_snapshot_for_worker(
        request,
        strategy_run_id,
        worker_run=run,
    )
    option_status = (
        _OPTION_PROTECTION_STATE_UNAVAILABLE
        if option_snapshot.get("blocking_reason") == "OPTIONS_PROTECTION_STATE_UNAVAILABLE"
        else option_snapshot.get("run_status")
    )
    generic_status = str(generic.get("status") or "active")
    generic_exit_submitted = bool(generic.get("exit_submitted"))
    run_status = str(run.get("status") or "open")

    blocking_reasons = _safety_blocking_reasons(
        run_status=run_status,
        generic_status=generic_status,
        generic_exit_submitted=generic_exit_submitted,
        option_status=option_status,
    )
    if option_snapshot.get("triggered"):
        blocking_reasons.append("OPTIONS_PROTECTION_TRIGGERED")
    blocking_reasons = list(dict.fromkeys(blocking_reasons))
    can_trade = not blocking_reasons
    now = _utcnow()
    fingerprint = build_safety_fingerprint(
        run_status=run_status,
        generic_status=generic_status,
        generic_exit_submitted=generic_exit_submitted,
        option_run_status=option_status,
    )
    token_expires_at = (now + timedelta(seconds=10)).isoformat() if can_trade else None
    token_value = (
        build_signed_safety_token(
            strategy_run_id=strategy_run_id,
            fingerprint=fingerprint,
            secret=_worker_safety_secret(request),
            now=now,
        )
        if can_trade
        else None
    )

    return {
        "strategy_run_id": strategy_run_id,
        "can_trade": can_trade,
        "run_status": run_status,
        "execution_mode": str(run.get("execution_mode") or "paper"),
        "safety_token": token_value,
        "token_expires_at": token_expires_at,
        "blocking_reasons": blocking_reasons,
        "generic_protection": {
            "status": generic_status,
            "triggered_rule": generic.get("triggered_rule"),
            "exit_submitted": generic_exit_submitted,
            "heartbeat_age_sec": generic.get("heartbeat_age_sec"),
            "last_checked_at": generic.get("last_checked_at"),
        },
        "options_protection": option_snapshot,
        "evaluated_at": now.isoformat(),
    }

async def get_worker_run_pnl(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    return await _build_worker_run_pnl_snapshot(request, run)

async def stream_worker_run_pnl(request: Request, strategy_run_id: str, interval_seconds: float = Query(1.0, ge=0.25, le=5.0)):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    return StreamingResponse(
        _worker_run_pnl_stream(request, run, interval_seconds=interval_seconds),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

async def patch_worker_run_risk(request: Request, strategy_run_id: str, payload: WorkerRiskPatchRequest):
    token = await require_worker_token(request)
    _require_action(token, "risk:update")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    await require_active_worker_run_session(request, run)
    if run.get("status") in {"closed", "failed"}:
        raise HTTPException(status_code=409, detail="Closed strategy runs cannot be risk-edited")
    return await _repo(request).update_run_risk(strategy_run_id, payload.patch)

async def patch_worker_run_protection(request: Request, strategy_run_id: str, payload: WorkerProtectionPatchRequest):
    token = await require_worker_token(request)
    _require_action(token, "risk:update")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    await require_active_worker_run_session(request, run)
    if run.get("status") in {"closed", "failed"}:
        raise HTTPException(status_code=409, detail="Closed strategy runs cannot be protection-edited")

    runtime_state = dict(run.get("runtime_state") or {})
    previous_state = dict(runtime_state.get("backend_protection_state") or {})
    if previous_state.get("exit_submitted"):
        raise HTTPException(status_code=409, detail="Backend protection cannot be reset after a terminal protection exit")
    if previous_state.get("exit_claim_id"):
        raise HTTPException(status_code=409, detail="Backend protection exit is already in progress")
    previous_status = str(previous_state.get("status") or "")
    if previous_status not in {"", "active", "disabled", "triggered", "error"}:
        raise HTTPException(status_code=409, detail="Backend protection cannot be reset from current status")

    try:
        protection = _normalized_backend_protection_runtime_state(
            payload.backend_protection,
            live=str(run.get("execution_mode") or "").lower() == "live",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    next_generation = _to_int(previous_state.get("generation"), default=0) + 1
    protection = _next_backend_protection_for_patch(protection, runtime_state)
    runtime_state["backend_protection"] = protection
    next_state = _initial_backend_protection_state(
        protection,
        generation=next_generation,
        reason=payload.reason,
    )
    if not payload.reset_trailing:
        next_state = _preserve_backend_trailing_state(next_state, previous_state)
    next_state["reset_trailing"] = payload.reset_trailing
    reset_event = _protection_reset_timeline_event(
        strategy_run_id=strategy_run_id,
        previous_state=previous_state,
        next_state=next_state,
        reason=payload.reason,
    )
    previous_generation = _to_int(previous_state.get("generation"), default=0)
    if hasattr(_repo(request), "update_run_backend_protection_with_events"):
        result = await _repo(request).update_run_backend_protection_with_events(
            strategy_run_id,
            protection,
            next_state,
            expected_generation=previous_generation,
            expected_triggered_rule=previous_state.get("triggered_rule") or "",
            expected_exit_claim_id=previous_state.get("exit_claim_id") or "",
            timeline_events=[reset_event] if reset_event else [],
        )
        if result is None:
            raise HTTPException(status_code=409, detail="Backend protection changed concurrently; reload and retry")
        for event in list(result.get("timeline_events") or []):
            await publish_event(f"worker.execution.events:{strategy_run_id}", event)
        return result.get("run")

    updated = await _repo(request).update_run_backend_protection(
        strategy_run_id,
        protection,
        next_state,
        expected_generation=previous_generation,
        expected_triggered_rule=previous_state.get("triggered_rule") or "",
        expected_exit_claim_id=previous_state.get("exit_claim_id") or "",
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Backend protection changed concurrently; reload and retry")
    return updated

async def list_worker_execution_events(
    request: Request,
    strategy_run_id: str,
    after_cursor: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    basket_execution_id: Optional[str] = None,
    event_type: Optional[str] = None,
):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    after_cursor_value = _query_int_param(after_cursor, default=0)
    limit_value = _query_int_param(limit, default=200)

    events = await asyncio.to_thread(
        _load_worker_timeline_events,
        strategy_run_id=strategy_run_id,
        after_cursor=after_cursor_value,
        limit=limit_value,
        event_kind="execution",
        event_type=event_type,
        basket_execution_id=basket_execution_id,
    )
    events = [_normalize_execution_event_compat_payload(item) for item in events]
    last_cursor = max([after_cursor_value] + [int(item.get("cursor") or 0) for item in events])
    return {
        "strategy_run_id": strategy_run_id,
        "after_cursor": after_cursor_value,
        "last_cursor": last_cursor,
        "events": events,
    }

async def stream_worker_execution_events(
    request: Request,
    strategy_run_id: str,
    after_cursor: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    basket_execution_id: Optional[str] = None,
    event_type: Optional[str] = None,
):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    after_cursor_value = _query_int_param(after_cursor, default=0)
    limit_value = _query_int_param(limit, default=500)

    channel = f"worker.execution.events:{strategy_run_id}"

    async def _event_stream() -> AsyncGenerator[str, None]:
        redis = get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        last_sent = max(0, after_cursor_value)
        try:
            rows = await asyncio.to_thread(
                _load_worker_timeline_events,
                strategy_run_id=strategy_run_id,
                after_cursor=last_sent,
                limit=limit_value,
                event_kind="execution",
                event_type=event_type,
                basket_execution_id=basket_execution_id,
            )
            for row in rows:
                cursor = int(row.get("cursor") or 0)
                if cursor <= last_sent:
                    continue
                compat_row = _normalize_execution_event_compat_payload(row)
                yield f"data: {json.dumps(compat_row, default=_json_default)}\n\n"
                last_sent = cursor

            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if not message or message.get("type") != "message":
                    yield ": heartbeat\n\n"
                    continue
                data = message.get("data")
                if isinstance(data, str):
                    payload = json.loads(data)
                elif isinstance(data, dict):
                    payload = data
                else:
                    continue
                live_event_kind = str(payload.get("event_kind") or "execution")
                if live_event_kind != "execution":
                    continue
                cursor = int(payload.get("cursor") or 0)
                if cursor <= last_sent:
                    continue
                if basket_execution_id and str(payload.get("basket_execution_id") or "") != str(basket_execution_id):
                    continue
                if event_type and str(payload.get("event_type") or "") != str(event_type):
                    continue
                compat_payload = _normalize_execution_event_compat_payload(payload)
                yield f"data: {json.dumps(compat_payload, default=_json_default)}\n\n"
                last_sent = cursor
        finally:
            try:
                await pubsub.unsubscribe(channel)
            finally:
                await pubsub.aclose()

    return StreamingResponse(_event_stream(), media_type="text/event-stream")

async def list_worker_timeline(
    request: Request,
    strategy_run_id: str,
    after_cursor: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    event_kind: Optional[str] = None,
    event_source: Optional[str] = None,
    event_type: Optional[str] = None,
    related_resource_type: Optional[str] = None,
    related_resource_id: Optional[str] = None,
    basket_execution_id: Optional[str] = None,
):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    after_cursor_value = _query_int_param(after_cursor, default=0)
    limit_value = _query_int_param(limit, default=200)

    events = await asyncio.to_thread(
        _load_worker_timeline_events,
        strategy_run_id=strategy_run_id,
        after_cursor=after_cursor_value,
        limit=limit_value,
        event_kind=event_kind,
        event_source=event_source,
        event_type=event_type,
        related_resource_type=related_resource_type,
        related_resource_id=related_resource_id,
        basket_execution_id=basket_execution_id,
    )
    last_cursor = max([after_cursor_value] + [int(item.get("cursor") or 0) for item in events])
    return {
        "strategy_run_id": strategy_run_id,
        "after_cursor": after_cursor_value,
        "last_cursor": last_cursor,
        "events": events,
    }

async def stream_worker_timeline(
    request: Request,
    strategy_run_id: str,
    after_cursor: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    event_kind: Optional[str] = None,
    event_source: Optional[str] = None,
    event_type: Optional[str] = None,
    related_resource_type: Optional[str] = None,
    related_resource_id: Optional[str] = None,
    basket_execution_id: Optional[str] = None,
):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    after_cursor_value = _query_int_param(after_cursor, default=0)
    limit_value = _query_int_param(limit, default=500)

    channel = f"worker.execution.events:{strategy_run_id}"

    async def _event_stream() -> AsyncGenerator[str, None]:
        redis = get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        last_sent = max(0, after_cursor_value)
        try:
            rows = await asyncio.to_thread(
                _load_worker_timeline_events,
                strategy_run_id=strategy_run_id,
                after_cursor=last_sent,
                limit=limit_value,
                event_kind=event_kind,
                event_source=event_source,
                event_type=event_type,
                related_resource_type=related_resource_type,
                related_resource_id=related_resource_id,
                basket_execution_id=basket_execution_id,
            )
            for row in rows:
                cursor = int(row.get("cursor") or 0)
                if cursor <= last_sent:
                    continue
                yield f"data: {json.dumps(row, default=_json_default)}\n\n"
                last_sent = cursor

            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if not message or message.get("type") != "message":
                    yield ": heartbeat\n\n"
                    continue
                data = message.get("data")
                if isinstance(data, str):
                    payload = json.loads(data)
                elif isinstance(data, dict):
                    payload = data
                else:
                    continue
                cursor = int(payload.get("cursor") or 0)
                if cursor <= last_sent:
                    continue
                if event_kind and str(payload.get("event_kind") or "") != str(event_kind):
                    continue
                if event_source and str(payload.get("event_source") or "") != str(event_source):
                    continue
                if event_type and str(payload.get("event_type") or "") != str(event_type):
                    continue
                if related_resource_type and str(payload.get("related_resource_type") or "") != str(related_resource_type):
                    continue
                if related_resource_id and str(payload.get("related_resource_id") or "") != str(related_resource_id):
                    continue
                if basket_execution_id and str(payload.get("basket_execution_id") or "") != str(basket_execution_id):
                    continue
                yield f"data: {json.dumps(payload, default=_json_default)}\n\n"
                last_sent = cursor
        finally:
            try:
                await pubsub.unsubscribe(channel)
            finally:
                await pubsub.aclose()

    return StreamingResponse(_event_stream(), media_type="text/event-stream")

async def create_worker_decision_event(
    request: Request,
    strategy_run_id: str,
    payload: WorkerDecisionEventRequest,
):
    token = await require_worker_token(request)
    _require_action(token, "runs:log")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    await require_active_worker_run_session(request, run)

    account_id = str(run.get("account_scope") or "")

    def _persist() -> Dict[str, Any]:
        db = SessionLocal()
        try:
            if payload.related_resource_type and payload.related_resource_id:
                _validate_decision_related_ref(
                    db=db,
                    strategy_run_id=strategy_run_id,
                    account_id=account_id,
                    related_resource_type=str(payload.related_resource_type),
                    related_resource_id=str(payload.related_resource_id),
                )
            event = worker_timeline_store.append_event(
                db=db,
                strategy_run_id=strategy_run_id,
                account_id=account_id,
                basket_execution_id=payload.basket_execution_id,
                event_kind="decision",
                event_source="worker",
                event_type=f"decision.{payload.decision_type}",
                related_resource_type=payload.related_resource_type,
                related_resource_id=payload.related_resource_id,
                summary=payload.summary,
                payload={
                    "decision_type": payload.decision_type,
                    "action": payload.action,
                    "summary": payload.summary,
                    "metadata": dict(payload.metadata or {}),
                    "related_resource_type": payload.related_resource_type,
                    "related_resource_id": payload.related_resource_id,
                    "basket_execution_id": payload.basket_execution_id,
                },
            )
            db.commit()
            return event
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    event = await asyncio.to_thread(_persist)
    await publish_event(f"worker.execution.events:{strategy_run_id}", event)
    return event

async def worker_ticks_ws(websocket: WebSocket):
    token = await require_worker_ws_token(websocket)
    _require_action(token, "market:stream")
    await websocket.accept()
    symbols = _parse_csv_values(websocket.query_params.get("symbols"))
    instrument_tokens = _parse_csv_int_values(websocket.query_params.get("tokens"), field_name="tokens")
    mode = websocket.query_params.get("mode") or "quote"
    async for event, payload in _market_data_service(websocket).stream_ticks_ws(
        websocket,
        token,
        symbols=symbols,
        instrument_tokens=instrument_tokens,
        mode=mode,
    ):
        await websocket.send_json({"event": event, "data": payload})

async def worker_candles_ws(websocket: WebSocket):
    token = await require_worker_ws_token(websocket)
    _require_action(token, "market:stream")
    await websocket.accept()
    instrument_token_param = websocket.query_params.get("instrument_token")
    instrument_token = normalize_instrument_token(instrument_token_param) if instrument_token_param else None
    async for event, payload in _market_data_service(websocket).stream_candles_ws(
        websocket,
        symbol=websocket.query_params.get("symbol"),
        instrument_token=instrument_token,
        interval=websocket.query_params.get("interval") or "5minute",
    ):
        await websocket.send_json({"event": event, "data": payload})

async def worker_run_pnl_ws(websocket: WebSocket, strategy_run_id: str):
    token = await require_worker_ws_token(websocket)
    _require_action(token, "runs:read")
    run = await _repo(websocket).get_run(strategy_run_id)
    if run is None:
        await websocket.close(code=4404)
        return
    _assert_run_access(token, run)
    try:
        interval_seconds = float(websocket.query_params.get("interval_seconds") or "1.0")
    except (TypeError, ValueError):
        await websocket.close(code=4400)
        return
    if interval_seconds < 0.25 or interval_seconds > 5.0:
        await websocket.close(code=4400)
        return
    await websocket.accept()
    async for event, payload in _worker_run_pnl_stream_ws(websocket, run, interval_seconds=interval_seconds):
        await websocket.send_json({"event": event, "data": payload})


router.add_api_route("/worker/runs/{strategy_run_id}/safety-check", get_worker_run_safety_check, methods=["GET"])
router.add_api_route("/worker/runs/{strategy_run_id}/pnl", get_worker_run_pnl, methods=["GET"])
router.add_api_route("/worker/runs/{strategy_run_id}/pnl/stream", stream_worker_run_pnl, methods=["GET"])
router.add_api_route("/worker/runs/{strategy_run_id}/risk", patch_worker_run_risk, methods=["PATCH"])
router.add_api_route("/worker/runs/{strategy_run_id}/protection", patch_worker_run_protection, methods=["PATCH"])
router.add_api_route("/worker/runs/{strategy_run_id}/execution-events", list_worker_execution_events, methods=["GET"])
router.add_api_route("/worker/runs/{strategy_run_id}/execution-events/stream", stream_worker_execution_events, methods=["GET"])
router.add_api_route("/worker/runs/{strategy_run_id}/timeline", list_worker_timeline, methods=["GET"])
router.add_api_route("/worker/runs/{strategy_run_id}/timeline/stream", stream_worker_timeline, methods=["GET"])
router.add_api_route("/worker/runs/{strategy_run_id}/decision-events", create_worker_decision_event, methods=["POST"])

router.add_api_websocket_route("/worker/ws/market/ticks", worker_ticks_ws)
router.add_api_websocket_route("/worker/ws/market/candles", worker_candles_ws)
router.add_api_websocket_route("/worker/ws/runs/{strategy_run_id}/pnl", worker_run_pnl_ws)
