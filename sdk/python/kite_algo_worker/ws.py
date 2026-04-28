from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from typing import Any, AsyncIterator, Iterable, Optional
from urllib.parse import urlencode

from .exceptions import StreamDisconnectedError


def _join(values: Iterable[str | int]) -> str:
    return ",".join(str(value).strip() for value in values if str(value).strip())


@dataclass
class WorkerWebSocketStream:
    websocket: Any

    async def recv(self) -> Any:
        try:
            message = await self.websocket.recv()
        except Exception as exc:  # pragma: no cover - transport-specific safety
            raise StreamDisconnectedError("websocket stream disconnected", status_code=0) from exc
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        if isinstance(message, str):
            try:
                return json.loads(message)
            except json.JSONDecodeError as exc:
                raise StreamDisconnectedError("websocket stream returned invalid JSON", status_code=0, response_body=message) from exc
        return message

    async def close(self) -> None:
        await self.websocket.close()


@dataclass
class WorkerWebSocketClient:
    base_url: str
    token: str

    def _ws_url(self, path: str, params: dict[str, Any]) -> str:
        base = self.base_url.rstrip("/")
        query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
        return f"{base}/api/algo-workers{path}?{query}"

    def _require_websockets(self):
        try:
            return import_module("websockets")
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("websockets dependency is required for WorkerWebSocketClient") from exc

    @asynccontextmanager
    async def stream(self, *, symbols: Iterable[str | int], mode: str = "quote", instrument_tokens: Optional[Iterable[str | int]] = None) -> AsyncIterator[WorkerWebSocketStream]:
        async with self.stream_ticks(symbols=symbols, mode=mode, instrument_tokens=instrument_tokens) as stream:
            yield stream

    @asynccontextmanager
    async def stream_ticks(self, *, symbols: Iterable[str | int], mode: str = "quote", instrument_tokens: Optional[Iterable[str | int]] = None) -> AsyncIterator[WorkerWebSocketStream]:
        ws = self._require_websockets()
        params = {
            "token": self.token,
            "symbols": _join(symbols),
            "tokens": _join(instrument_tokens or []),
            "mode": mode,
        }
        async with ws.connect(self._ws_url("/worker/ws/market/ticks", params)) as websocket:
            yield WorkerWebSocketStream(websocket)

    @asynccontextmanager
    async def stream_candles(self, *, symbol: Optional[str] = None, instrument_token: Optional[str | int] = None, interval: str = "5minute") -> AsyncIterator[WorkerWebSocketStream]:
        ws = self._require_websockets()
        params = {
            "token": self.token,
            "symbol": symbol,
            "instrument_token": instrument_token,
            "interval": interval,
        }
        async with ws.connect(self._ws_url("/worker/ws/market/candles", params)) as websocket:
            yield WorkerWebSocketStream(websocket)

    @asynccontextmanager
    async def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0) -> AsyncIterator[WorkerWebSocketStream]:
        ws = self._require_websockets()
        params = {"token": self.token, "interval_seconds": interval_seconds}
        async with ws.connect(self._ws_url(f"/worker/ws/runs/{strategy_run_id}/pnl", params)) as websocket:
            yield WorkerWebSocketStream(websocket)


WorkerTickWebSocketClient = WorkerWebSocketClient
WorkerCandleWebSocketClient = WorkerWebSocketClient
WorkerRunPnlWebSocketClient = WorkerWebSocketClient


__all__ = [
    "WorkerCandleWebSocketClient",
    "WorkerRunPnlWebSocketClient",
    "WorkerTickWebSocketClient",
    "WorkerWebSocketClient",
    "WorkerWebSocketStream",
]
