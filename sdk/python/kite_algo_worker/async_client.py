from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Dict, Iterable, Mapping, Optional

from .client import AlgoWorkerConfig, JsonDict
from .exceptions import error_for_status
from .models import (
    WorkerHistoricalCandles,
    WorkerOrderSnapshot,
    WorkerOrdersResponse,
    WorkerRunPnlSnapshot,
    WorkerTradesResponse,
)


@dataclass(frozen=True)
class AsyncKiteAlgoWorkerClient:
    config: AlgoWorkerConfig
    client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.config.base_url:
            raise ValueError("base_url is required")
        if not self.config.token:
            raise ValueError("token is required")
        httpx = import_module("httpx")
        object.__setattr__(self, "client", httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.config.timeout,
        ))

    async def __aenter__(self) -> "AsyncKiteAlgoWorkerClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> JsonDict:
        return await self._request("GET", "/worker/health")

    async def get_run(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}")

    async def get_run_pnl(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}/pnl")

    async def get_run_pnl_snapshot(self, strategy_run_id: str) -> WorkerRunPnlSnapshot:
        return WorkerRunPnlSnapshot.model_validate(await self.get_run_pnl(strategy_run_id))

    async def get_funds(self, *, mode: str = "paper", account_scope: Optional[str] = None) -> JsonDict:
        params: JsonDict = {"mode": mode}
        if account_scope is not None:
            params["account_scope"] = account_scope
        return await self._request("GET", "/worker/funds", params=params)

    async def get_run_funds(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}/funds")

    async def list_orders(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", "/worker/orders", params={"strategy_run_id": strategy_run_id})

    async def get_orders_snapshot(self, strategy_run_id: str) -> WorkerOrdersResponse:
        return WorkerOrdersResponse.model_validate(await self.list_orders(strategy_run_id))

    async def list_trades(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", "/worker/trades", params={"strategy_run_id": strategy_run_id})

    async def get_trades_snapshot(self, strategy_run_id: str) -> WorkerTradesResponse:
        return WorkerTradesResponse.model_validate(await self.list_trades(strategy_run_id))

    async def get_order_snapshot(self, strategy_run_id: str, order_id: str) -> WorkerOrderSnapshot:
        response = await self._request("GET", f"/worker/orders/{order_id}", params={"strategy_run_id": strategy_run_id})
        return WorkerOrderSnapshot.model_validate(response.get("order") or response)

    async def get_candles(self, instrument: str | int, interval: str = "5minute", lookback: int = 50) -> JsonDict:
        params: JsonDict = {"interval": interval, "lookback": lookback}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        return await self._request("GET", "/worker/market/candles", params=params)

    async def get_candles_snapshot(self, instrument: str | int, interval: str = "5minute", lookback: int = 50) -> WorkerHistoricalCandles:
        return WorkerHistoricalCandles.model_validate(await self.get_candles(instrument, interval=interval, lookback=lookback))

    async def get_historical_candles(
        self,
        instrument: str | int,
        timeframe: str = "day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        ingest: bool = True,
        passthrough: bool = False,
    ) -> JsonDict:
        params: JsonDict = {"timeframe": timeframe, "ingest": ingest, "passthrough": passthrough}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        if from_date is not None:
            params["from"] = from_date
        if to_date is not None:
            params["to"] = to_date
        return await self._request("GET", "/worker/market/history", params=params)

    async def get_historical_candles_snapshot(
        self,
        instrument: str | int,
        timeframe: str = "day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        ingest: bool = True,
        passthrough: bool = False,
    ) -> WorkerHistoricalCandles:
        return WorkerHistoricalCandles.model_validate(
            await self.get_historical_candles(
                instrument,
                timeframe=timeframe,
                from_date=from_date,
                to_date=to_date,
                ingest=ingest,
                passthrough=passthrough,
            )
        )

    async def resolve_ticker(self, symbol: str) -> JsonDict:
        return await self._request("GET", "/worker/market/instruments/resolve", params={"symbol": symbol})

    async def resolve_tickers(self, instruments: Iterable[str | int]) -> JsonDict:
        symbols: list[str] = []
        tokens: list[int] = []
        for instrument in instruments:
            value = str(instrument).strip()
            if isinstance(instrument, int) or value.isdigit():
                tokens.append(int(value))
            elif value:
                symbols.append(value)
        return await self._request(
            "POST",
            "/worker/market/instruments/resolve",
            json={"symbols": symbols, "instrument_tokens": tokens},
        )

    async def search_tickers(self, query: str, exchange: Optional[str] = None, limit: int = 20) -> JsonDict:
        params: JsonDict = {"query": query, "limit": limit}
        if exchange:
            params["exchange"] = exchange
        return await self._request("GET", "/worker/market/instruments/search", params=params)

    async def get_quotes(self, instruments: Iterable[str | int], mode: str = "quote") -> JsonDict:
        symbols: list[str] = []
        tokens: list[int] = []
        for instrument in instruments:
            value = str(instrument).strip()
            if isinstance(instrument, int) or value.isdigit():
                tokens.append(int(value))
            elif value:
                symbols.append(value)
        return await self._request(
            "POST",
            "/worker/market/quotes",
            json={"symbols": symbols, "instrument_tokens": tokens, "mode": mode},
        )

    async def get_market_snapshot(
        self,
        *,
        symbols: Optional[list[str]] = None,
        instrument_tokens: Optional[list[int]] = None,
        candles: Optional[list[Mapping[str, Any]]] = None,
        mode: str = "quote",
    ) -> JsonDict:
        return await self._request(
            "POST",
            "/worker/market/snapshot",
            json={
                "symbols": symbols or [],
                "instrument_tokens": instrument_tokens or [],
                "candles": list(candles or []),
                "mode": mode,
            },
        )

    async def preview_order(self, strategy_run_id: str, order: Mapping[str, Any], *, metadata: Optional[Mapping[str, Any]] = None) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/preview/order",
            json={"order": dict(order), "metadata": dict(metadata or {})},
        )

    async def preview_basket(
        self,
        strategy_run_id: str,
        orders: Iterable[Mapping[str, Any]],
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        all_or_none: bool = False,
    ) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/preview/basket",
            json={
                "orders": [dict(order) for order in orders],
                "metadata": dict(metadata or {}),
                "all_or_none": all_or_none,
            },
        )

    async def wait_for_terminal_order_state(
        self,
        strategy_run_id: str,
        order_id: str,
        *,
        attempts: int = 20,
        sleep_seconds: float = 1.0,
    ) -> WorkerOrderSnapshot:
        last_snapshot: Optional[WorkerOrderSnapshot] = None
        for _ in range(attempts):
            last_snapshot = await self.get_order_snapshot(strategy_run_id, order_id)
            if last_snapshot.status in {"COMPLETE", "CANCELLED", "REJECTED"}:
                return last_snapshot
            await asyncio.sleep(sleep_seconds)
        if last_snapshot is None:
            raise RuntimeError("wait_for_terminal_order_state exhausted without fetching an order snapshot")
        return last_snapshot

    def _url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        prefix = "/" + self.config.api_prefix.strip("/")
        suffix = "/" + path.strip("/")
        return f"{base}{prefix}{suffix}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        response = await self.client.request(method, self._url(path), **kwargs)
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        try:
            body: Any = response.json()
        except ValueError:
            body = {"raw": response.text}
        raise error_for_status(response.status_code, body, fallback=f"Worker API returned {response.status_code} for {method} {path}")


__all__ = ["AsyncKiteAlgoWorkerClient"]
