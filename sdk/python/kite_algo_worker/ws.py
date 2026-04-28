from __future__ import annotations

import json
import asyncio
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
    manager: Any
    websocket: Any

    async def recv(self, *, ignore_heartbeats: bool = False) -> Any:
        while True:
            message = await self._recv_with_reconnect()
            payload = self._decode_message(message)
            if ignore_heartbeats and isinstance(payload, dict) and payload.get("event") == "heartbeat":
                continue
            return payload

    async def _recv_with_reconnect(self) -> Any:
        try:
            return await self.websocket.recv()
        except Exception as exc:  # pragma: no cover - transport-specific safety
            try:
                await self.manager.close(self.websocket)
            except Exception:
                pass
            try:
                self.websocket = await self.manager.reconnect()
            except Exception:
                raise StreamDisconnectedError("websocket stream disconnected", status_code=0) from exc
            return await self.websocket.recv()

    @staticmethod
    def _decode_message(message: Any) -> Any:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        if isinstance(message, str):
            try:
                return json.loads(message)
            except json.JSONDecodeError as exc:
                raise StreamDisconnectedError("websocket stream returned invalid JSON", status_code=0, response_body=message) from exc
        return message

    async def close(self) -> None:
        await self.manager.close(self.websocket)


@dataclass
class _WorkerWebSocketConnectionManager:
    connector: Any
    url: str
    reconnect_attempts: int
    reconnect_delay_seconds: float

    async def connect(self) -> Any:
        connection = self.connector(self.url)
        websocket = await connection.__aenter__()
        return _ManagedWebSocket(connection=connection, websocket=websocket)

    async def reconnect(self) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(max(0, self.reconnect_attempts)):
            if attempt > 0 or self.reconnect_delay_seconds > 0:
                await asyncio.sleep(self.reconnect_delay_seconds)
            try:
                return await self.connect()
            except Exception as exc:  # pragma: no cover - transport-specific safety
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("websocket reconnect failed")

    async def close(self, managed_websocket: Any) -> None:
        await managed_websocket.connection.__aexit__(None, None, None)


@dataclass
class _ManagedWebSocket:
    connection: Any
    websocket: Any

    async def recv(self) -> Any:
        return await self.websocket.recv()


@dataclass
class WorkerWebSocketClient:
    base_url: str
    token: str
    reconnect_attempts: int = 0
    reconnect_delay_seconds: float = 1.0

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
        manager = _WorkerWebSocketConnectionManager(
            connector=ws.connect,
            url=self._ws_url("/worker/ws/market/ticks", params),
            reconnect_attempts=self.reconnect_attempts,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
        )
        websocket = await manager.connect()
        try:
            yield WorkerWebSocketStream(manager=manager, websocket=websocket)
        finally:
            await manager.close(websocket)

    @asynccontextmanager
    async def stream_candles(self, *, symbol: Optional[str] = None, instrument_token: Optional[str | int] = None, interval: str = "5minute") -> AsyncIterator[WorkerWebSocketStream]:
        ws = self._require_websockets()
        params = {
            "token": self.token,
            "symbol": symbol,
            "instrument_token": instrument_token,
            "interval": interval,
        }
        manager = _WorkerWebSocketConnectionManager(
            connector=ws.connect,
            url=self._ws_url("/worker/ws/market/candles", params),
            reconnect_attempts=self.reconnect_attempts,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
        )
        websocket = await manager.connect()
        try:
            yield WorkerWebSocketStream(manager=manager, websocket=websocket)
        finally:
            await manager.close(websocket)

    @asynccontextmanager
    async def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0) -> AsyncIterator[WorkerWebSocketStream]:
        ws = self._require_websockets()
        params = {"token": self.token, "interval_seconds": interval_seconds}
        manager = _WorkerWebSocketConnectionManager(
            connector=ws.connect,
            url=self._ws_url(f"/worker/ws/runs/{strategy_run_id}/pnl", params),
            reconnect_attempts=self.reconnect_attempts,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
        )
        websocket = await manager.connect()
        try:
            yield WorkerWebSocketStream(manager=manager, websocket=websocket)
        finally:
            await manager.close(websocket)


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
