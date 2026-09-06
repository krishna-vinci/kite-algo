from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from ..contracts import DecisionRequest, ExitRunRequest, PageRequest, ProtectionRequest, RiskRequest, RunSelector
from ..server import DispatchError, MCPRuntime
from .common import args_model, register_tool


def _safety_ok(value: Any) -> tuple[bool, str | None]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if not isinstance(value, dict):
        return True, None
    allowed = value.get("allowed", value.get("safe", value.get("can_submit", True)))
    reason = value.get("reason") or value.get("rejection_reason")
    return bool(allowed), str(reason) if reason else None


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def check_run_safety(request: RunSelector) -> Any:
        values = args_model(request)
        return await runtime.invoke("check_run_safety", values, lambda _lease: runtime.client.safety_check(request.strategy_run_id))

    async def get_run_protection(request: RunSelector) -> Any:
        values = args_model(request)
        method = getattr(runtime.client, "get_run_protection_state", None)
        if method is not None:
            operation = lambda _lease: method(request.strategy_run_id)
        else:
            operation = lambda _lease: runtime.client._request("GET", f"/worker/runs/{request.strategy_run_id}/protection")
        return await runtime.invoke("get_run_protection", values, operation)

    async def get_run_pnl(request: RunSelector) -> Any:
        values = args_model(request)
        method = getattr(runtime.client, "get_run_pnl_snapshot", None)
        if method is None:
            method = getattr(runtime.client, "get_run_pnl", None)
        if method is None:
            raise RuntimeError("worker SDK does not expose run PnL")
        return await runtime.invoke("get_run_pnl", values, lambda _lease: method(request.strategy_run_id))

    async def list_run_timeline(request: PageRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("list_run_timeline", values, lambda _lease: runtime.client.list_timeline(request.strategy_run_id, limit=request.limit, after_cursor=request.after_cursor))

    async def list_execution_events(request: PageRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("list_execution_events", values, lambda _lease: runtime.client.list_execution_events(request.strategy_run_id, after_cursor=request.after_cursor, limit=request.limit))

    async def exit_run(request: ExitRunRequest) -> Any:
        values = args_model(request)
        async def operation(lease: Any) -> Any:
            return await lease.call(
                runtime.client.exit_run,
                request.strategy_run_id,
                reason=request.reason,
                idempotency_key=request.idempotency_key,
            )
        return await runtime.invoke("exit_run", values, operation, run_id=request.strategy_run_id)

    async def update_run_risk(request: RiskRequest) -> Any:
        values = args_model(request)
        async def operation(lease: Any) -> Any:
            return await lease.call(runtime.client.patch_risk, request.strategy_run_id, request.patch(), reason=request.reason)
        return await runtime.invoke("update_run_risk", values, operation, run_id=request.strategy_run_id)

    async def update_run_protection(request: ProtectionRequest) -> Any:
        values = args_model(request)
        from kite_algo_worker.protection import BackendProtection, BasketProtection

        async def operation(lease: Any) -> Any:
            protection = BackendProtection(
                enabled=request.enabled,
                mode=request.mode,
                basket=BasketProtection(
                    stoploss_pct=request.stoploss_pct,
                    target_pct=request.target_pct,
                    trailing_activate_pct=request.trailing_activate_pct,
                    trailing_drawdown_pct=request.trailing_drawdown_pct,
                ) if any(value is not None for value in (request.stoploss_pct, request.target_pct, request.trailing_activate_pct, request.trailing_drawdown_pct)) else None,
            )
            return await lease.call(
                runtime.client.update_backend_protection,
                request.strategy_run_id,
                protection,
                reason=request.reason,
            )
        return await runtime.invoke("update_run_protection", values, operation, run_id=request.strategy_run_id)

    async def log_run_decision(request: DecisionRequest) -> Any:
        values = args_model(request)
        async def operation(lease: Any) -> Any:
            # The SDK exposes this as keyword fields; only the reviewed fields
            # cross the MCP boundary.
            return await lease.call(
                runtime.client.log_decision_event,
                request.strategy_run_id,
                decision_type=request.decision_type,
                summary=request.summary,
            )
        return await runtime.invoke("log_run_decision", values, operation, run_id=request.strategy_run_id)

    for name, function in {
        "check_run_safety": check_run_safety,
        "get_run_protection": get_run_protection,
        "get_run_pnl": get_run_pnl,
        "list_run_timeline": list_run_timeline,
        "list_execution_events": list_execution_events,
        "exit_run": exit_run,
        "update_run_risk": update_run_risk,
        "update_run_protection": update_run_protection,
        "log_run_decision": log_run_decision,
    }.items():
        register_tool(server, runtime, name, function)
