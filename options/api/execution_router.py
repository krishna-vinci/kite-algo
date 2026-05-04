from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from algo_runtime.account_scope import parse_account_scope
from algo_runtime.execution_attribution import build_execution_attribution
from broker_api.kite_orders import OrdersService
from broker_api.kite_session import build_kite_client, get_system_access_token
from database import SessionLocal
from execution_accounting.contracts import ChargesStatus, ExecutionCostContract
from execution_accounting.kite_costs import build_live_basket_cost_contract

from options.execution import (
    OptionRunCreateRequest,
    get_option_run_store,
    mark_cleanup_required,
    mark_closed,
    mark_entering,
    mark_entry_previewed,
    mark_exit_previewed,
    mark_exiting,
    mark_partial_entry,
    mark_partial_exit,
)
from options.execution.previews import build_entry_preview_packet, build_exit_preview_packet
from options.execution.runtime_instance import (
    OptionExecutionRuntimeInstance,
    get_option_execution_runtime_instance,
)
from options.execution.store import OptionRunStore
from options.execution.updates import summarize_order_results

router = APIRouter(prefix="/api/options", tags=["Options"])


def _get_run_or_404(store: OptionRunStore, strategy_run_id: str):
    try:
        return store.get_run(strategy_run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "OPTION_RUN_NOT_FOUND",
                "message": f"Option run not found: {strategy_run_id}",
            },
        ) from exc


def _invalid_transition(exc: Exception):
    raise HTTPException(
        status_code=409,
        detail={
            "code": "OPTION_RUN_INVALID_TRANSITION",
            "message": str(exc),
        },
    )


def _normalize_injected_orders(results: list[dict], *, phase: str) -> list[dict]:
    normalized: list[dict] = []
    for item in results:
        entry = dict(item)
        entry.setdefault("status", "filled")
        entry["status"] = str(entry.get("status") or "").lower()
        entry["phase"] = phase
        normalized.append(entry)
    return normalized


def _normalize_injected_trades(results: list[dict], *, phase: str) -> list[dict]:
    normalized: list[dict] = []
    for item in results:
        entry = dict(item)
        entry["phase"] = phase
        normalized.append(entry)
    return normalized


def _build_broker_preview_contract(request: Request, order_plan: list[dict]) -> ExecutionCostContract | None:
    if not order_plan:
        return None
    try:
        db = SessionLocal()
        try:
            access_token = get_system_access_token(db)
        finally:
            db.close()
    except Exception:
        return None
    if not access_token:
        return None
    try:
        kite = build_kite_client(access_token, session_id="system")
        orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
        return build_live_basket_cost_contract(
            kite=kite,
            orders_service=orders_service,
            orders=[dict(order) for order in order_plan],
            corr_id=f"options-preview:{order_plan[0].get('leg_id') or 'basket'}",
        )
    except Exception:
        return None


def _attach_broker_preview_contract(preview_packet: dict, contract: ExecutionCostContract | None) -> dict:
    if contract is None:
        return preview_packet
    payload = contract.journal_payload()
    preview_packet["cost_contract"] = payload
    preview_packet.setdefault("margin", {})["cost_contract"] = payload
    preview_packet.setdefault("charges", {})["cost_contract"] = payload
    if contract.charges_status == ChargesStatus.BROKER_QUOTED:
        preview_packet["margin"]["fallback_required"] = preview_packet["margin"].get("required")
        preview_packet["charges"]["fallback_estimated"] = preview_packet["charges"].get("estimated")
        preview_packet["margin"]["required"] = float(contract.margin_required)
        preview_packet["margin"]["source"] = ChargesStatus.BROKER_QUOTED.value
        preview_packet["charges"]["estimated"] = float(contract.total_charges or contract.charges_estimate)
        preview_packet["charges"]["source"] = ChargesStatus.BROKER_QUOTED.value
    elif contract.charges_status == ChargesStatus.UNAVAILABLE:
        preview_packet["margin"].setdefault("source", ChargesStatus.UNAVAILABLE.value)
        preview_packet["charges"].setdefault("source", ChargesStatus.UNAVAILABLE.value)
    return preview_packet


def _should_route_to_paper(payload: dict) -> bool:
    mode = str(payload.get("mode") or payload.get("execution_mode") or "").strip().lower()
    if mode == "paper":
        return True
    account_scope = str(payload.get("account_scope") or "").strip()
    if not account_scope:
        return False
    try:
        return parse_account_scope(account_scope).mode == "paper"
    except ValueError:
        return False


def _paper_account_scope_for_run(payload: dict, run) -> str:
    candidate = str(payload.get("account_scope") or (run.metadata or {}).get("account_scope") or "").strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="paper execution requires account_scope")
    if ":" not in candidate:
        return candidate
    try:
        parsed = parse_account_scope(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if parsed.mode != "paper":
        raise HTTPException(status_code=422, detail="paper execution requires a paper account_scope")
    return parsed.normalized


def _build_paper_basket_attribution(run, account_scope: str, payload: dict) -> dict:
    metadata = dict(run.metadata or {})
    return build_execution_attribution(
        execution_mode="paper",
        strategy_run_id=str(run.strategy_run_id),
        strategy_family=str(metadata.get("strategy_family") or "options"),
        strategy_name=str(metadata.get("strategy_name") or run.strategy_name or run.strategy_run_id),
        account_ref=account_scope,
        entry_surface=str(metadata.get("entry_surface") or "options_api"),
        source="options_api",
        idempotency_key=str(payload.get("idempotency_key") or f"{run.strategy_run_id}:enter"),
        metadata=payload.get("metadata") or {},
        extras={
            "strategy_id": str(run.strategy_run_id),
            "option_strategy_id": str(run.strategy_run_id),
            "product": str(run.product or ""),
        },
    )


async def _submit_paper_entry(
    *,
    request: Request,
    run,
    payload: dict,
    order_plan: list[dict],
) -> tuple[list[dict], list[dict], dict]:
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")

    account_scope = _paper_account_scope_for_run(payload, run)
    attribution = _build_paper_basket_attribution(run, account_scope, payload)
    basket_orders = []
    for order in order_plan:
        order_metadata = dict((order.get("metadata") or {}))
        order_metadata.setdefault("leg_id", order.get("leg_id"))
        basket_orders.append({**dict(order), "metadata": order_metadata})

    paper_result = await paper_runtime_service.place_basket(
        account_scope=account_scope,
        basket_payload={
            "orders": basket_orders,
            "all_or_none": bool(payload.get("all_or_none", False)),
            "metadata": payload.get("metadata") or {},
        },
        attribution=attribution,
    )
    order_results, trade_results = _normalize_paper_basket_result(order_plan=order_plan, paper_result=paper_result)
    return order_results, trade_results, paper_result


def _normalize_paper_basket_result(*, order_plan: list[dict], paper_result: dict) -> tuple[list[dict], list[dict]]:
    leg_by_symbol = {
        (str(item.get("tradingsymbol") or "").strip().upper(), str(item.get("transaction_type") or "").strip().upper()): str(item.get("leg_id") or "")
        for item in order_plan
    }
    order_results: list[dict] = []
    trade_results: list[dict] = []
    seen_order_ids: set[str] = set()

    for item in list(paper_result.get("results") or []) + list(paper_result.get("errors") or []):
        order = dict(item.get("order") or {})
        trade = dict(item.get("trade") or {})
        order_id = str(order.get("order_id") or "")
        if order_id and order_id in seen_order_ids:
            continue
        if order_id:
            seen_order_ids.add(order_id)
        metadata = dict(order.get("metadata") or trade.get("metadata") or {})
        transaction_type = str(order.get("transaction_type") or trade.get("transaction_type") or "").upper()
        tradingsymbol = str(order.get("tradingsymbol") or trade.get("tradingsymbol") or "").strip().upper()
        leg_id = str(metadata.get("leg_id") or leg_by_symbol.get((tradingsymbol, transaction_type)) or "")
        status = str(item.get("status") or order.get("status") or "pending").lower()
        order_results.append(
            {
                "order_id": order.get("order_id"),
                "leg_id": leg_id,
                "tradingsymbol": tradingsymbol or None,
                "transaction_type": transaction_type or None,
                "quantity": int(order.get("quantity") or trade.get("quantity") or 0),
                "product": order.get("product") or trade.get("product"),
                "status": status,
                "phase": "entry",
            }
        )
        if trade:
            trade_results.append(
                {
                    "trade_id": trade.get("trade_id"),
                    "order_id": trade.get("order_id") or order.get("order_id"),
                    "leg_id": leg_id,
                    "tradingsymbol": tradingsymbol or None,
                    "transaction_type": transaction_type or None,
                    "quantity": int(trade.get("quantity") or order.get("quantity") or 0),
                    "product": trade.get("product") or order.get("product"),
                    "phase": "entry",
                }
            )
    return order_results, trade_results


@router.post("/runs")
async def create_option_run(
    payload: OptionRunCreateRequest,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = store.create_run(payload)
    return run.__dict__


@router.get("/runs")
async def list_option_runs(store: OptionRunStore = Depends(get_option_run_store)):
    return {"runs": [run.__dict__ for run in store.list_runs()]}


@router.get("/runs/{strategy_run_id}")
async def get_option_run(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return run.__dict__


@router.post("/runs/{strategy_run_id}/preview-entry")
async def preview_option_run_entry(
    strategy_run_id: str,
    request: Request,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
):
    _ = payload
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_entry_previewed(run)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    instruments_repository = getattr(paper_runtime_service, "instruments_repository", None)
    preview_packet = build_entry_preview_packet(run, instruments_repository=instruments_repository)
    return _attach_broker_preview_contract(preview_packet, _build_broker_preview_contract(request, preview_packet.get("order_plan") or []))


@router.post("/runs/{strategy_run_id}/enter")
async def enter_option_run(
    strategy_run_id: str,
    request: Request,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    payload = payload or {}
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_entering(run)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)

    order_plan = runtime.build_entry_plan(run)
    paper_result = None
    if _should_route_to_paper(payload):
        order_results, trade_results, paper_result = await _submit_paper_entry(
            request=request,
            run=run,
            payload=payload,
            order_plan=order_plan,
        )
    elif payload.get("order_results") is None and payload.get("trade_results") is None:
        order_results, trade_results = runtime.default_entry_results(run)
    else:
        order_results = _normalize_injected_orders(payload.get("order_results") or [], phase="entry")
        trade_results = _normalize_injected_trades(payload.get("trade_results") or [], phase="entry")

    if order_results:
        run = store.record_orders(strategy_run_id, order_results)
    if trade_results:
        run = store.record_trades(strategy_run_id, trade_results)

    summary = summarize_order_results(order_plan, order_results)
    if summary.failed_legs:
        try:
            run = mark_partial_entry(
                run,
                completed_legs=summary.completed_legs,
                failed_legs=[],
                pending_legs=summary.pending_legs or summary.failed_legs,
            )
            run.failed_legs = list(summary.failed_legs)
            run = mark_cleanup_required(run)
        except ValueError as exc:
            _invalid_transition(exc)
    else:
        try:
            run = mark_partial_entry(
                run,
                completed_legs=summary.completed_legs,
                failed_legs=[],
                pending_legs=summary.pending_legs,
            )
        except ValueError as exc:
            _invalid_transition(exc)
    store.save_run(run)

    return {
        "strategy_run_id": run.strategy_run_id,
        "mode": "paper" if paper_result is not None else payload.get("mode") or payload.get("execution_mode") or "deterministic",
        "status": run.status,
        "completed_legs": list(run.completed_legs),
        "failed_legs": list(run.failed_legs),
        "pending_legs": list(run.pending_legs),
        "orders": list(order_results),
        "trades": list(trade_results),
        **({"paper_result": paper_result} if paper_result is not None else {}),
    }


@router.post("/runs/{strategy_run_id}/preview-exit")
async def preview_option_run_exit(
    strategy_run_id: str,
    request: Request,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
):
    _ = payload
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_exit_previewed(run)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)
    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    instruments_repository = getattr(paper_runtime_service, "instruments_repository", None)
    preview_packet = build_exit_preview_packet(run, instruments_repository=instruments_repository)
    return _attach_broker_preview_contract(preview_packet, _build_broker_preview_contract(request, preview_packet.get("order_plan") or []))


@router.post("/runs/{strategy_run_id}/exit")
async def exit_option_run(
    strategy_run_id: str,
    payload: dict | None = None,
    store: OptionRunStore = Depends(get_option_run_store),
    runtime: OptionExecutionRuntimeInstance = Depends(get_option_execution_runtime_instance),
):
    payload = payload or {}
    run = _get_run_or_404(store, strategy_run_id)
    try:
        run = mark_exiting(run, pending_legs=run.completed_legs)
    except ValueError as exc:
        _invalid_transition(exc)
    store.save_run(run)

    order_plan = runtime.build_exit_plan(run)
    if payload.get("order_results") is None and payload.get("trade_results") is None:
        order_results, trade_results = runtime.default_exit_results(run)
    else:
        order_results = _normalize_injected_orders(payload.get("order_results") or [], phase="exit")
        trade_results = _normalize_injected_trades(payload.get("trade_results") or [], phase="exit")

    if order_results:
        run = store.record_orders(strategy_run_id, order_results)
    if trade_results:
        run = store.record_trades(strategy_run_id, trade_results)

    summary = summarize_order_results(order_plan, order_results)
    if summary.failed_legs or summary.pending_legs:
        try:
            run = mark_partial_exit(
                run,
                remaining_open_legs=summary.failed_legs + summary.pending_legs,
                failed_legs=[],
            )
        except ValueError as exc:
            _invalid_transition(exc)
        run.failed_legs = list(summary.failed_legs)
    else:
        try:
            run = mark_closed(run)
        except ValueError as exc:
            _invalid_transition(exc)
    store.save_run(run)

    return {
        "strategy_run_id": run.strategy_run_id,
        "status": run.status,
        "completed_legs": list(run.completed_legs),
        "failed_legs": list(run.failed_legs),
        "pending_legs": list(run.pending_legs),
        "orders": list(order_results),
        "trades": list(trade_results),
    }


@router.get("/runs/{strategy_run_id}/orders")
async def list_option_run_orders(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return {"strategy_run_id": strategy_run_id, "orders": list(run.orders)}


@router.get("/runs/{strategy_run_id}/trades")
async def list_option_run_trades(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return {"strategy_run_id": strategy_run_id, "trades": list(run.trades)}


@router.get("/runs/{strategy_run_id}/state")
async def get_option_run_state(
    strategy_run_id: str,
    store: OptionRunStore = Depends(get_option_run_store),
):
    run = _get_run_or_404(store, strategy_run_id)
    return {
        "strategy_run_id": strategy_run_id,
        "state": {
            "status": run.status,
            "completed_legs": list(run.completed_legs),
            "failed_legs": list(run.failed_legs),
            "pending_legs": list(run.pending_legs),
            "product": run.product,
        },
    }
