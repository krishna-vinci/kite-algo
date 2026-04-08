from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from typing import Any, Dict
from uuid import uuid4

from broker_api.redis_events import get_redis
from runtime_monitor import heartbeat, set_component_status


logger = logging.getLogger(__name__)


class PaperMarketEngine:
    def __init__(
        self,
        *,
        service: Any,
        market_data_runtime: Any | None = None,
        redis_client: Any | None = None,
        component_name: str = "paper_runtime_market_engine",
    ) -> None:
        self.service = service
        self.market_data_runtime = market_data_runtime
        self.redis = redis_client or get_redis()
        self.component_name = component_name
        self.owner_id = f"backend:paper-runtime:{uuid4()}"
        self._running = False
        self._task: asyncio.Task | None = None
        self._stats: Dict[str, Any] = {"processed_ticks": 0, "last_tick": None, "last_error": None}
        self._last_sync_at: float = 0.0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.sync_subscriptions()
        self._task = asyncio.create_task(self._run())
        set_component_status(self.component_name, "healthy", detail="Paper market engine started", meta=self.status())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.market_data_runtime is not None:
            try:
                await self.market_data_runtime.delete_owner(self.owner_id)
            except Exception:
                logger.warning("Failed to delete paper runtime market owner %s", self.owner_id, exc_info=True)
        set_component_status(self.component_name, "stopped", detail="Paper market engine stopped", meta=self.status())

    async def sync_subscriptions(self) -> Dict[str, Any]:
        tokens = await self.service.active_market_tokens()
        if self.market_data_runtime is not None:
            if tokens:
                await self.market_data_runtime.set_owner_subscriptions(self.owner_id, {int(token): "full" for token in tokens})
            else:
                try:
                    await self.market_data_runtime.delete_owner(self.owner_id)
                except Exception:
                    logger.debug("Paper market engine owner cleanup skipped", exc_info=True)
        self._stats["subscribed_tokens"] = list(tokens)
        self._last_sync_at = monotonic()
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {"running": self._running, "owner_id": self.owner_id, "stats": dict(self._stats)}

    async def process_tick(self, tick: Dict[str, Any]) -> None:
        await self.service.process_tick(tick)
        self._stats["processed_ticks"] += 1
        self._stats["last_tick"] = {"instrument_token": tick.get("instrument_token"), "last_price": tick.get("last_price")}
        heartbeat(self.component_name, detail="Paper market engine healthy", meta=self.status())

    async def _run(self) -> None:
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe("market:ticks")
            while self._running:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if monotonic() - self._last_sync_at >= 5.0:
                    await self.sync_subscriptions()
                if not message or message.get("type") != "message":
                    continue
                payload = message.get("data")
                tick = json.loads(payload) if isinstance(payload, str) else payload
                try:
                    await self.process_tick(tick)
                except Exception as exc:
                    logger.error("Paper market engine tick handling failed: %s", exc, exc_info=True)
                    self._stats["last_error"] = str(exc)
                    set_component_status(self.component_name, "degraded", detail=str(exc), meta=self.status())
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await pubsub.unsubscribe("market:ticks")
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass
