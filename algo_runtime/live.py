from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Set, Tuple
from uuid import uuid4

from broker_api.kite_session import make_account_id
from broker_api.redis_events import get_redis
from runtime_monitor import heartbeat, set_component_status

from .models import AlgoLifecycleState, TriggerEvent, TriggerType


logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class AlgoRuntimeLiveWorker:
    def __init__(
        self,
        *,
        service: Any,
        market_data_runtime: Any | None = None,
        candle_aggregator: Any | None = None,
        redis_client: Any | None = None,
        queue_maxsize: int = 2000,
        component_name: str = "algo_runtime_live_triggers",
    ) -> None:
        self.service = service
        self.market_data_runtime = market_data_runtime
        self.candle_aggregator = candle_aggregator
        self.redis = redis_client or get_redis()
        self.component_name = component_name
        self.owner_id = f"backend:algo-runtime:{uuid4()}"
        self.candle_owner_id = f"{self.owner_id}:candles"
        self.queue_maxsize = max(100, int(queue_maxsize))
        self.fill_progress_limit = 5000

        self._running = False
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue(maxsize=self.queue_maxsize)
        self._tasks: Dict[str, asyncio.Task] = {}
        self._fill_progress: Dict[Tuple[str, str], int] = {}
        self._routing: Dict[str, Any] = {
            "market_tokens": set(),
            "candle_pairs": set(),
            "candle_tokens": set(),
            "account_scopes": set(),
            "enabled_triggers": set(),
            "allow_unscoped_order_events": False,
            "allow_unscoped_position_events": False,
        }
        self._stats: Dict[str, Any] = {
            "received": {"tick": 0, "candle_close": 0, "order_update": 0, "fill_update": 0, "position_update": 0},
            "enqueued": 0,
            "dropped": 0,
            "processed": 0,
            "last_received_at": None,
            "last_processed_at": None,
            "last_trigger": None,
            "last_results": None,
            "last_error": None,
            "last_error_at": None,
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.sync_dependencies()
        self._tasks["dispatcher"] = asyncio.create_task(self._dispatch_loop())
        self._tasks["ticks"] = asyncio.create_task(
            self._pubsub_loop(channels=["market:ticks"], on_message=self._handle_tick_message, loop_name="ticks")
        )
        self._tasks["candles"] = asyncio.create_task(
            self._pubsub_loop(patterns=["realtime_candles:*:*"], on_message=self._handle_candle_message, loop_name="candles")
        )
        self._tasks["orders"] = asyncio.create_task(
            self._pubsub_loop(channels=["orders.events"], on_message=self._handle_order_message, loop_name="orders")
        )
        self._tasks["positions"] = asyncio.create_task(
            self._pubsub_loop(patterns=["positions.events:*"], on_message=self._handle_position_message, loop_name="positions")
        )
        set_component_status(self.component_name, "healthy", detail="Algo runtime live trigger worker started", meta=self.status())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self.market_data_runtime is not None:
            try:
                await self.market_data_runtime.delete_owner(self.owner_id)
            except Exception:
                logger.warning("Failed to remove algo runtime market owner %s", self.owner_id, exc_info=True)
        if self.candle_aggregator is not None and hasattr(self.candle_aggregator, "set_external_tokens"):
            try:
                await self.candle_aggregator.set_external_tokens(self.candle_owner_id, set())
            except Exception:
                logger.warning("Failed to clear algo runtime candle tokens", exc_info=True)
        set_component_status(self.component_name, "stopped", detail="Algo runtime live trigger worker stopped", meta=self.status())

    async def sync_dependencies(self) -> Dict[str, Any]:
        active_statuses = {AlgoLifecycleState.ENABLED, AlgoLifecycleState.RUNNING}
        instances = await self.service.kernel.list_instances()
        dependency_specs = [instance.dependency_spec for instance in instances if instance.status in active_statuses]
        aggregated = self.service.kernel.dependency_aggregator.aggregate(dependency_specs)

        market_tokens = set(int(token) for token in aggregated.market_tokens.keys())
        candle_pairs = {(int(spec.token), str(spec.timeframe)) for spec in aggregated.candle_series}
        candle_pairs.update((int(spec.token), str(spec.timeframe)) for spec in aggregated.indicators)
        candle_tokens = {token for token, _ in candle_pairs}
        account_scopes = {str(scope) for scope in self.service.kernel.dependency_aggregator.account_scopes(dependency_specs)}
        enabled_triggers = {trigger.value for trigger in aggregated.triggers}
        allow_unscoped_order_events = any(
            spec.order_scope.value != "none"
            and not spec.account_scope
            and bool({TriggerType.ORDER_UPDATE, TriggerType.FILL_UPDATE}.intersection(spec.triggers))
            for spec in dependency_specs
        )
        allow_unscoped_position_events = any(
            TriggerType.POSITION_UPDATE in spec.triggers and not spec.account_scope and bool(spec.position_filters)
            for spec in dependency_specs
        )

        self._routing = {
            "market_tokens": market_tokens,
            "candle_pairs": candle_pairs,
            "candle_tokens": candle_tokens,
            "account_scopes": account_scopes,
            "enabled_triggers": enabled_triggers,
            "allow_unscoped_order_events": allow_unscoped_order_events,
            "allow_unscoped_position_events": allow_unscoped_position_events,
        }

        if self.market_data_runtime is not None:
            if market_tokens:
                payload = {
                    int(token): (mode.value if hasattr(mode, "value") else str(mode))
                    for token, mode in aggregated.market_tokens.items()
                }
                await self.market_data_runtime.set_owner_subscriptions(self.owner_id, payload)
            else:
                try:
                    await self.market_data_runtime.delete_owner(self.owner_id)
                except Exception:
                    logger.debug("Algo runtime market owner %s did not need cleanup during sync", self.owner_id, exc_info=True)

        if self.candle_aggregator is not None and hasattr(self.candle_aggregator, "set_external_tokens"):
            await self.candle_aggregator.set_external_tokens(self.candle_owner_id, candle_tokens)

        heartbeat(self.component_name, detail="Algo runtime live trigger routing synced", meta=self.status())
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "owner_id": self.owner_id,
            "queue_size": self._queue.qsize(),
            "routing": {
                "market_tokens": sorted(self._routing["market_tokens"]),
                "candle_pairs": [f"{token}:{timeframe}" for token, timeframe in sorted(self._routing["candle_pairs"])],
                "candle_tokens": sorted(self._routing["candle_tokens"]),
                "account_scopes": sorted(self._routing["account_scopes"]),
                "enabled_triggers": sorted(self._routing["enabled_triggers"]),
                "allow_unscoped_order_events": bool(self._routing["allow_unscoped_order_events"]),
                "allow_unscoped_position_events": bool(self._routing["allow_unscoped_position_events"]),
            },
            "stats": {
                **self._stats,
                "received": dict(self._stats["received"]),
            },
        }

    async def _pubsub_loop(
        self,
        *,
        channels: Optional[list[str]] = None,
        patterns: Optional[list[str]] = None,
        on_message: Any,
        loop_name: str,
    ) -> None:
        pubsub = None
        retry_delay = 1.0
        channels = channels or []
        patterns = patterns or []
        try:
            while self._running:
                try:
                    if pubsub is None:
                        pubsub = self.redis.pubsub()
                        if channels:
                            await pubsub.subscribe(*channels)
                        if patterns:
                            await pubsub.psubscribe(*patterns)
                        retry_delay = 1.0
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message or message.get("type") not in {"message", "pmessage"}:
                        continue
                    raw_payload = message.get("data")
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    await on_message(payload, message)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.error("Algo runtime live %s loop failed", loop_name, exc_info=True)
                    self._stats["last_error"] = f"{loop_name}_loop_failed"
                    self._stats["last_error_at"] = _utcnow()
                    set_component_status(self.component_name, "degraded", detail=f"Algo runtime live {loop_name} loop error", meta=self.status())
                    if pubsub is not None:
                        try:
                            await pubsub.aclose()
                        except Exception:
                            pass
                        pubsub = None
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 10.0)
        finally:
            if pubsub is not None:
                try:
                    if channels:
                        await pubsub.unsubscribe(*channels)
                except Exception:
                    pass
                try:
                    if patterns:
                        await pubsub.punsubscribe(*patterns)
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                trigger = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                results = await self.service.dispatch_trigger(trigger)
                self._stats["processed"] += 1
                self._stats["last_processed_at"] = _utcnow()
                self._stats["last_trigger"] = trigger.model_dump(mode="json", by_alias=True)
                self._stats["last_results"] = {
                    "matched_instances": len(results),
                    "action_count": sum(int(result.get("action_count") or 0) for result in results),
                }
                heartbeat(self.component_name, detail="Algo runtime live trigger worker healthy", meta=self.status())
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Algo runtime live dispatch failed: %s", exc, exc_info=True)
                self._stats["last_error"] = str(exc)
                self._stats["last_error_at"] = _utcnow()
                set_component_status(self.component_name, "degraded", detail=str(exc), meta=self.status())
            finally:
                self._queue.task_done()

    async def _handle_tick_message(self, payload: Dict[str, Any], message: Dict[str, Any]) -> None:
        if TriggerType.TICK.value not in self._routing["enabled_triggers"]:
            return
        token = payload.get("instrument_token") if isinstance(payload, dict) else None
        try:
            token = int(token)
        except (TypeError, ValueError):
            return
        if token not in self._routing["market_tokens"]:
            return
        self._record_received(TriggerType.TICK.value)
        occurred_at = _parse_iso(payload.get("exchange_timestamp") or payload.get("received_at"))
        await self._enqueue(
            TriggerEvent(
                type=TriggerType.TICK,
                token=token,
                occurred_at=occurred_at or datetime.now(timezone.utc),
                payload=payload,
            )
        )

    async def _handle_candle_message(self, payload: Dict[str, Any], message: Dict[str, Any]) -> None:
        if TriggerType.CANDLE_CLOSE.value not in self._routing["enabled_triggers"]:
            return
        if not isinstance(payload, dict):
            return
        try:
            token = int(payload.get("instrument_token"))
        except (TypeError, ValueError):
            return
        timeframe = str(payload.get("interval") or "").strip().lower()
        if not timeframe or (token, timeframe) not in self._routing["candle_pairs"]:
            return
        self._record_received(TriggerType.CANDLE_CLOSE.value)
        candle = payload.get("candle") if isinstance(payload.get("candle"), list) else []
        occurred_at = _parse_iso(candle[0]) if candle else None
        await self._enqueue(
            TriggerEvent(
                type=TriggerType.CANDLE_CLOSE,
                token=token,
                timeframe=timeframe,
                occurred_at=occurred_at or datetime.now(timezone.utc),
                payload=payload,
            )
        )

    async def _handle_order_message(self, payload: Dict[str, Any], message: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        account_id = payload.get("account_id") or make_account_id(payload.get("user_id"))
        if not account_id and not self._routing["allow_unscoped_order_events"]:
            return
        if account_id and account_id not in self._routing["account_scopes"] and not self._routing["allow_unscoped_order_events"]:
            return
        occurred_at = _parse_iso(payload.get("event_timestamp")) or datetime.now(timezone.utc)
        order_id = str(payload.get("order_id") or "").strip() or None

        if TriggerType.ORDER_UPDATE.value in self._routing["enabled_triggers"]:
            self._record_received(TriggerType.ORDER_UPDATE.value)
            await self._enqueue(
                TriggerEvent(
                    type=TriggerType.ORDER_UPDATE,
                    account_id=account_id,
                    order_id=order_id,
                    occurred_at=occurred_at,
                    payload=payload,
                )
            )

        if TriggerType.FILL_UPDATE.value in self._routing["enabled_triggers"]:
            try:
                filled_quantity = int(payload.get("filled_quantity") or 0)
            except (TypeError, ValueError):
                filled_quantity = 0
            fill_identity = order_id or payload.get("id") or payload.get("event_timestamp") or payload.get("exchange_update_timestamp") or ""
            fill_key = (str(account_id or "unknown"), str(fill_identity))
            previous_filled = self._fill_progress.get(fill_key, 0)
            if filled_quantity > previous_filled:
                self._fill_progress[fill_key] = filled_quantity
                while len(self._fill_progress) > self.fill_progress_limit:
                    self._fill_progress.pop(next(iter(self._fill_progress)))
                self._record_received(TriggerType.FILL_UPDATE.value)
                await self._enqueue(
                    TriggerEvent(
                        type=TriggerType.FILL_UPDATE,
                        account_id=account_id,
                        order_id=order_id,
                        occurred_at=occurred_at,
                        payload=payload,
                    )
                )

    async def _handle_position_message(self, payload: Dict[str, Any], message: Dict[str, Any]) -> None:
        if TriggerType.POSITION_UPDATE.value not in self._routing["enabled_triggers"]:
            return
        if not isinstance(payload, dict):
            return
        account_id = str(payload.get("account_id") or "").strip()
        if not account_id and not self._routing["allow_unscoped_position_events"]:
            return
        if account_id and account_id not in self._routing["account_scopes"] and not self._routing["allow_unscoped_position_events"]:
            return
        self._record_received(TriggerType.POSITION_UPDATE.value)
        occurred_at = _parse_iso(payload.get("timestamp")) or datetime.now(timezone.utc)
        await self._enqueue(
            TriggerEvent(
                type=TriggerType.POSITION_UPDATE,
                account_id=account_id,
                occurred_at=occurred_at,
                payload=payload,
            )
        )

    async def _enqueue(self, trigger: TriggerEvent) -> None:
        try:
            self._queue.put_nowait(trigger)
            self._stats["enqueued"] += 1
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            logger.warning("Algo runtime live queue full; dropping trigger %s", trigger.trigger_type.value)
            set_component_status(self.component_name, "degraded", detail="Algo runtime live queue is full", meta=self.status())

    def _record_received(self, trigger_type: str) -> None:
        received = self._stats["received"]
        received[trigger_type] = int(received.get(trigger_type) or 0) + 1
        self._stats["last_received_at"] = _utcnow()
