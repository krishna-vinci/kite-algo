from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from ..server import MCPRuntime
from ..contracts import CreateRunRequest, RunListRequest, RunSelector
from .common import args_model, register_tool


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def get_capabilities() -> Any:
        async def operation(_lease: Any) -> dict[str, Any]:
            health = await runtime.client.health()
            body = dict(health or {}) if isinstance(health, dict) else {"status": str(health)}
            # Keep maintained, useful capability metadata stable even when a
            # backend version returns additional private fields.
            known = {
                key: body[key]
                for key in (
                    "status", "service", "version", "schema_version", "allowed_actions", "allowed_modes",
                    "allowed_templates", "account_scope", "account_scopes", "execution_modes", "freshness",
                    "supported_intervals", "indices", "order_types", "products", "validities",
                )
                if key in body
            }
            known.update({
                "supported_intervals": body.get("supported_intervals") or ["minute", "3minute", "5minute", "15minute", "30minute", "60minute", "day"],
                "index_universes": body.get("indices") or ["nifty50", "nifty500", "niftybank"],
                "indicator_names": [
                    "sma", "ema", "wma", "vwma", "supertrend", "rsi", "macd", "ppo", "dpo", "stochastic",
                    "cci", "williams_r", "linreg", "atr", "bbands", "keltner", "adx", "aroon", "sar", "obv",
                    "vwap", "mfi", "crossover", "crossunder", "highest", "lowest", "rising", "falling",
                ],
                "mcp_profile": runtime.config.profile,
                "data_refresh_enabled": runtime.config.allow_data_refresh,
            })
            return known

        return await runtime.invoke("get_capabilities", {}, operation)

    async def list_runs(request: RunListRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("list_runs", values, lambda _lease: runtime.client.list_runs(**values))

    async def get_run(request: RunSelector) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_run", values, lambda _lease: runtime.client.get_run(request.strategy_run_id), run_id=request.strategy_run_id)

    async def get_run_health(request: RunSelector) -> Any:
        values = args_model(request)
        method = getattr(runtime.client, "get_run_health_snapshot", None)
        if method is None:
            method = getattr(runtime.client, "get_run", None)
        if method is None:
            raise RuntimeError("worker SDK does not expose run health")
        return await runtime.invoke("get_run_health", values, lambda _lease: method(request.strategy_run_id), run_id=request.strategy_run_id)

    async def get_funds(mode: Literal["paper", "live", "dry_run"] = "paper", account_scope: str | None = None) -> Any:
        values = {"mode": mode, "account_scope": account_scope}
        return await runtime.invoke("get_funds", values, lambda _lease: runtime.client.get_funds(mode=mode, account_scope=account_scope))

    async def get_run_funds(request: RunSelector) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_run_funds", values, lambda _lease: runtime.client.get_run_funds(request.strategy_run_id), run_id=request.strategy_run_id)

    async def get_account_portfolio(account_scope: str | None = None) -> Any:
        values = {"account_scope": account_scope}
        return await runtime.invoke("get_account_portfolio", values, lambda _lease: runtime.client.get_account_portfolio(account_scope=account_scope))

    async def create_run(request: CreateRunRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke(
            "create_run",
            values,
            lambda _lease: runtime.client.create_run(
                template_id=request.template_id,
                account_scope=request.account_scope,
                execution_mode=request.execution_mode,
                strategy_run_id=request.strategy_run_id,
            ),
        )

    register_tool(server, runtime, "get_capabilities", get_capabilities)
    register_tool(server, runtime, "list_runs", list_runs)
    register_tool(server, runtime, "get_run", get_run)
    register_tool(server, runtime, "get_run_health", get_run_health)
    register_tool(server, runtime, "get_funds", get_funds)
    register_tool(server, runtime, "get_run_funds", get_run_funds)
    register_tool(server, runtime, "get_account_portfolio", get_account_portfolio)
    register_tool(server, runtime, "create_run", create_run)
