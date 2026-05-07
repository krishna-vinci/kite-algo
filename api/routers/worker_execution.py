from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import text
from database import SessionLocal
from broker_api.orders.basket_execution import basket_execution_store
from broker_api.orders.bracket_runtime import bracket_runtime_store
from api.schemas.worker import WorkerBasketPreviewRequest, WorkerBracketCreateRequest, WorkerExitRequest, WorkerIntentRequest, WorkerOrderActionRequest, WorkerOrderModifyRequest, WorkerOrderPreviewRequest
from api.routers.worker_shared import *
from api.routers.worker_protection import _build_worker_run_pnl_snapshot, validate_worker_run_safety_token
from algo_runtime.execution_attribution import build_execution_attribution, build_paper_execution_attribution

router = APIRouter(prefix='/algo-workers', tags=['Algo Workers'])

async def _submit_live_worker_intent(*, request: Request, token: WorkerToken, run: Dict[str, Any], payload: WorkerIntentRequest) -> Dict[str, Any]:
    from broker_api.orders import BasketOrderRequest, OrdersService, PlaceOrderRequest

    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-live-{uuid.uuid4()}"
    worker_session_id = f"worker:{token.token_id}:{run['strategy_run_id']}"
    attribution = _live_attribution_for_worker_intent(token=token, run=run, request=payload)

    if payload.intent_type == "place_order":
        order_payload = payload.payload.get("order") or payload.payload
        req = PlaceOrderRequest.model_validate(_inject_live_attribution(order_payload, attribution))
        result = await orders_service.place_order(
            kite,
            req,
            corr_id,
            idempotency_key=payload.idempotency_key,
            session_id=worker_session_id,
            response=Response(),
        )
        return {"mode": "live", "intent_type": payload.intent_type, "result": result.model_dump(mode="json")}

    if payload.intent_type == "place_basket":
        basket_payload = dict(payload.payload.get("basket") or payload.payload)
        orders = [_inject_live_attribution(order, attribution) for order in basket_payload.get("orders") or []]
        basket_payload["orders"] = orders
        req = BasketOrderRequest.model_validate(basket_payload)
        basket_execution_id = _live_basket_execution_id(
            strategy_run_id=str(run["strategy_run_id"]),
            idempotency_key=payload.idempotency_key,
        )
        db = SessionLocal()
        basket_snapshot: Optional[Dict[str, Any]] = None
        try:
            pending_result = {
                "mode": "live",
                "intent_type": payload.intent_type,
                "basket_execution_id": basket_execution_id,
                "basket_status": "submitting",
                "action_required": False,
                "action_reason": None,
                "result": {"status": "pending", "results": [], "errors": []},
            }
            begun = await _repo(request).begin_intent(
                token_id=token.token_id,
                strategy_run_id=str(run["strategy_run_id"]),
                request=payload,
                initial_result=pending_result,
                status="pending",
                db=db,
            )
            if bool(begun.get("claimed")):
                basket_snapshot = basket_execution_store.create_live_basket_execution(
                    db,
                    strategy_run_id=str(run["strategy_run_id"]),
                    account_id=str(run["account_scope"]),
                    all_or_none=bool(getattr(req, "all_or_none", basket_payload.get("all_or_none", False))),
                    orders=orders,
                    basket_execution_id=basket_execution_id,
                )
            db.commit()

            if not bool(begun.get("claimed")):
                return dict(begun.get("result") or pending_result)

            try:
                result = await orders_service.place_basket(
                    kite,
                    req,
                    corr_id,
                    session_id=worker_session_id,
                    idempotency_key=payload.idempotency_key,
                    response=Response(),
                    basket_execution_id=basket_execution_id,
                )
                read_db = SessionLocal()
                try:
                    latest = basket_execution_store.get_basket_for_run(
                        read_db,
                        strategy_run_id=str(run["strategy_run_id"]),
                        basket_execution_id=basket_execution_id,
                    )
                finally:
                    read_db.close()
                enriched_result = {
                    "mode": "live",
                    "intent_type": payload.intent_type,
                    "basket_execution_id": basket_execution_id,
                    "basket_status": str((latest or {}).get("status") or result.basket_status or "submitting"),
                    "action_required": bool((latest or {}).get("action_required") or result.action_required),
                    "action_reason": (latest or {}).get("action_reason") or result.action_reason,
                    "result": result.model_dump(mode="json"),
                }
                await _repo(request).finalize_intent_result(
                    strategy_run_id=str(run["strategy_run_id"]),
                    idempotency_key=payload.idempotency_key,
                    status=str(result.status or "accepted"),
                    result=enriched_result,
                )
                return enriched_result
            except Exception as exc:
                failed_result = {
                    "mode": "live",
                    "intent_type": payload.intent_type,
                    "basket_execution_id": basket_execution_id,
                    "basket_status": "failed",
                    "action_required": True,
                    "action_reason": "submit_failed",
                    "result": {"status": "failed", "results": [], "errors": [{"error": str(exc)}]},
                }
                await _repo(request).finalize_intent_result(
                    strategy_run_id=str(run["strategy_run_id"]),
                    idempotency_key=payload.idempotency_key,
                    status="failed",
                    result=failed_result,
                )
                raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    raise HTTPException(status_code=400, detail=f"Unsupported intent_type '{payload.intent_type}'")

def _live_attribution_for_worker_intent(*, token: WorkerToken, run: Dict[str, Any], request: WorkerIntentRequest) -> Dict[str, Any]:
    metadata = dict(run.get("metadata") or {})
    account_scope = str(run.get("account_scope") or token.account_scope or "")
    _validate_live_run_contract(account_scope=account_scope, metadata=metadata)
    return {
        "strategy_run_id": str(run["strategy_run_id"]),
        "strategy_family": str(metadata["strategy_family"]),
        "strategy_name": str(metadata["strategy_name"]),
        "execution_mode": "live",
        "account_ref": account_scope,
        "entry_surface": str(metadata.get("entry_surface") or "algo_worker"),
        "journal_run_id": metadata.get("journal_run_id") or None,
        "source": "algo_worker",
        "idempotency_key": request.idempotency_key,
        "metadata": {
            "token_id": token.token_id,
            "template_id": run.get("template_id"),
            "worker_run_metadata": metadata,
            "intent_metadata": request.metadata,
        },
    }

def _paper_attribution_for_worker_intent(*, token: WorkerToken, run: Dict[str, Any], request: WorkerIntentRequest) -> Dict[str, Any]:
    metadata = dict(run.get("metadata") or {})
    account_scope = str(run.get("account_scope") or token.account_scope or "")
    strategy_family = str(metadata.get("strategy_family") or "indicator_strategy").strip()
    if strategy_family not in VALID_WORKER_STRATEGY_FAMILIES:
        strategy_family = "indicator_strategy"
    strategy_name = str(metadata.get("strategy_name") or run.get("template_id") or run.get("strategy_run_id") or "paper-run").strip()
    return build_paper_execution_attribution(
        strategy_run_id=str(run["strategy_run_id"]),
        strategy_family=strategy_family,
        strategy_name=strategy_name,
        account_ref=account_scope,
        entry_surface=str(metadata.get("entry_surface") or "algo_worker"),
        source="algo_worker",
        idempotency_key=request.idempotency_key,
        metadata=request.metadata,
        extras={
            "token_id": token.token_id,
            "template_id": run.get("template_id"),
            "strategy_id": str(run["strategy_run_id"]),
            "option_strategy_id": str(run["strategy_run_id"]),
            "strategy_tag": metadata.get("strategy_tag") or run.get("template_id"),
            "algo_instance_id": metadata.get("algo_instance_id"),
            "journal_run_id": metadata.get("journal_run_id") or None,
            "journal_ref": metadata.get("journal_ref") or None,
            "worker_run_metadata": metadata,
            "intent_metadata": request.metadata,
        },
    )

def _inject_live_attribution(order_payload: Dict[str, Any], attribution: Dict[str, Any]) -> Dict[str, Any]:
    order = dict(order_payload)
    order["attribution"] = dict(attribution)
    return order

def _validate_live_exit_legs(legs: List[Dict[str, Any]]) -> None:
    for leg in legs:
        net_quantity = int(leg.get("net_quantity") or 0)
        broker_net_quantity = leg.get("broker_net_quantity")
        if not leg.get("exchange") or not leg.get("tradingsymbol") or not leg.get("product") or not leg.get("instrument_token"):
            raise HTTPException(status_code=409, detail="Live exit cannot proceed because one or more attributed legs is missing broker instrument metadata")
        if broker_net_quantity is None:
            raise HTTPException(
                status_code=409,
                detail=f"Live exit cannot proceed because broker position is missing for {leg.get('exchange')}:{leg.get('tradingsymbol')} {leg.get('product')}",
            )
        broker_net = int(broker_net_quantity or 0)
        if net_quantity > 0 and broker_net < net_quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Live exit cannot proceed because broker net quantity for {leg.get('tradingsymbol')} is lower than the attributed long quantity",
            )
        if net_quantity < 0 and broker_net > net_quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Live exit cannot proceed because broker net quantity for {leg.get('tradingsymbol')} is lower than the attributed short quantity",
            )

def _live_exit_orders_from_legs(legs: List[Dict[str, Any]], attribution: Dict[str, Any]) -> List[Dict[str, Any]]:
    orders: List[Dict[str, Any]] = []
    for leg in legs:
        net_quantity = int(leg.get("net_quantity") or 0)
        if net_quantity == 0:
            continue
        orders.append(
            {
                "exchange": str(leg["exchange"]),
                "tradingsymbol": str(leg["tradingsymbol"]),
                "transaction_type": "SELL" if net_quantity > 0 else "BUY",
                "variety": "regular",
                "product": str(leg["product"]),
                "order_type": "MARKET",
                "quantity": abs(net_quantity),
                "validity": "DAY",
                "market_protection": -1,
                "attribution": dict(attribution),
            }
        )
    return orders

def _live_exit_idempotency_key(*, strategy_run_id: str, legs: List[Dict[str, Any]], supplied_key: Optional[str]) -> str:
    if supplied_key:
        return supplied_key
    normalized = [
        {
            "instrument_token": int(leg.get("instrument_token") or 0),
            "product": str(leg.get("product") or ""),
            "net_quantity": int(leg.get("net_quantity") or 0),
        }
        for leg in legs
    ]
    digest = hashlib.sha1(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
    run_digest = hashlib.sha1(strategy_run_id.encode("utf-8")).hexdigest()[:8]
    return f"live-exit:{run_digest}:{digest}"

def _live_basket_execution_id(*, strategy_run_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha1(f"{strategy_run_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:20]
    return f"bex_{digest}"

async def _exit_live_worker_run(*, request: Request, token: WorkerToken, run: Dict[str, Any], payload: WorkerExitRequest) -> Dict[str, Any]:
    from broker_api.orders import BasketOrderRequest, OrdersService

    strategy_run_id = str(run["strategy_run_id"])
    if str(run.get("status") or "") == "closed":
        return {"mode": "live", "status": "closed", "message": "Live worker run is already closed", "run": run}

    account_id = str(run["account_scope"])
    kite = await asyncio.to_thread(_load_live_kite_for_account, account_id)
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-live-exit-{uuid.uuid4()}"
    refresh_result = await _refresh_live_account_state(kite=kite, account_id=account_id, corr_id=corr_id)
    legs = await _repo(request).list_live_strategy_open_legs(strategy_run_id=strategy_run_id, account_id=account_id)

    if not legs:
        attribution_refs = await _worker_run_live_attribution_refs(request, run)
        broker_positions = await _repo(request).list_live_strategy_broker_positions(
            strategy_run_id=strategy_run_id,
            account_id=account_id,
        )
        if not broker_positions:
            broker_positions = await _live_broker_positions_for_attribution(
                request,
                kite=kite,
                corr_id=corr_id,
                refs=attribution_refs,
            )
        if broker_positions:
            return {
                "mode": "live",
                "status": "deferred",
                "deferred": True,
                "message": "Live exit attribution is still synchronizing; broker exposure exists so the run cannot be marked flat yet",
                "broker_positions": broker_positions,
                "refresh": refresh_result,
                "run": run,
            }
        updated = await _repo(request).update_run_status(
            strategy_run_id,
            "closed",
            state_patch={
                "exit_reason": payload.reason or "live_worker_flat",
                "live_exit_finalized_at": _utcnow().isoformat(),
                "live_exit_flat_confirmation": {"source": "live_order_attribution", "refresh": refresh_result},
            },
        )
        return {"mode": "live", "status": "closed", "message": "Live worker run is already flat", "run": updated}

    _validate_live_exit_legs(legs)
    exit_idempotency_key = _live_exit_idempotency_key(
        strategy_run_id=strategy_run_id,
        legs=legs,
        supplied_key=payload.idempotency_key,
    )
    live_exit_state = dict((run.get("runtime_state") or {}).get("live_exit") or {})
    if live_exit_state.get("idempotency_key") == exit_idempotency_key and live_exit_state.get("order_result"):
        return {
            "mode": "live",
            "status": str(run.get("status") or "exiting"),
            "message": "Live exit was already submitted for this position state",
            "run": run,
            "exit": live_exit_state,
        }

    attribution = _live_attribution_for_worker_intent(
        token=token,
        run=run,
        request=WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key=exit_idempotency_key,
            payload={},
            metadata={"exit_reason": payload.reason or "live_worker_exit"},
        ),
    )
    orders = _live_exit_orders_from_legs(legs, attribution)
    planned_exit = {
        "idempotency_key": exit_idempotency_key,
        "reason": payload.reason or "live_worker_exit",
        "dry_run": payload.dry_run,
        "planned_at": _utcnow().isoformat(),
        "legs": legs,
        "orders": orders,
        "refresh": refresh_result,
    }

    if payload.dry_run:
        return {"mode": "live", "status": "dry_run", "message": "Live exit dry run built without placing broker orders", "exit": planned_exit}

    await _repo(request).update_run_status(strategy_run_id, "exiting", state_patch={"live_exit": planned_exit, "exit_reason": payload.reason})
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    worker_session_id = f"worker:{token.token_id}:{strategy_run_id}:exit"
    req = BasketOrderRequest.model_validate({"orders": orders, "all_or_none": False, "dry_run": False})
    result = await orders_service.place_basket(
        kite,
        req,
        corr_id,
        session_id=worker_session_id,
        idempotency_key=exit_idempotency_key,
        response=Response(),
    )
    result_payload = result.model_dump(mode="json")
    planned_exit["submitted_at"] = _utcnow().isoformat()
    planned_exit["order_result"] = result_payload

    post_refresh = await _refresh_live_account_state(kite=kite, account_id=account_id, corr_id=corr_id)
    remaining_legs = await _repo(request).list_live_strategy_open_legs(strategy_run_id=strategy_run_id, account_id=account_id)
    planned_exit["post_submit_refresh"] = post_refresh
    planned_exit["remaining_legs"] = remaining_legs

    if not remaining_legs:
        updated = await _repo(request).update_run_status(
            strategy_run_id,
            "closed",
            state_patch={
                "live_exit": planned_exit,
                "exit_reason": payload.reason or "live_worker_exit",
                "live_exit_finalized_at": _utcnow().isoformat(),
                "live_exit_flat_confirmation": {"source": "live_order_attribution", "refresh": post_refresh},
            },
        )
        return {"mode": "live", "status": "closed", "result": result_payload, "run": updated}

    updated = await _repo(request).update_run_status(strategy_run_id, "exiting", state_patch={"live_exit": planned_exit, "exit_reason": payload.reason})
    return {
        "mode": "live",
        "status": "exiting",
        "message": "Live exit orders submitted; run remains open until broker fills confirm the strategy is flat",
        "result": result_payload,
        "remaining_legs": remaining_legs,
        "run": updated,
    }

async def _place_bracket_entry(
    *,
    request: Request,
    token: WorkerToken,
    run: Dict[str, Any],
    bracket_intent_id: str,
    entry_order: Dict[str, Any],
    idempotency_key: str,
) -> Dict[str, Any]:
    from broker_api.orders import OrdersService, PlaceOrderRequest

    metadata = dict(run.get("metadata") or {})
    attribution = build_execution_attribution(
        execution_mode="live",
        strategy_run_id=str(run["strategy_run_id"]),
        strategy_family=str(metadata.get("strategy_family") or "indicator_strategy"),
        strategy_name=str(metadata.get("strategy_name") or run.get("template_id") or run["strategy_run_id"]),
        account_ref=str(run["account_scope"]),
        entry_surface="worker_bracket",
        source="algo_worker_bracket",
        idempotency_key=idempotency_key,
        metadata={
            "token_id": token.token_id,
            "template_id": run.get("template_id"),
            "bracket_intent_id": bracket_intent_id,
        },
        extras={"bracket_intent_id": bracket_intent_id},
    )
    attribution["bracket_intent_id"] = bracket_intent_id
    order_payload = _inject_live_attribution(dict(entry_order or {}), attribution)
    req = PlaceOrderRequest.model_validate(order_payload)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-bracket-entry-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    result = await orders_service.place_order(
        kite,
        req,
        corr_id,
        idempotency_key=idempotency_key,
        session_id=f"backend:bracket:{bracket_intent_id}",
        response=Response(),
    )
    return {
        "order_id": str(result.order_id),
        "idempotency_key": idempotency_key,
    }

async def list_worker_orders(request: Request, strategy_run_id: str):
    from broker_api.orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-orders-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    orders = await asyncio.to_thread(orders_service.orders, kite, corr_id)
    serialized = [_serialize_model(order) for order in orders]
    filtered = [order for order in serialized if _payload_matches_worker_run(order, strategy_run_id, attribution_refs)]
    return {"strategy_run_id": strategy_run_id, "orders": filtered}

async def list_worker_trades(request: Request, strategy_run_id: str):
    from broker_api.orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Trade inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-trades-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    trades = await asyncio.to_thread(orders_service.trades, kite, corr_id)
    serialized = [_serialize_model(trade) for trade in trades]
    filtered = [trade for trade in serialized if _payload_matches_worker_run(trade, strategy_run_id, attribution_refs)]
    return {"strategy_run_id": strategy_run_id, "trades": filtered}

async def get_worker_order(request: Request, order_id: str, strategy_run_id: str):
    from broker_api.orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-order-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    order = await asyncio.to_thread(orders_service.order_snapshot, kite, order_id, corr_id)
    payload = _serialize_model(order)
    if not _payload_matches_worker_run(payload, strategy_run_id, attribution_refs):
        raise HTTPException(status_code=404, detail="Order not found for strategy run")
    return {"strategy_run_id": strategy_run_id, "order": payload}

async def get_worker_order_history(request: Request, order_id: str, strategy_run_id: str):
    from broker_api.orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order inspection")
    attribution_refs = await _worker_run_live_attribution_refs(request, run)
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-order-history-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    history = await asyncio.to_thread(orders_service.order_history, kite, order_id, corr_id)
    entries = [_serialize_model(item) for item in history]
    if not entries or not any(_payload_matches_worker_run(item, strategy_run_id, attribution_refs) for item in entries):
        raise HTTPException(status_code=404, detail="Order not found for strategy run")
    return {"strategy_run_id": strategy_run_id, "order_id": order_id, "history": entries}

async def cancel_worker_order(request: Request, order_id: str, payload: WorkerOrderActionRequest):
    from broker_api.orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(payload.strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order cancellation")
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-cancel-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    result = await orders_service.cancel_order(
        kite,
        payload.variety or "regular",
        order_id,
        corr_id,
        parent_order_id=payload.parent_order_id,
    )
    return {"strategy_run_id": payload.strategy_run_id, "order_id": order_id, "result": _serialize_model(result)}

async def modify_worker_order(request: Request, order_id: str, payload: WorkerOrderModifyRequest):
    from broker_api.orders import OrdersService

    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(payload.strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Order modification")
    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    corr_id = request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"algo-worker-modify-{uuid.uuid4()}"
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    result = await orders_service.modify_order(
        kite,
        payload.variety or "regular",
        order_id,
        payload.to_modify_request(),
        corr_id,
        parent_order_id=payload.parent_order_id,
    )
    return {"strategy_run_id": payload.strategy_run_id, "order_id": order_id, "result": _serialize_model(result)}

async def preview_worker_order(request: Request, strategy_run_id: str, payload: WorkerOrderPreviewRequest):
    from broker_api.orders import OrdersService, PlaceOrderRequest
    from execution_accounting.kite_costs import build_live_order_cost_contract

    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    if str(run.get("execution_mode") or "").lower() != "live":
        raise HTTPException(status_code=409, detail="Order preview is only required for live runs")

    kite = await asyncio.to_thread(_load_live_kite_for_account, str(run["account_scope"]))
    order_payload = dict(payload.order or {})
    attribution = _live_attribution_for_worker_intent(
        token=token,
        run=run,
        request=WorkerIntentRequest(
            intent_type="place_order",
            idempotency_key=payload.idempotency_key or f"preview:{strategy_run_id}",
            payload={},
            metadata=payload.metadata or {},
        ),
    )
    req = PlaceOrderRequest.model_validate(_inject_live_attribution(order_payload, attribution))
    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    cost_contract = build_live_order_cost_contract(
        kite=kite,
        orders_service=orders_service,
        order=req.model_dump(exclude_none=True),
        corr_id=f"preview-{strategy_run_id}",
    )
    return {
        "strategy_run_id": strategy_run_id,
        "mode": "live",
        "preview": {
            "intent_type": "place_order",
            "order": req.model_dump(mode="json", exclude_none=True),
            "cost_contract": cost_contract.journal_payload(),
        },
    }

async def preview_worker_basket(request: Request, strategy_run_id: str, payload: WorkerBasketPreviewRequest):
    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    if str(run.get("execution_mode") or "").lower() != "live":
        raise HTTPException(status_code=409, detail="Basket preview is only required for live runs")
    preview = await _submit_live_worker_intent(
        request=request,
        token=token,
        run=run,
        payload=WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key=payload.idempotency_key or f"preview:{strategy_run_id}:basket",
            payload={"basket": {"orders": payload.orders, "all_or_none": payload.all_or_none, "dry_run": True}},
            metadata=payload.metadata or {},
        ),
    )
    return {"strategy_run_id": strategy_run_id, "mode": "live", "preview": preview}

async def create_worker_bracket(request: Request, strategy_run_id: str, payload: WorkerBracketCreateRequest):
    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Bracket intents")
    await require_active_worker_run_session(request, run)

    bracket_intent_id = f"brk_{uuid.uuid4().hex}"
    idempotency_key = str(payload.idempotency_key or f"bracket:{bracket_intent_id}:entry:1")
    if not idempotency_key.startswith("bracket:"):
        idempotency_key = f"bracket:{bracket_intent_id}:entry:1"

    db = SessionLocal()
    try:
        intent = bracket_runtime_store.create_bracket_intent(
            db,
            strategy_run_id=strategy_run_id,
            account_id=str(run.get("account_scope") or ""),
            config={
                "entry": dict(payload.entry_order or {}),
                "stoploss": dict(payload.stoploss or {}),
                "target": dict(payload.target or {}),
            },
            metadata={"created_by": "worker", **dict(payload.metadata or {})},
            bracket_intent_id=bracket_intent_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    try:
        entry_result = await _place_bracket_entry(
            request=request,
            token=token,
            run=run,
            bracket_intent_id=bracket_intent_id,
            entry_order=dict(payload.entry_order or {}),
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        db = SessionLocal()
        try:
            bracket_runtime_store.update_bracket_status(
                db,
                bracket_intent_id=bracket_intent_id,
                status="failed",
                action_required=False,
                action_reason="entry_submit_failed",
                metadata_patch={"entry_error": str(exc)},
            )
            db.commit()
        finally:
            db.close()
        raise

    db = SessionLocal()
    try:
        bracket_runtime_store.update_bracket_status(
            db,
            bracket_intent_id=bracket_intent_id,
            status="entry_working",
            action_required=False,
            action_reason=None,
            metadata_patch={"entry_result": dict(entry_result)},
        )
        latest = bracket_runtime_store.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_intent_id)
        if latest:
            cfg = dict(latest.get("config") or {})
            cfg.setdefault("entry", {})["broker_order_id"] = entry_result.get("order_id")
            db.execute(
                text(
                    """
                    UPDATE public.bracket_intents
                    SET config_json = :config_json,
                        updated_at = NOW()
                    WHERE bracket_intent_id = :bracket_intent_id
                    """
                ),
                {"bracket_intent_id": bracket_intent_id, "config_json": _json_dumps(cfg)},
            )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        current = bracket_runtime_store.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_intent_id)
    finally:
        db.close()
    return {
        "strategy_run_id": strategy_run_id,
        "bracket_intent_id": bracket_intent_id,
        "status": str((current or {}).get("status") or "entry_working"),
        "action_required": bool((current or {}).get("action_required")),
        "action_reason": (current or {}).get("action_reason"),
        "entry_result": entry_result,
    }

async def list_worker_brackets(request: Request, strategy_run_id: str, limit: int = Query(50, ge=1, le=200)):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Bracket intents")
    db = SessionLocal()
    try:
        rows = bracket_runtime_store.list_bracket_intents_for_run(db, strategy_run_id=strategy_run_id, limit=limit)
    finally:
        db.close()
    return {"strategy_run_id": strategy_run_id, "brackets": rows}

async def get_worker_bracket(request: Request, strategy_run_id: str, bracket_intent_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Bracket intents")
    db = SessionLocal()
    try:
        row = bracket_runtime_store.get_bracket_intent(db, strategy_run_id=strategy_run_id, bracket_intent_id=bracket_intent_id)
        if not row:
            raise HTTPException(status_code=404, detail="Bracket intent not found")
    finally:
        db.close()
    return row

async def cancel_worker_bracket(request: Request, strategy_run_id: str, bracket_intent_id: str):
    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    _require_live_run(run, feature="Bracket intents")
    await require_active_worker_run_session(request, run)

    db = SessionLocal()
    try:
        updated = bracket_runtime_store.request_cancel_bracket(
            db,
            strategy_run_id=strategy_run_id,
            bracket_intent_id=bracket_intent_id,
        )
        db.commit()
    except KeyError:
        db.rollback()
        raise HTTPException(status_code=404, detail="Bracket intent not found")
    finally:
        db.close()
    return {
        "strategy_run_id": strategy_run_id,
        "bracket_intent_id": bracket_intent_id,
        "status": str(updated.get("status") or "cancelling"),
        "action_required": bool(updated.get("action_required")),
        "action_reason": updated.get("action_reason"),
    }

async def submit_worker_intent(request: Request, strategy_run_id: str, payload: WorkerIntentRequest):
    token = await require_worker_token(request)
    _require_action(token, "intents:submit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    await require_active_worker_run_session(request, run)
    if str(run.get("status") or "open") != "open":
        raise HTTPException(status_code=409, detail="Worker intents can only be submitted for open strategy runs")
    mode = str(run.get("execution_mode") or "").lower()
    _require_v1_mode(mode)
    if mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot submit intents for this execution mode")

    existing = await _repo(request).get_intent_result(strategy_run_id, payload.idempotency_key)
    if existing is not None:
        return {"status": "deduped", "result": existing}

    if payload.safety_token:
        await validate_worker_run_safety_token(
            request,
            strategy_run_id,
            payload.safety_token,
            run=run,
        )

    result: Dict[str, Any]
    if mode == "dry_run":
        attribution = build_execution_attribution(
            execution_mode="dry_run",
            strategy_run_id=str(run["strategy_run_id"]),
            strategy_family=str((run.get("metadata") or {}).get("strategy_family") or "indicator_strategy"),
            strategy_name=str((run.get("metadata") or {}).get("strategy_name") or run.get("template_id") or run["strategy_run_id"]),
            account_ref=str(run.get("account_scope") or ""),
            entry_surface=str((run.get("metadata") or {}).get("entry_surface") or "algo_worker"),
            source="algo_worker",
            idempotency_key=payload.idempotency_key,
            metadata=payload.metadata,
            extras={
                "token_id": token.token_id,
                "template_id": run.get("template_id"),
                "strategy_id": str(run["strategy_run_id"]),
                "option_strategy_id": str(run["strategy_run_id"]),
            },
        )
        result = {
            "mode": "dry_run",
            "status": "validated",
            "intent_type": payload.intent_type,
            "mutated_state": False,
            "payload": payload.payload,
            "attribution": attribution,
        }
    elif mode == "live":
        result = await _submit_live_worker_intent(request=request, token=token, run=run, payload=payload)
    else:
        paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
        if paper_runtime_service is None:
            raise HTTPException(status_code=503, detail="Paper runtime is not available")
        attribution = _paper_attribution_for_worker_intent(token=token, run=run, request=payload)
        if payload.intent_type == "place_order":
            result = await paper_runtime_service.place_order(
                account_scope=str(run["account_scope"]),
                order_payload=payload.payload.get("order") or payload.payload,
                attribution=attribution,
            )
        elif payload.intent_type == "place_basket":
            result = await paper_runtime_service.place_basket(
                account_scope=str(run["account_scope"]),
                basket_payload=payload.payload.get("basket") or payload.payload,
                attribution=attribution,
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported intent_type '{payload.intent_type}'")

    if mode == "live" and payload.intent_type == "place_basket":
        return {"status": "accepted", "result": result}

    stored = await _repo(request).save_intent_result(
        token_id=token.token_id,
        strategy_run_id=strategy_run_id,
        request=payload,
        status=str(result.get("status") or "accepted"),
        result=result,
    )
    return {"status": "accepted", "result": stored}

async def list_worker_baskets(
    request: Request,
    strategy_run_id: str,
    limit: int = Query(100, ge=1, le=500),
):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    def _load() -> List[Dict[str, Any]]:
        db = SessionLocal()
        try:
            return basket_execution_store.list_baskets_for_run(db, strategy_run_id=strategy_run_id, limit=limit)
        finally:
            db.close()

    baskets = await asyncio.to_thread(_load)
    return {"strategy_run_id": strategy_run_id, "baskets": baskets}

async def get_worker_basket(request: Request, strategy_run_id: str, basket_execution_id: str):
    token = await require_worker_token(request)
    _require_action(token, "runs:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)

    def _load() -> Optional[Dict[str, Any]]:
        db = SessionLocal()
        try:
            return basket_execution_store.get_basket_for_run(
                db,
                strategy_run_id=strategy_run_id,
                basket_execution_id=basket_execution_id,
            )
        finally:
            db.close()

    basket = await asyncio.to_thread(_load)
    if basket is None:
        raise HTTPException(status_code=404, detail="Basket execution not found")
    return basket

async def exit_worker_run(request: Request, strategy_run_id: str, payload: WorkerExitRequest):
    token = await require_worker_token(request)
    _require_action(token, "runs:exit")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    await require_active_worker_run_session(request, run)
    mode = str(run.get("execution_mode") or "").lower()
    _require_v1_mode(mode)
    if mode == "dry_run":
        updated = await _repo(request).update_run_status(strategy_run_id, "closed", state_patch={"exit_reason": payload.reason or "dry_run_exit"})
        return {"mode": "dry_run", "status": "closed", "run": updated}
    if mode == "live":
        return await _exit_live_worker_run(request=request, token=token, run=run, payload=payload)

    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    result = await paper_runtime_service.exit_strategy(account_scope=str(run["account_scope"]), strategy_id=strategy_run_id)
    result_status = str(result.get("status") or "success").lower()
    if result_status in {"success", "closed", "noop"}:
        updated = await _repo(request).update_run_status(strategy_run_id, "closed", state_patch={"exit_result": result, "exit_reason": payload.reason})
        return {"mode": "paper", "status": "closed", "result": result, "run": updated}
    updated = await _repo(request).update_run_status(strategy_run_id, str(run.get("status") or "open"), state_patch={"exit_result": result, "exit_reason": payload.reason})
    return {"mode": "paper", "status": result_status, "result": result, "run": updated}
