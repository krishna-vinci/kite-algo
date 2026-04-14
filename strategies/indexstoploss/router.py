"""
Slim strategy router for the options workspace and runtime-managed option execution.

Legacy position-protection endpoints were removed after the option strategy flow
fully moved onto the modular algo runtime.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from kiteconnect import KiteConnect
from pydantic import BaseModel

from algo_runtime.admin import (
    update_instance_status as update_algo_runtime_instance_status_impl,
    upsert_instance as upsert_algo_runtime_instance_impl,
)
from algo_runtime.models import AlgoLifecycleState
from auth_service import require_app_user
from broker_api.broker_api import get_kite
from broker_api.instruments_repository import InstrumentsRepository
from broker_api.kite_orders import get_correlation_id, realtime_positions_service, run_kite_write_action
from broker_api.kite_session import get_kite_session_id, get_session_account_id
from database import SessionLocal
from strategies.option_strategy import (
    OptionStrategyStore,
    StrategyExecutionMode,
    StrategyProtectionPreferences,
    build_runtime_option_instance,
    compile_option_strategy_preview,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Strategies"])


def _get_strike_selector(request: Request):
    if not hasattr(request.app.state, "strike_selector"):
        from strategies.strike_selector import StrikeSelector

        osm = getattr(request.app.state, "options_session_manager", None)
        if not osm:
            raise HTTPException(status_code=503, detail="Options session manager not available. Start an options session first.")

        instruments_repo = InstrumentsRepository(db=SessionLocal)
        request.app.state.strike_selector = StrikeSelector(osm, instruments_repo)
        logger.info("StrikeSelector initialized")

    return request.app.state.strike_selector


def _get_position_builder(request: Request):
    if not hasattr(request.app.state, "position_builder"):
        from strategies.strike_selector import PositionBuilder

        strike_selector = _get_strike_selector(request)
        instruments_repo = InstrumentsRepository(db=SessionLocal)
        request.app.state.position_builder = PositionBuilder(strike_selector, instruments_repo)
        logger.info("PositionBuilder initialized")

    return request.app.state.position_builder


def _get_option_strategy_store(request: Request) -> OptionStrategyStore:
    if not hasattr(request.app.state, "option_strategy_store"):
        request.app.state.option_strategy_store = OptionStrategyStore(
            journal_service=getattr(request.app.state, "journal_service", None),
        )
    return request.app.state.option_strategy_store


def _resolve_live_runtime_identity(request: Request) -> tuple[str, str]:
    session_id = get_kite_session_id(request)
    if not session_id:
        raise HTTPException(status_code=401, detail="Live execution requires an authenticated Kite session")
    db = SessionLocal()
    try:
        account_scope = get_session_account_id(db, session_id)
    finally:
        db.close()
    if not account_scope:
        raise HTTPException(status_code=503, detail="Live execution could not resolve broker account identity for runtime monitoring")
    return session_id, account_scope


def _runtime_monitoring_required(req: "BuildPositionRequest") -> bool:
    return bool(req.enable_runtime_monitoring)


def _build_runtime_instance_for_plan(request: Request, req: "BuildPositionRequest", plan: Dict[str, Any], strategy_id: str, strategy_preview: Dict[str, Any], execution_mode: str):
    if not _runtime_monitoring_required(req):
        return None
    rules = list(strategy_preview.get("rules") or [])
    if not rules:
        return None

    spot_token = None
    if any(str(rule.get("metric") or "") == "index_price" for rule in rules):
        spot_token = InstrumentsRepository(db=SessionLocal).get_spot_token(req.underlying.upper())
        if spot_token is None:
            raise HTTPException(status_code=503, detail=f"Could not resolve spot token for {req.underlying} required by runtime monitoring")

    if execution_mode == StrategyExecutionMode.LIVE.value:
        session_id, account_scope = _resolve_live_runtime_identity(request)
    else:
        session_id, account_scope = None, req.account_scope

    return build_runtime_option_instance(
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        account_scope=account_scope,
        selected_legs=plan.get("strategy_legs") or [item.model_dump(mode="json") for item in (req.selected_strikes or [])],
        strategy_preview=strategy_preview,
        session_id=session_id,
        spot_token=spot_token,
        underlying=req.underlying.upper(),
    )


async def _arm_runtime_monitoring(request: Request, instance) -> Optional[str]:
    if instance is None:
        return None
    service = getattr(request.app.state, "algo_runtime_service", None)
    if not service:
        raise HTTPException(status_code=503, detail="Algo runtime service is not available for runtime-managed exits")
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    result = await upsert_algo_runtime_instance_impl(service, instance, live_worker=live_worker)
    return result["instance"]["instance_id"]


async def _activate_runtime_monitoring(request: Request, instance_id: Optional[str]) -> None:
    if not instance_id:
        return
    service = getattr(request.app.state, "algo_runtime_service", None)
    if not service:
        raise HTTPException(status_code=503, detail="Algo runtime service is not available for runtime-managed exits")
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    updated = await update_algo_runtime_instance_status_impl(
        service,
        instance_id=instance_id,
        status=AlgoLifecycleState.ENABLED,
        live_worker=live_worker,
    )
    if updated is None:
        raise HTTPException(status_code=500, detail=f"Failed to activate runtime monitoring for {instance_id}")


async def _disarm_runtime_monitoring(request: Request, instance_id: Optional[str]) -> None:
    if not instance_id:
        return
    service = getattr(request.app.state, "algo_runtime_service", None)
    if not service:
        return
    live_worker = getattr(request.app.state, "algo_runtime_live_worker", None)
    try:
        await update_algo_runtime_instance_status_impl(
            service,
            instance_id=instance_id,
            status=AlgoLifecycleState.STOPPED,
            live_worker=live_worker,
        )
    except Exception:
        logger.warning("Failed to stop runtime-managed option strategy %s", instance_id, exc_info=True)


def _resolve_execution_mode(req: "BuildPositionRequest") -> str:
    if req.execution_mode is not None:
        return req.execution_mode.value if hasattr(req.execution_mode, "value") else str(req.execution_mode)
    return StrategyExecutionMode.LIVE.value if req.place_orders else StrategyExecutionMode.DRY_RUN.value


def _compile_preview_from_plan(req: "BuildPositionRequest", plan: Dict[str, Any]) -> Dict[str, Any]:
    strategy_legs = plan.get("strategy_legs") or [item.model_dump() for item in (req.selected_strikes or [])]
    preview = compile_option_strategy_preview(
        underlying=req.underlying.upper(),
        template_id=req.template_id,
        strategy_type=req.strategy_type,
        current_spot=req.current_spot,
        legs=strategy_legs,
        protection_preferences=StrategyProtectionPreferences.from_payload(req.protection_config),
    )
    return preview.model_dump(mode="json")


class SuggestStrikesRequest(BaseModel):
    underlying: str
    expiry: date
    strategy_type: str
    target_delta: float = 0.30
    risk_amount: Optional[float] = None


class SelectedStrikeData(BaseModel):
    instrument_token: int
    tradingsymbol: str
    strike: float
    option_type: str
    ltp: float
    lot_size: int
    delta: float
    lots: int
    transaction_type: str
    quantity: Optional[int] = None
    expiry_key: Optional[str] = None


class BuildPositionRequest(BaseModel):
    underlying: str
    expiry: date
    strategy_type: str
    template_id: Optional[str] = None
    selected_strikes: Optional[List[SelectedStrikeData]] = None
    target_delta: float = 0.30
    risk_amount: Optional[float] = None
    protection_config: Optional[Dict[str, Any]] = None
    current_spot: Optional[float] = None
    entry_surface: Optional[str] = None
    execution_mode: Optional[StrategyExecutionMode] = None
    account_scope: str = "default"
    enable_runtime_monitoring: bool = True
    place_orders: bool = False


class StrategyPreviewRequest(BaseModel):
    underlying: str
    expiry: date
    strategy_type: str
    template_id: Optional[str] = None
    selected_strikes: List[SelectedStrikeData]
    protection_config: Optional[Dict[str, Any]] = None
    current_spot: Optional[float] = None


@router.get("/available-expiries/{underlying}")
async def get_available_expiries(underlying: str, request: Request):
    try:
        osm = request.app.state.options_session_manager
        session = osm.sessions.get(underlying.upper())
        if not session or not session.snapshot:
            raise HTTPException(status_code=404, detail=f"No active options session for {underlying}. Start a session first.")

        snapshot = session.snapshot
        return {
            "underlying": underlying.upper(),
            "expiries": snapshot.get("expiries", []),
            "spot_ltp": snapshot.get("spot_ltp"),
            "timestamp": snapshot.get("timestamp"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get available expiries: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mini-chain/{underlying}/{expiry}")
async def get_mini_chain(
    underlying: str,
    expiry: date,
    request: Request,
    center_strike: Optional[float] = Query(None, description="Center strike (uses ATM if not provided)"),
    count: int = Query(11, ge=5, le=25, description="Number of strikes to return"),
):
    try:
        selector = _get_strike_selector(request)
        chain_data = await selector.get_mini_chain(
            underlying=underlying.upper(),
            expiry=expiry,
            center_strike=center_strike,
            count=count,
        )
        if isinstance(chain_data, dict) and "error" in chain_data:
            raise HTTPException(status_code=404, detail=chain_data["error"])
        return chain_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get mini chain: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/suggest-strikes")
async def suggest_strikes(req: SuggestStrikesRequest, request: Request):
    try:
        selector = _get_strike_selector(request)
        return await selector.suggest_strikes(
            underlying=req.underlying.upper(),
            expiry=req.expiry,
            strategy_type=req.strategy_type,
            target_delta=req.target_delta,
            risk_amount=req.risk_amount,
        )
    except Exception as e:
        logger.error("Failed to suggest strikes: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/build-position")
async def build_position(
    req: BuildPositionRequest,
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    strategy_id = None
    algo_instance_id = None
    try:
        builder = _get_position_builder(request)
        execution_mode = _resolve_execution_mode(req)
        if execution_mode == StrategyExecutionMode.PAPER.value:
            require_app_user(request)

        if req.selected_strikes:
            plan = await builder.build_position_plan_from_strikes(
                underlying=req.underlying.upper(),
                expiry=req.expiry,
                strategy_type=req.strategy_type,
                selected_strikes=[s.model_dump() for s in req.selected_strikes],
                protection_config=req.protection_config,
            )
        else:
            plan = await builder.build_position_plan(
                underlying=req.underlying.upper(),
                expiry=req.expiry,
                strategy_type=req.strategy_type,
                target_delta=req.target_delta,
                risk_amount=req.risk_amount,
                protection_config=req.protection_config,
            )

        if "error" in plan:
            raise HTTPException(status_code=400, detail=plan["error"])

        strategy_preview = _compile_preview_from_plan(req, plan)
        plan["canonical_strategy"] = strategy_preview

        if execution_mode == StrategyExecutionMode.DRY_RUN.value:
            return {
                "mode": StrategyExecutionMode.DRY_RUN.value,
                "plan": plan,
                "strategy": strategy_preview,
                "message": "Dry run complete. Use execution_mode=paper or live to execute.",
            }

        strategy_store = _get_option_strategy_store(request)
        strategy_id = strategy_store.create_run(
            underlying=req.underlying.upper(),
            expiry=req.expiry.isoformat(),
            user_intent=strategy_preview["user_intent"],
            inferred_structure=strategy_preview["inferred_structure"],
            inferred_family=strategy_preview["inferred_family"],
            execution_mode=execution_mode,
            selected_legs=plan.get("strategy_legs") or [item.model_dump(mode="json") for item in (req.selected_strikes or [])],
            canonical_strategy=strategy_preview,
            order_plan=plan,
            entry_surface=req.entry_surface,
        )
        journal_run_id = strategy_store.get_linked_journal_run_id(strategy_id)

        runtime_instance = _build_runtime_instance_for_plan(request, req, plan, strategy_id, strategy_preview, execution_mode)
        if runtime_instance is not None and journal_run_id:
            runtime_instance = runtime_instance.model_copy(
                update={
                    "metadata": {
                        **runtime_instance.metadata,
                        "journal_run_id": journal_run_id,
                        "journal_ref": f"option_strategy_run:{strategy_id}",
                    }
                }
            )
        if runtime_instance is not None and execution_mode == StrategyExecutionMode.LIVE.value:
            runtime_instance = runtime_instance.model_copy(update={"status": AlgoLifecycleState.PAUSED})
        algo_instance_id = await _arm_runtime_monitoring(request, runtime_instance)

        if execution_mode == StrategyExecutionMode.PAPER.value:
            paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
            if not paper_runtime_service:
                await _disarm_runtime_monitoring(request, algo_instance_id)
                raise HTTPException(status_code=503, detail="Paper runtime is not available")
            basket_result = await paper_runtime_service.place_basket(
                account_scope=req.account_scope,
                basket_payload={
                    "orders": [
                        {
                            "exchange": order.get("exchange", "NFO"),
                            "tradingsymbol": order["tradingsymbol"],
                            "product": order.get("product", "MIS"),
                            "transaction_type": order["transaction_type"],
                            "order_type": order.get("order_type", "MARKET"),
                            "quantity": order["quantity"],
                        }
                        for order in plan["orders"]
                    ],
                    "all_or_none": True,
                },
                attribution={
                    "source": "frontend-next-options",
                    "strategy_tag": strategy_preview["inferred_structure"],
                    "option_strategy_id": strategy_id,
                    "algo_instance_id": algo_instance_id,
                    "journal_run_id": journal_run_id,
                    "journal_ref": f"option_strategy_run:{strategy_id}",
                    "notes": f"options-page:{strategy_preview['user_intent']}",
                },
            )
            status = str(basket_result.get("status") or "success")
            if status == "failed":
                await _disarm_runtime_monitoring(request, algo_instance_id)
            strategy_store.update_execution_result(
                strategy_id,
                status=status,
                execution_result=basket_result,
                algo_instance_id=algo_instance_id,
            )
            return {
                "mode": StrategyExecutionMode.PAPER.value,
                "status": status,
                "strategy_id": strategy_id,
                "journal_run_id": journal_run_id,
                "algo_instance_id": algo_instance_id,
                "strategy": strategy_preview,
                "plan": plan,
                "message": f"Paper strategy {status}",
                **basket_result,
            }

        orders_placed = []
        orders_failed = []
        for order in plan["orders"]:
            try:
                order_id = await run_kite_write_action(
                    "option_strategy_place_order",
                    corr_id,
                    lambda _order=order: kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange=kite.EXCHANGE_NFO,
                        tradingsymbol=_order["tradingsymbol"],
                        transaction_type=_order["transaction_type"],
                        quantity=_order["quantity"],
                        product=kite.PRODUCT_MIS,
                        order_type=kite.ORDER_TYPE_MARKET,
                    ),
                    meta={"strategy_type": req.strategy_type, "underlying": req.underlying, "tradingsymbol": order["tradingsymbol"]},
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.error("Failed to place order for %s: %s", order["tradingsymbol"], e)
                orders_failed.append({"tradingsymbol": order["tradingsymbol"], "error": str(e)})
                continue

            orders_placed.append(
                {
                    "order_id": order_id,
                    "tradingsymbol": order["tradingsymbol"],
                    "transaction_type": order["transaction_type"],
                    "quantity": order["quantity"],
                    "status": "placed",
                }
            )

        if not orders_placed:
            await _disarm_runtime_monitoring(request, algo_instance_id)
            strategy_store.update_execution_result(
                strategy_id,
                status="failed",
                execution_result={
                    "orders_placed": [],
                    "orders_failed": orders_failed,
                    "message": "All orders failed to place",
                },
                algo_instance_id=algo_instance_id,
            )
            return {
                "mode": execution_mode,
                "status": "failed",
                "orders_placed": [],
                "orders_failed": orders_failed,
                "strategy": strategy_preview,
                "strategy_id": strategy_id,
                "journal_run_id": journal_run_id,
                "algo_instance_id": algo_instance_id,
                "message": "All orders failed to place",
            }

        await _activate_runtime_monitoring(request, algo_instance_id)

        strategy_store.update_execution_result(
            strategy_id,
            status="success" if not orders_failed else "partial",
            execution_result={
                "orders_placed": orders_placed,
                "orders_failed": orders_failed,
                "message": f"Executed {len(orders_placed)}/{len(plan['orders'])} orders successfully",
            },
            algo_instance_id=algo_instance_id,
        )

        return {
            "mode": execution_mode,
            "status": "success" if not orders_failed else "partial",
            "orders_placed": orders_placed,
            "orders_failed": orders_failed,
            "strategy_id": strategy_id,
            "journal_run_id": journal_run_id,
            "algo_instance_id": algo_instance_id,
            "strategy": strategy_preview,
            "plan": plan,
            "message": f"Executed {len(orders_placed)}/{len(plan['orders'])} orders successfully",
        }
    except HTTPException as exc:
        if algo_instance_id:
            await _disarm_runtime_monitoring(request, algo_instance_id)
        if strategy_id:
            try:
                _get_option_strategy_store(request).update_execution_result(
                    strategy_id,
                    status="failed",
                    execution_result={"message": str(exc.detail) if hasattr(exc, "detail") else str(exc)},
                    algo_instance_id=algo_instance_id,
                )
            except Exception:
                logger.warning("Failed to persist option strategy failure for %s", strategy_id, exc_info=True)
        raise
    except Exception as e:
        if algo_instance_id:
            await _disarm_runtime_monitoring(request, algo_instance_id)
        if strategy_id:
            try:
                _get_option_strategy_store(request).update_execution_result(
                    strategy_id,
                    status="failed",
                    execution_result={"message": str(e)},
                    algo_instance_id=algo_instance_id,
                )
            except Exception:
                logger.warning("Failed to persist option strategy failure for %s", strategy_id, exc_info=True)
        logger.error("Failed to build position: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/preview-option-strategy")
async def preview_option_strategy(req: StrategyPreviewRequest):
    try:
        preview = compile_option_strategy_preview(
            underlying=req.underlying.upper(),
            template_id=req.template_id,
            strategy_type=req.strategy_type,
            current_spot=req.current_spot,
            legs=[item.model_dump(mode="json") for item in req.selected_strikes],
            protection_preferences=StrategyProtectionPreferences.from_payload(req.protection_config),
        )
        return {"strategy": preview.model_dump(mode="json")}
    except Exception as e:
        logger.error("Failed to preview option strategy: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/positions/realtime-summary")
async def get_realtime_positions_summary(
    request: Request,
    kite: KiteConnect = Depends(get_kite),
    corr_id: str = Depends(get_correlation_id),
):
    try:
        sid = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
        if not sid:
            raise HTTPException(401, "Session ID required")

        positions = await realtime_positions_service.get_positions(sid, corr_id)
        if not positions:
            logger.info("No cached positions, initializing from Kite API")
            positions = await realtime_positions_service.initialize_positions(kite, sid, corr_id)

        total_pnl = sum(pos.pnl for pos in positions.values())
        realized_pnl = sum(pos.realized_pnl for pos in positions.values())
        unrealized_pnl = sum(pos.unrealized_pnl for pos in positions.values())

        by_exchange: Dict[str, Dict[str, Any]] = {}
        by_product: Dict[str, Dict[str, Any]] = {}
        for pos in positions.values():
            if pos.exchange not in by_exchange:
                by_exchange[pos.exchange] = {"count": 0, "pnl": 0.0, "positions": []}
            by_exchange[pos.exchange]["count"] += 1
            by_exchange[pos.exchange]["pnl"] += pos.pnl
            by_exchange[pos.exchange]["positions"].append({"tradingsymbol": pos.tradingsymbol, "quantity": pos.quantity, "pnl": pos.pnl})

            if pos.product not in by_product:
                by_product[pos.product] = {"count": 0, "pnl": 0.0}
            by_product[pos.product]["count"] += 1
            by_product[pos.product]["pnl"] += pos.pnl

        return {
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_positions": len(positions),
                "total_pnl": round(total_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
            },
            "by_exchange": by_exchange,
            "by_product": by_product,
            "positions": {
                k: {
                    "tradingsymbol": v.tradingsymbol,
                    "exchange": v.exchange,
                    "product": v.product,
                    "quantity": v.quantity,
                    "multiplier": v.multiplier,
                    "average_price": round(v.average_price, 2),
                    "last_price": round(v.last_price, 2),
                    "pnl": round(v.pnl, 2),
                    "realized_pnl": round(v.realized_pnl, 2),
                    "unrealized_pnl": round(v.unrealized_pnl, 2),
                    "day_change_percentage": round(v.day_change_percentage, 2),
                }
                for k, v in positions.items()
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get realtime positions summary: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
