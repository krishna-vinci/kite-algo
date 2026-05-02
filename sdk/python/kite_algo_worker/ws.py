from __future__ import annotations

import json
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
import time
from typing import Any, AsyncIterator, Iterable, Optional
from urllib.parse import urlencode

from .exceptions import StreamDisconnectedError


def _join(values: Iterable[str | int]) -> str:
    return ",".join(str(value).strip() for value in values if str(value).strip())


def _now() -> float:
    return time.monotonic()


def _subscription_key(path: str, params: dict[str, Any]) -> str:
    query = urlencode({key: value for key, value in params.items() if key != "token" and value not in (None, "")})
    return f"{path}?{query}" if query else path


@dataclass
class StreamHealth:
    stream_name: str = ""
    subscription_key: str = ""
    connected: bool = False
    reconnect_count: int = 0
    last_message_at: Optional[float] = None
    last_reconnect_at: Optional[float] = None
    subscription_replayed: bool = False
    subscription_replayed_at: Optional[float] = None
    is_stale: bool = False
    last_error: Optional[str] = None
    next_reconnect_delay_seconds: float = 0.0

    def mark_connected(self, *, replayed: bool) -> None:
        self.connected = True
        self.is_stale = False
        self.last_error = None
        if replayed:
            self.reconnect_count += 1
            self.last_reconnect_at = _now()
            self.subscription_replayed = True
            self.subscription_replayed_at = self.last_reconnect_at

    def mark_message(self) -> None:
        self.last_message_at = _now()

    def mark_disconnect(self, error: Exception) -> None:
        self.connected = False
        self.is_stale = True
        self.last_error = str(error)

    def mark_retry_delay(self, delay_seconds: float) -> None:
        self.next_reconnect_delay_seconds = delay_seconds

    def mark_closed(self) -> None:
        self.connected = False

    def mark_reconnect_failed(self, error: Exception) -> None:
        self.connected = False
        self.is_stale = True
        self.last_error = str(error)


@dataclass
class WorkerWebSocketStream:
    manager: Any
    websocket: Any
    health: StreamHealth

    async def recv(self, *, ignore_heartbeats: bool = False) -> Any:
        while True:
            message = await self._recv_with_reconnect()
            payload = self._decode_message(message)
            self.health.mark_message()
            if ignore_heartbeats and isinstance(payload, dict) and payload.get("event") == "heartbeat":
                continue
            return payload

    async def _recv_with_reconnect(self) -> Any:
        try:
            return await self.websocket.recv()
        except Exception as exc:  # pragma: no cover - transport-specific safety
            self.health.mark_disconnect(exc)
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
        self.health.mark_closed()
        await self.manager.close(self.websocket)


@dataclass
class _WorkerWebSocketConnectionManager:
    connector: Any
    url: str
    reconnect_attempts: int
    reconnect_delay_seconds: float
    health: StreamHealth

    def _next_backoff(self, attempt: int) -> float:
        base = max(0.0, self.reconnect_delay_seconds)
        cap = max(base, 30.0)
        return min(cap, base * (2**attempt))

    async def connect(self, *, replayed: bool) -> Any:
        connection = self.connector(self.url)
        websocket = await connection.__aenter__()
        self.health.mark_connected(replayed=replayed)
        return _ManagedWebSocket(connection=connection, websocket=websocket)

    async def reconnect(self) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(max(0, self.reconnect_attempts)):
            delay_seconds = self._next_backoff(attempt)
            self.health.mark_retry_delay(delay_seconds)
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            try:
                return await self.connect(replayed=True)
            except Exception as exc:  # pragma: no cover - transport-specific safety
                last_error = exc
        if last_error is not None:
            self.health.mark_reconnect_failed(last_error)
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
    health: Optional[StreamHealth] = None

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
        path = "/worker/ws/market/ticks"
        health = StreamHealth(stream_name="ticks", subscription_key=_subscription_key(path, params))
        manager = _WorkerWebSocketConnectionManager(
            connector=ws.connect,
            url=self._ws_url(path, params),
            reconnect_attempts=self.reconnect_attempts,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
            health=health,
        )
        websocket = await manager.connect(replayed=False)
        stream = WorkerWebSocketStream(manager=manager, websocket=websocket, health=health)
        self.health = health
        try:
            yield stream
        finally:
            health.mark_closed()
            await manager.close(stream.websocket)

    @asynccontextmanager
    async def stream_candles(self, *, symbol: Optional[str] = None, instrument_token: Optional[str | int] = None, interval: str = "5minute") -> AsyncIterator[WorkerWebSocketStream]:
        ws = self._require_websockets()
        params = {
            "token": self.token,
            "symbol": symbol,
            "instrument_token": instrument_token,
            "interval": interval,
        }
        path = "/worker/ws/market/candles"
        health = StreamHealth(stream_name="candles", subscription_key=_subscription_key(path, params))
        manager = _WorkerWebSocketConnectionManager(
            connector=ws.connect,
            url=self._ws_url(path, params),
            reconnect_attempts=self.reconnect_attempts,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
            health=health,
        )
        websocket = await manager.connect(replayed=False)
        stream = WorkerWebSocketStream(manager=manager, websocket=websocket, health=health)
        self.health = health
        try:
            yield stream
        finally:
            health.mark_closed()
            await manager.close(stream.websocket)

    @asynccontextmanager
    async def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0) -> AsyncIterator[WorkerWebSocketStream]:
        ws = self._require_websockets()
        params = {"token": self.token, "interval_seconds": interval_seconds}
        path = f"/worker/ws/runs/{strategy_run_id}/pnl"
        health = StreamHealth(stream_name="run_pnl", subscription_key=_subscription_key(path, params))
        manager = _WorkerWebSocketConnectionManager(
            connector=ws.connect,
            url=self._ws_url(path, params),
            reconnect_attempts=self.reconnect_attempts,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
            health=health,
        )
        websocket = await manager.connect(replayed=False)
        stream = WorkerWebSocketStream(manager=manager, websocket=websocket, health=health)
        self.health = health
        try:
            yield stream
        finally:
            health.mark_closed()
            await manager.close(stream.websocket)


WorkerTickWebSocketClient = WorkerWebSocketClient
WorkerCandleWebSocketClient = WorkerWebSocketClient
WorkerRunPnlWebSocketClient = WorkerWebSocketClient


__all__ = [
    "WorkerCandleWebSocketClient",
    "StreamHealth",
    "WorkerRunPnlWebSocketClient",
    "WorkerTickWebSocketClient",
    "WorkerWebSocketClient",
    "WorkerWebSocketStream",
]
