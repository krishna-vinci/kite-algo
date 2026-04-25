import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from broker_api.order_runtime import order_event_runtime
from broker_api.redis_events import get_redis, publish_event


logger = logging.getLogger(__name__)

RUNTIME_TICKS_CHANNEL = "market:ticks"
RUNTIME_STATUS_EVENTS_CHANNEL = "market:status:events"
RUNTIME_ORDER_UPDATES_CHANNEL = "market:order_updates"


def market_runtime_enabled() -> bool:
    value = os.getenv("MARKET_RUNTIME_ENABLED", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def market_runtime_http_url() -> str:
    return os.getenv("MARKET_RUNTIME_HTTP_URL", "http://localhost:8780").rstrip("/")


def _normalize_mode(value: str | None, default: str = "quote") -> str:
    mode = str(value or default).strip().lower()
    if mode in {"ltp", "quote", "full"}:
        return mode
    return default


def _parse_iso_datetime(value: Any) -> Any:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return value
    return value


def _format_ws_timestamp(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


class MarketRuntimeClient:
    def __init__(self, base_url: str, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_seconds),
        )

    async def get_status(self) -> Dict[str, Any]:
        return await self._request_json("GET", "/internal/market-runtime/status")

    async def set_owner_subscriptions(self, owner_id: str, subscriptions: Dict[int, str]) -> Dict[str, Any]:
        payload = {"tokens": {str(int(token)): _normalize_mode(mode) for token, mode in subscriptions.items()}}
        return await self._request_json(
            "PUT",
            f"/internal/market-runtime/subscriptions/{quote(owner_id, safe='')}",
            json=payload,
        )

    async def get_owner_subscriptions(self, owner_id: str) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/internal/market-runtime/subscriptions/{quote(owner_id, safe='')}",
        )

    async def delete_owner(self, owner_id: str) -> Dict[str, Any]:
        return await self._request_json(
            "DELETE",
            f"/internal/market-runtime/subscriptions/{quote(owner_id, safe='')}",
        )

    async def _request_json(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


class MarketDataRuntime:
    def __init__(self, *, realtime_positions_service=None, tick_flush_ms: int = 100):
        self.redis = get_redis()
        self.realtime_positions_service = realtime_positions_service
        self.tick_flush_ms = max(20, int(tick_flush_ms))

        self.latest_ticks: Dict[int, Dict[str, Any]] = {}
        self.runtime_status: Dict[str, Any] = {"status": "unknown"}
        self.last_order_update_at: Optional[datetime] = None
        self.order_updates_enabled: bool = True

        self._owners: Dict[str, Dict[int, str]] = {}
        self._owners_lock = asyncio.Lock()
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False
        self._pending_position_ticks: Dict[int, Dict[str, Any]] = {}
        self._tick_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            self.runtime_status = await (await get_market_runtime_client()).get_status()
        except Exception as exc:
            logger.warning("Failed to fetch initial market runtime status: %s", exc, exc_info=True)
            self.runtime_status = {"status": "degraded", "detail": str(exc)}

        self._tasks["ticks"] = asyncio.create_task(self._ticks_loop())
        self._tasks["status"] = asyncio.create_task(self._status_loop())
        self._tasks["orders"] = asyncio.create_task(self._order_updates_loop())
        self._tasks["lease"] = asyncio.create_task(self._owner_lease_loop())
        self._tasks["positions"] = asyncio.create_task(self._positions_tick_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

        owners = await self._copy_owner_subscriptions()
        if owners:
            client = await get_market_runtime_client()
            for owner_id in list(owners.keys()):
                try:
                    await client.delete_owner(owner_id)
                except Exception:
                    logger.warning("Failed to delete runtime owner %s during shutdown", owner_id, exc_info=True)
        async with self._owners_lock:
            self._owners.clear()

    def get_websocket_status(self) -> str:
        status = str(self.runtime_status.get("status") or "unknown").lower()
        mapping = {
            "healthy": "CONNECTED",
            "connected": "CONNECTED",
            "degraded": "DEGRADED",
            "reconnecting": "RECONNECTING",
            "waiting_for_token": "WAITING_FOR_TOKEN",
            "exhausted": "EXHAUSTED",
            "unknown": "UNKNOWN",
        }
        return mapping.get(status, status.upper())

    async def refresh_status(self) -> Dict[str, Any]:
        self.runtime_status = await (await get_market_runtime_client()).get_status()
        return self.runtime_status

    async def set_owner_subscriptions(self, owner_id: str, subscriptions: Dict[int, str]) -> Dict[str, Any]:
        normalized = {int(token): _normalize_mode(mode) for token, mode in subscriptions.items()}
        client = await get_market_runtime_client()
        response = await client.set_owner_subscriptions(owner_id, normalized)
        async with self._owners_lock:
            self._owners[owner_id] = dict(normalized)
        await self.prime_tick_cache(normalized.keys())
        return response

    async def delete_owner(self, owner_id: str) -> Dict[str, Any]:
        client = await get_market_runtime_client()
        response = await client.delete_owner(owner_id)
        async with self._owners_lock:
            self._owners.pop(owner_id, None)
        return response

    async def prime_tick_cache(self, tokens) -> None:
        token_list = sorted({int(token) for token in tokens if token is not None})
        missing = [token for token in token_list if token not in self.latest_ticks]
        if not missing:
            return
        try:
            raw_values = await self.redis.mget([f"market:tick:{token}" for token in missing])
        except RedisConnectionError:
            logger.warning("Redis unavailable while priming market runtime tick cache")
            return
        for token, raw_value in zip(missing, raw_values):
            if not raw_value:
                continue
            try:
                payload = json.loads(raw_value)
            except Exception:
                continue
            tick = self._normalize_tick_payload(payload)
            if tick:
                self.latest_ticks[token] = tick

    async def get_tick(self, token: int) -> Optional[Dict[str, Any]]:
        tick = self.latest_ticks.get(int(token))
        if tick is not None:
            return tick
        await self.prime_tick_cache([token])
        return self.latest_ticks.get(int(token))

    async def get_last_price(self, token: int) -> Optional[float]:
        tick = await self.get_tick(token)
        if isinstance(tick, dict) and tick.get("last_price") is not None:
            return float(tick["last_price"])
        return None

    async def _copy_owner_subscriptions(self) -> Dict[str, Dict[int, str]]:
        async with self._owners_lock:
            return {owner_id: dict(subscriptions) for owner_id, subscriptions in self._owners.items()}

    async def _owner_lease_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(25)
                owners = await self._copy_owner_subscriptions()
                if not owners:
                    continue
                client = await get_market_runtime_client()
                for owner_id, subscriptions in owners.items():
                    try:
                        await client.set_owner_subscriptions(owner_id, subscriptions)
                    except Exception:
                        logger.warning("Failed to refresh market runtime owner %s", owner_id, exc_info=True)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Market runtime lease loop failed", exc_info=True)

    async def _ticks_loop(self) -> None:
        await self._pubsub_loop(
            channels=[RUNTIME_TICKS_CHANNEL],
            on_message=self._handle_tick_message,
            loop_name="ticks",
        )

    async def _status_loop(self) -> None:
        await self._pubsub_loop(
            channels=[RUNTIME_STATUS_EVENTS_CHANNEL],
            on_message=self._handle_status_message,
            loop_name="status",
        )

    async def _order_updates_loop(self) -> None:
        await self._pubsub_loop(
            channels=[RUNTIME_ORDER_UPDATES_CHANNEL],
            on_message=self._handle_order_update_message,
            loop_name="order updates",
        )

    async def _pubsub_loop(self, *, channels: list[str], on_message, loop_name: str) -> None:
        pubsub = None
        retry_delay = 1.0
        try:
            while self._running:
                try:
                    if pubsub is None:
                        pubsub = self.redis.pubsub()
                        await pubsub.subscribe(*channels)
                        retry_delay = 1.0
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    raw_payload = message.get("data")
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    await on_message(payload)
                except RedisConnectionError:
                    logger.warning("Market runtime %s loop lost Redis pubsub; retrying in %.1fs", loop_name, retry_delay)
                    if pubsub is not None:
                        try:
                            await pubsub.aclose()
                        except Exception:
                            pass
                        pubsub = None
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 10.0)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.error("Market runtime %s loop failed", loop_name, exc_info=True)
                    await asyncio.sleep(1)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(*channels)
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

    async def _handle_tick_message(self, payload: Dict[str, Any]) -> None:
        tick = self._normalize_tick_payload(payload)
        if not tick:
            return
        token = int(tick["instrument_token"])
        self.latest_ticks[token] = tick
        async with self._tick_lock:
            self._pending_position_ticks[token] = tick

    async def _positions_tick_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.tick_flush_ms / 1000.0)
                if self.realtime_positions_service is None:
                    continue
                async with self._tick_lock:
                    if not self._pending_position_ticks:
                        continue
                    batch = list(self._pending_position_ticks.values())
                    self._pending_position_ticks.clear()
                await self.realtime_positions_service.process_ticks(batch, corr_id="market_runtime_tick")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Market runtime positions tick loop failed", exc_info=True)

    async def _handle_status_message(self, payload: Dict[str, Any]) -> None:
        if isinstance(payload, dict):
            self.runtime_status = payload

    async def _handle_order_update_message(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        self.last_order_update_at = datetime.now(timezone.utc)
        if not self.order_updates_enabled:
            return

        order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        payload_dict = self._normalize_order_update_payload(order)
        if payload_dict is None:
            return
        try:
            ingest_result = await order_event_runtime.ingest_ws_event(payload_dict, corr_id="market_runtime_order_update")
            if ingest_result.get("duplicate"):
                return
            event_timestamp = payload_dict.get("exchange_update_timestamp") or payload_dict.get("order_timestamp")
            await publish_event(
                "orders.events",
                {
                    "source": "ws",
                    "id": ingest_result.get("canonical_event_id"),
                    "order_id": payload_dict.get("order_id"),
                    "user_id": payload_dict.get("user_id"),
                    "status": payload_dict.get("status"),
                    "event_timestamp": event_timestamp,
                    "exchange": payload_dict.get("exchange"),
                    "tradingsymbol": payload_dict.get("tradingsymbol"),
                    "instrument_token": payload_dict.get("instrument_token"),
                    "transaction_type": payload_dict.get("transaction_type"),
                    "quantity": payload_dict.get("quantity"),
                    "filled_quantity": payload_dict.get("filled_quantity"),
                    "average_price": payload_dict.get("average_price"),
                    "payload": payload_dict,
                },
            )
        except Exception:
            logger.error("Failed to ingest market runtime order update", exc_info=True)

    def _normalize_tick_payload(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return None
        token = payload.get("instrument_token")
        if token is None:
            return None
        try:
            normalized = dict(payload)
            normalized["instrument_token"] = int(token)
            if normalized.get("last_price") is not None:
                normalized["last_price"] = float(normalized["last_price"])
            for field in ("exchange_timestamp", "last_trade_time", "received_at"):
                normalized[field] = _parse_iso_datetime(normalized.get(field))
            return normalized
        except Exception:
            logger.debug("Failed to normalize market runtime tick payload", exc_info=True)
            return None

    def _normalize_order_update_payload(self, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(order, dict):
            return None
        if not order.get("order_id"):
            return None
        try:
            return {
                "user_id": order.get("user_id") or order.get("placed_by") or "unknown",
                "app_id": int(order.get("app_id") or 0),
                "checksum": "",
                "source": "websocket",
                "placed_by": order.get("placed_by") or order.get("user_id") or "unknown",
                "order_id": order.get("order_id"),
                "exchange_order_id": order.get("exchange_order_id"),
                "parent_order_id": order.get("parent_order_id"),
                "status": order.get("status") or "UPDATE",
                "status_message": order.get("status_message"),
                "status_message_raw": order.get("status_message_raw"),
                "order_timestamp": _format_ws_timestamp(order.get("order_timestamp") or order.get("exchange_timestamp") or datetime.now(timezone.utc)),
                "exchange_update_timestamp": _format_ws_timestamp(order.get("exchange_update_timestamp")) if order.get("exchange_update_timestamp") else None,
                "exchange_timestamp": _format_ws_timestamp(order.get("exchange_timestamp")) if order.get("exchange_timestamp") else None,
                "variety": order.get("variety") or "regular",
                "exchange": order.get("exchange"),
                "tradingsymbol": order.get("tradingsymbol"),
                "instrument_token": int(order.get("instrument_token") or 0),
                "order_type": order.get("order_type") or "MARKET",
                "transaction_type": order.get("transaction_type"),
                "validity": order.get("validity") or "DAY",
                "validity_ttl": order.get("validity_ttl"),
                "product": order.get("product"),
                "quantity": int(order.get("quantity") or order.get("filled_quantity") or 0),
                "disclosed_quantity": int(order.get("disclosed_quantity") or 0),
                "price": float(order.get("price") or 0.0),
                "trigger_price": float(order.get("trigger_price") or 0.0),
                "average_price": float(order.get("average_price") or 0.0),
                "filled_quantity": int(order.get("filled_quantity") or 0),
                "pending_quantity": int(order.get("pending_quantity") or 0),
                "cancelled_quantity": int(order.get("cancelled_quantity") or 0),
                "unfilled_quantity": int(order.get("unfilled_quantity") or 0),
                "market_protection": int(order.get("market_protection") or 0),
                "meta": order.get("meta") or {},
                "tag": order.get("tag"),
                "tags": order.get("tags"),
                "guid": order.get("guid"),
            }
        except Exception:
            logger.error("Failed to normalize market runtime order update payload", exc_info=True)
            return None


_client_lock = asyncio.Lock()
_client: MarketRuntimeClient | None = None


async def get_market_runtime_client() -> MarketRuntimeClient:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            _client = MarketRuntimeClient(market_runtime_http_url())
            logger.info("Initialized market runtime client for %s", _client.base_url)
    return _client
