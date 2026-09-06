from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from ..contracts import CalendarRequest, CandleRequest, HistoricalCandleRequest, IndexRequest, InstrumentRequest, SearchInstrumentsRequest, SymbolRequest
from ..server import MCPRuntime
from .common import args_model, register_tool


def _depth_view(response: Any) -> dict[str, Any]:
    """Expose only depth actually present in quote payloads."""

    if not isinstance(response, dict):
        return {"available": False, "reason": "worker returned no structured quote payload", "quotes": response}
    quotes = response.get("quotes") if isinstance(response.get("quotes"), (list, dict)) else response.get("data", response)
    items = list(quotes.values()) if isinstance(quotes, dict) else list(quotes or []) if isinstance(quotes, list) else []
    depth_items: list[dict[str, Any]] = []
    found = False
    for quote in items:
        if not isinstance(quote, dict):
            continue
        depth = quote.get("depth")
        if isinstance(depth, dict):
            buys = depth.get("buy") or depth.get("buys") or []
            sells = depth.get("sell") or depth.get("sells") or []
            depth_items.append({"symbol": quote.get("symbol") or quote.get("tradingsymbol"), "buy": buys, "sell": sells})
            found = True
        elif "buy" in quote or "sell" in quote:
            depth_items.append({"symbol": quote.get("symbol") or quote.get("tradingsymbol"), "buy": quote.get("buy") or [], "sell": quote.get("sell") or []})
            found = True
    if found:
        return {"available": True, "reason": None, "depth": depth_items}
    return {"available": False, "reason": "upstream quote did not provide market depth", "depth": []}


def register(server: FastMCP, runtime: MCPRuntime) -> None:
    async def search_instruments(request: SearchInstrumentsRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("search_instruments", values, lambda _lease: runtime.client.search_tickers(request.query, request.exchange, request.limit))

    async def resolve_instruments(request: SymbolRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("resolve_instruments", values, lambda _lease: runtime.client.resolve_tickers(request.symbols))

    async def get_quotes(request: SymbolRequest, mode: Literal["ltp", "quote", "full"] = "quote") -> Any:
        values = {**args_model(request), "mode": mode}
        return await runtime.invoke("get_quotes", values, lambda _lease: runtime.client.get_quotes(request.symbols, mode=mode))

    async def get_market_snapshot(request: SymbolRequest, mode: Literal["ltp", "quote", "full"] = "quote") -> Any:
        values = {**args_model(request), "mode": mode}
        return await runtime.invoke("get_market_snapshot", values, lambda _lease: runtime.client.get_market_snapshot(symbols=request.symbols, mode=mode))

    async def get_market_depth(request: SymbolRequest) -> Any:
        async def operation(_lease: Any) -> dict[str, Any]:
            response = await runtime.client.get_quotes(request.symbols, mode="full")
            return _depth_view(response)

        return await runtime.invoke("get_market_depth", args_model(request), operation)

    async def get_candles(request: CandleRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_candles", values, lambda _lease: runtime.client.get_candles(request.instrument, request.interval, request.lookback))

    async def get_current_candle(request: CandleRequest) -> Any:
        values = args_model(request)
        async def operation(_lease: Any) -> Any:
            response = await runtime.client.get_candles(request.instrument, request.interval, 1)
            if isinstance(response, dict) and isinstance(response.get("candles"), list):
                return {**response, "candles": response["candles"][-1:]}
            return response
        return await runtime.invoke("get_current_candle", values, operation)

    async def get_historical_candles(request: HistoricalCandleRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke(
            "get_historical_candles",
            values,
            lambda _lease: runtime.client.get_historical_candles(
                request.instrument,
                timeframe=request.timeframe,
                from_date=request.from_date,
                to_date=request.to_date,
                lookback_days=request.lookback_days,
                ingest=False,
                passthrough=False,
            ),
        )

    async def request_history(request: HistoricalCandleRequest) -> Any:
        values = {**args_model(request), "request_history": True}
        return await runtime.invoke(
            "request_history",
            values,
            lambda _lease: runtime.client.get_historical_candles(
                request.instrument,
                timeframe=request.timeframe,
                from_date=request.from_date,
                to_date=request.to_date,
                lookback_days=request.lookback_days,
                ingest=True,
                passthrough=False,
            ),
        )

    async def get_market_calendar(request: CalendarRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_market_calendar", values, lambda _lease: runtime.client.get_market_calendar(request.from_date, request.to_date, exchange=request.exchange, segment=request.segment))

    async def get_market_calendar_status(exchange: str = "NSE", segment: str = "CM") -> Any:
        values = {"exchange": exchange, "segment": segment}
        return await runtime.invoke("get_market_calendar_status", values, lambda _lease: runtime.client.get_market_calendar_status(exchange=exchange, segment=segment))

    async def get_index_constituents(request: IndexRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_index_constituents", values, lambda _lease: runtime.client.get_index_constituents(request.source_list))

    async def get_index_status(request: IndexRequest) -> Any:
        values = args_model(request)
        return await runtime.invoke("get_index_status", values, lambda _lease: runtime.client.get_index_constituent_status(request.source_list))

    names = {
        "search_instruments": search_instruments,
        "resolve_instruments": resolve_instruments,
        "get_quotes": get_quotes,
        "get_market_snapshot": get_market_snapshot,
        "get_market_depth": get_market_depth,
        "get_candles": get_candles,
        "get_current_candle": get_current_candle,
        "get_historical_candles": get_historical_candles,
        "request_history": request_history,
        "get_market_calendar": get_market_calendar,
        "get_market_calendar_status": get_market_calendar_status,
        "get_index_constituents": get_index_constituents,
        "get_index_status": get_index_status,
    }
    for name, function in names.items():
        register_tool(server, runtime, name, function)
