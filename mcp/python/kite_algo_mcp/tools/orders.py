from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from ..contracts import BasketRequest, BracketRequest, GttRequest, OrderActionRequest, OrderModifyRequest, PageRequest, PlaceOrderRequest, RunSelector
from ..server import DispatchError, MCPRuntime
from .common import args_model, register_tool


async def _ensure_entry_safety(runtime: MCPRuntime, run_id: str) -> None:
    safety = await runtime.client.safety_check(run_id)
    if hasattr(safety, "model_dump"):
        safety = safety.model_dump(mode="python")
    if isinstance(safety, dict):
        allowed = safety.get("allowed", safety.get("safe", safety.get("can_submit", True)))
        if not allowed:
            reason = safety.get("reason") or safety.get("rejection_reason") or "backend safety check refused entry"
            raise DispatchError("safety_refused", str(reason))


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def preview_order(request: PlaceOrderRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("preview_order", values, lambda _lease: runtime.client.preview_order(request.strategy_run_id, request.order.sdk_payload()))

    async def preview_basket(request: BasketRequest) -> Any:
        values = args_model(request)
        orders = [order.sdk_payload() for order in request.orders]
        return await runtime.invoke("preview_basket", values, lambda _lease: runtime.client.preview_basket(request.strategy_run_id, orders, all_or_none=request.all_or_none))

    async def place_order(request: PlaceOrderRequest) -> Any:
        values = args_model(request)
        async def operation(lease: Any) -> Any:
            await _ensure_entry_safety(runtime, request.strategy_run_id)
            return await lease.call(runtime.client.place_order, request.strategy_run_id, request.order.sdk_payload(), request.idempotency_key)
        return await runtime.invoke("place_order", values, operation, run_id=request.strategy_run_id)

    async def place_basket(request: BasketRequest) -> Any:
        values = args_model(request)
        async def operation(lease: Any) -> Any:
            await _ensure_entry_safety(runtime, request.strategy_run_id)
            orders = [order.sdk_payload() for order in request.orders]
            return await lease.call(runtime.client.place_basket, request.strategy_run_id, orders, request.idempotency_key, all_or_none=request.all_or_none)
        return await runtime.invoke("place_basket", values, operation, run_id=request.strategy_run_id)

    async def list_orders(request: RunSelector) -> Any:
        values = args_model(request)
        return await runtime.invoke("list_orders", values, lambda _lease: runtime.client.list_orders(request.strategy_run_id))

    async def list_trades(request: RunSelector) -> Any:
        values = args_model(request)
        return await runtime.invoke("list_trades", values, lambda _lease: runtime.client.list_trades(request.strategy_run_id))

    async def get_order(request: OrderActionRequest) -> Any:
        values = args_model(request)
        method = getattr(runtime.client, "get_order_snapshot", None)
        if method is None:
            method = getattr(runtime.client, "get_order", None)
        if method is None:
            raise RuntimeError("worker SDK does not expose order inspection")
        return await runtime.invoke("get_order", values, lambda _lease: method(request.strategy_run_id, request.order_id))

    async def get_order_history(request: OrderActionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_order_history", values, lambda _lease: runtime.client.get_order_history(request.strategy_run_id, request.order_id))

    async def modify_order(request: OrderModifyRequest) -> Any:
        values = args_model(request)
        patch = request.model_dump(exclude_none=True, exclude={"strategy_run_id", "order_id", "variety"}, mode="json")
        async def operation(lease: Any) -> Any:
            lease.ensure_alive()
            return await runtime.client.modify_order(request.strategy_run_id, request.order_id, patch, variety=request.variety)
        return await runtime.invoke("modify_order", values, operation, run_id=request.strategy_run_id)

    async def cancel_order(request: OrderActionRequest) -> Any:
        values = args_model(request)
        async def operation(lease: Any) -> Any:
            lease.ensure_alive()
            return await runtime.client.cancel_order(request.strategy_run_id, request.order_id, variety=request.variety)
        return await runtime.invoke("cancel_order", values, operation, run_id=request.strategy_run_id)

    async def list_baskets(request: RunSelector, limit: int = 100) -> Any:
        values = {**args_model(request), "limit": limit}
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return await runtime.invoke("list_baskets", values, lambda _lease: runtime.client.list_baskets(request.strategy_run_id, limit=limit))

    async def get_basket(strategy_run_id: str, basket_execution_id: str) -> Any:
        values = {"strategy_run_id": strategy_run_id, "basket_execution_id": basket_execution_id}
        return await runtime.invoke("get_basket", values, lambda _lease: runtime.client.get_basket(strategy_run_id, basket_execution_id), run_id=strategy_run_id)

    async def create_bracket(request: BracketRequest) -> Any:
        values = args_model(request)
        async def operation(lease: Any) -> Any:
            return await lease.call(
                runtime.client.create_bracket,
                request.strategy_run_id,
                entry_order=request.entry_order.sdk_payload(),
                stoploss=request.stoploss.sdk_payload(),
                target=request.target.sdk_payload() if request.target else None,
                idempotency_key=request.idempotency_key,
            )
        return await runtime.invoke("create_bracket", values, operation, run_id=request.strategy_run_id)

    async def list_brackets(request: RunSelector, limit: int = 50) -> Any:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        values = {**args_model(request), "limit": limit}
        return await runtime.invoke("list_brackets", values, lambda _lease: runtime.client.list_brackets(request.strategy_run_id, limit=limit))

    async def get_bracket(strategy_run_id: str, bracket_intent_id: str) -> Any:
        values = {"strategy_run_id": strategy_run_id, "bracket_intent_id": bracket_intent_id}
        return await runtime.invoke("get_bracket", values, lambda _lease: runtime.client.get_bracket(strategy_run_id, bracket_intent_id), run_id=strategy_run_id)

    async def cancel_bracket(strategy_run_id: str, bracket_intent_id: str) -> Any:
        values = {"strategy_run_id": strategy_run_id, "bracket_intent_id": bracket_intent_id}
        async def operation(lease: Any) -> Any:
            return await lease.call(runtime.client.cancel_bracket, strategy_run_id, bracket_intent_id)
        return await runtime.invoke("cancel_bracket", values, operation, run_id=strategy_run_id)

    async def create_gtt(request: GttRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("create_gtt", values, lambda _lease: runtime.client.place_gtt(request.sdk_payload()))

    async def list_gtts() -> Any:
        return await runtime.invoke("list_gtts", {}, lambda _lease: runtime.client.list_gtts())

    async def get_gtt(trigger_id: int) -> Any:
        if trigger_id < 1:
            raise ValueError("trigger_id must be positive")
        return await runtime.invoke("get_gtt", {"trigger_id": trigger_id}, lambda _lease: runtime.client.get_gtt(trigger_id))

    async def modify_gtt(trigger_id: int, request: GttRequest) -> Any:
        values = {"trigger_id": trigger_id, **args_model(request)}
        return await runtime.invoke("modify_gtt", values, lambda _lease: runtime.client.modify_gtt(trigger_id, request.sdk_payload()))

    async def delete_gtt(trigger_id: int) -> Any:
        if trigger_id < 1:
            raise ValueError("trigger_id must be positive")
        return await runtime.invoke("delete_gtt", {"trigger_id": trigger_id}, lambda _lease: runtime.client.delete_gtt(trigger_id))

    for name, function in {
        "preview_order": preview_order,
        "preview_basket": preview_basket,
        "place_order": place_order,
        "place_basket": place_basket,
        "list_orders": list_orders,
        "list_trades": list_trades,
        "get_order": get_order,
        "get_order_history": get_order_history,
        "modify_order": modify_order,
        "cancel_order": cancel_order,
        "list_baskets": list_baskets,
        "get_basket": get_basket,
        "create_bracket": create_bracket,
        "list_brackets": list_brackets,
        "get_bracket": get_bracket,
        "cancel_bracket": cancel_bracket,
        "create_gtt": create_gtt,
        "list_gtts": list_gtts,
        "get_gtt": get_gtt,
        "modify_gtt": modify_gtt,
        "delete_gtt": delete_gtt,
    }.items():
        register_tool(server, runtime, name, function)
