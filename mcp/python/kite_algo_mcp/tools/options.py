from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from ..contracts import OptionActionRequest, OptionMetricSnapshot, OptionReplayRequest, OptionRequest, OptionRunRequest
from ..server import MCPRuntime
from .common import args_model, register_tool


def _options(runtime: MCPRuntime) -> Any:
    value = getattr(runtime.client, "options", None)
    if value is None:
        raise RuntimeError("worker SDK options namespace is unavailable")
    return value


def _selection_payload(request: OptionRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if request.expiry:
        payload["expiry"] = request.expiry
    if request.selector:
        payload["legs"] = [request.selector.sdk_selection()]
    return payload


async def _resolved_legs(runtime: MCPRuntime, request: OptionRunRequest) -> list[dict[str, Any]]:
    response = await _options(runtime).resolve_contracts(
        request.underlying,
        {"expiry": request.expiry, "legs": [selector.sdk_selection() for selector in request.legs]},
    )
    contracts = response.get("resolved") if isinstance(response, dict) else None
    contracts = contracts if contracts is not None else response.get("contracts", []) if isinstance(response, dict) else []
    if len(contracts) < len(request.legs):
        raise ValueError("option resolver returned fewer contracts than requested legs")
    legs: list[dict[str, Any]] = []
    for selector, contract in zip(request.legs, contracts):
        lot_size = int(contract.get("lot_size") or 0)
        if lot_size <= 0:
            raise ValueError("resolved option contract has no valid lot_size")
        legs.append({
            "tradingsymbol": contract.get("tradingsymbol"),
            "transaction_type": request.transaction_type,
            "quantity": lot_size * request.quantity_lots,
            "exchange": contract.get("exchange", "NFO"),
            "product": request.product,
            "instrument_token": contract.get("instrument_token"),
            "strike": contract.get("strike"),
            "option_type": contract.get("option_type") or selector.option_type,
            "expiry_key": contract.get("expiry_key") or request.expiry,
            "ltp": contract.get("ltp"),
            "lot_size": lot_size,
            "lots": request.quantity_lots,
            "order_type": "MARKET",
        })
    return legs


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def list_option_expiries(request: OptionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("list_option_expiries", values, lambda _lease: _options(runtime).list_expiries(request.underlying))

    async def get_option_chain(request: OptionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_option_chain", values, lambda _lease: _options(runtime).get_chain(request.underlying, expiry=request.expiry))

    async def get_option_mini_chain(request: OptionRequest, window: int = 5) -> Any:
        if not 1 <= window <= 20:
            raise ValueError("window must be between 1 and 20")
        values = {**args_model(request), "window": window}
        return await runtime.invoke("get_option_mini_chain", values, lambda _lease: _options(runtime).get_mini_chain(request.underlying, expiry=request.expiry, window=window))

    async def get_option_greeks(request: OptionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_option_greeks", values, lambda _lease: _options(runtime).get_greeks(request.underlying, expiry=request.expiry))

    async def get_option_pcr(request: OptionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_option_pcr", values, lambda _lease: _options(runtime).get_pcr(request.underlying, expiry=request.expiry))

    async def get_option_max_pain(request: OptionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_option_max_pain", values, lambda _lease: _options(runtime).get_max_pain(request.underlying, expiry=request.expiry))

    async def resolve_option_contracts(request: OptionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("resolve_option_contracts", values, lambda _lease: _options(runtime).resolve_contracts(request.underlying, _selection_payload(request)))

    async def preview_option_strategy(request: OptionRunRequest) -> Any:
        values = args_model(request)
        payload = {"strategy_name": request.strategy_name, "product": request.product, "underlying": request.underlying, "expiry": request.expiry, "legs": [selector.sdk_selection() for selector in request.legs]}
        return await runtime.invoke("preview_option_strategy", values, lambda _lease: _options(runtime).preview_strategy(payload))

    async def preview_option_entry(request: OptionActionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("preview_option_entry", values, lambda _lease: _options(runtime).preview_run_entry(request.strategy_run_id, {}), run_id=request.strategy_run_id)

    async def preview_option_exit(request: OptionActionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("preview_option_exit", values, lambda _lease: _options(runtime).preview_exit(request.strategy_run_id, {}), run_id=request.strategy_run_id)

    async def create_option_run(request: OptionRunRequest) -> Any:
        values = args_model(request)
        async def operation(_lease: Any) -> Any:
            legs = await _resolved_legs(runtime, request)
            return await _options(runtime).create_run(strategy_name=request.strategy_name, product=request.product, legs=legs)
        return await runtime.invoke("create_option_run", values, operation)

    async def enter_option_run(request: OptionActionRequest) -> Any:
        values = args_model(request)
        payload = {key: value for key, value in {"execution_mode": request.execution_mode, "account_scope": request.account_scope, "idempotency_key": request.idempotency_key, "all_or_none": request.all_or_none}.items() if value is not None}
        async def operation(lease: Any) -> Any:
            return await lease.call(_options(runtime).enter, request.strategy_run_id, payload)
        return await runtime.invoke("enter_option_run", values, operation, run_id=request.strategy_run_id)

    async def exit_option_run(request: OptionActionRequest) -> Any:
        values = args_model(request)
        payload = {key: value for key, value in {"execution_mode": request.execution_mode, "account_scope": request.account_scope, "idempotency_key": request.idempotency_key, "all_or_none": request.all_or_none}.items() if value is not None}
        async def operation(lease: Any) -> Any:
            return await lease.call(_options(runtime).exit, request.strategy_run_id, payload)
        return await runtime.invoke("exit_option_run", values, operation, run_id=request.strategy_run_id)

    async def get_option_run_state(request: OptionActionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_option_run_state", values, lambda _lease: _options(runtime).get_run_state(request.strategy_run_id), run_id=request.strategy_run_id)

    async def update_option_protection(request: OptionActionRequest, stoploss_pct: float | None = None, target_pct: float | None = None) -> Any:
        if stoploss_pct is None and target_pct is None:
            raise ValueError("stoploss_pct or target_pct is required")
        values = {**args_model(request), "stoploss_pct": stoploss_pct, "target_pct": target_pct}
        protection = {key: value for key, value in {"stoploss_pct": stoploss_pct, "target_pct": target_pct}.items() if value is not None}
        async def operation(lease: Any) -> Any:
            return await lease.call(_options(runtime).update_protection, request.strategy_run_id, protection)
        return await runtime.invoke("update_option_protection", values, operation, run_id=request.strategy_run_id)

    async def get_option_protection(request: OptionActionRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_option_protection", values, lambda _lease: _options(runtime).get_protection_state(request.strategy_run_id), run_id=request.strategy_run_id)

    async def replay_option_protection(request: OptionReplayRequest) -> Any:
        values = args_model(request)
        snapshots = [item.model_dump(exclude_none=True, mode="json") for item in request.metric_snapshots]
        return await runtime.invoke("replay_option_protection", values, lambda _lease: _options(runtime).replay_protection(request.strategy_run_id, snapshots), run_id=request.strategy_run_id)

    for name, function in {
        "list_option_expiries": list_option_expiries,
        "get_option_chain": get_option_chain,
        "get_option_mini_chain": get_option_mini_chain,
        "get_option_greeks": get_option_greeks,
        "get_option_pcr": get_option_pcr,
        "get_option_max_pain": get_option_max_pain,
        "resolve_option_contracts": resolve_option_contracts,
        "preview_option_strategy": preview_option_strategy,
        "preview_option_entry": preview_option_entry,
        "preview_option_exit": preview_option_exit,
        "create_option_run": create_option_run,
        "enter_option_run": enter_option_run,
        "exit_option_run": exit_option_run,
        "get_option_run_state": get_option_run_state,
        "update_option_protection": update_option_protection,
        "get_option_protection": get_option_protection,
        "replay_option_protection": replay_option_protection,
    }.items():
        register_tool(server, runtime, name, function)
