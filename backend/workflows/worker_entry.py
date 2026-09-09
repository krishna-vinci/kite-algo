"""Runnable skeleton entrypoint for the evaluation worker (Task 7).

Usage::

    python -m backend.workflows.worker_entry

Environment:
- ``DATABASE_URL`` (required) — Postgres (or SQLite) DSN for durable state
- ``REDIS_URL`` (required) — Redis for the tick / completed-candle pub/sub
- ``MARKET_RUNTIME_URL`` (optional) — market-runtime HTTP base URL; forwarded
  to the client's own ``MARKET_RUNTIME_HTTP_URL`` variable when set
- ``ALERTS_INSTRUMENT_TOKENS`` (optional) — JSON mapping of
  ``"EXCHANGE:SYMBOL"`` to numeric instrument tokens, e.g.
  ``{"NSE:RELIANCE": 738561}``; the worker subscribes these tokens on the
  market-runtime and uses them to resolve candle history. Empty/absent is
  warned loudly: without tokens no instrument can resolve.
- ``ALERTS_POLL_INTERVAL_S`` (optional, default 2.0)
- ``ALERTS_REFRESH_INTERVAL_S`` (optional, default 10) — subscription
  activation/pause refresh cadence
- ``ALERTS_HEALTH_INTERVAL_S`` (optional, default 30) — health report cadence
- ``ALERTS_HEALTH_FILE`` (optional) — path the JSON health snapshot is
  written to every health interval
- ``ALERTS_DELIVERY_ENABLED`` (optional, default "1") — set to
  ``0/false/no/off`` to disable the supervised delivery worker
- ``MARKET_RUNTIME_OWNER_LEASE_TTL_SEC`` (optional, default 90) — the
  market-runtime owner lease TTL; ownership is renewed every TTL/3

This module imports cleanly WITHOUT backend.app dependencies: every heavy
import (SQLAlchemy engine, repositories, redis, market-runtime client) happens
inside :func:`main`. Compose/service wiring is deliberately deferred.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("backend.workflows.worker_entry")

# ``make_resolver`` is a PINNED contract being added to
# backend.notifications.worker by a parallel change. Import it when present;
# otherwise the minimal local fallback below keeps this entrypoint (and its
# tests) working until the real resolver lands.
try:  # pragma: no cover - depends on the parallel agent's landing order
    from backend.notifications.worker import make_resolver as _pinned_make_resolver
except ImportError:  # pragma: no cover
    _pinned_make_resolver = None

_OFF_VALUES = {"0", "false", "no", "off"}


def build_instrument_tokens(raw: Optional[str] = None) -> Dict[str, int]:
    """Parse ``ALERTS_INSTRUMENT_TOKENS`` (JSON: instrument key -> token)."""
    raw = (
        raw if raw is not None else os.environ.get("ALERTS_INSTRUMENT_TOKENS", "")
    ).strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("must be a JSON object")
        return {str(key): int(value) for key, value in data.items()}
    except (TypeError, ValueError) as exc:
        logger.error("invalid ALERTS_INSTRUMENT_TOKENS (%s); ignoring: %s", raw, exc)
        return {}


def warn_if_no_instruments(tokens: Dict[str, int]) -> None:
    """Loud warning when no instrument tokens are configured.

    Without tokens neither the tick feeds nor candle history can resolve any
    instrument, so the worker would silently do nothing.
    """
    if not tokens:
        logger.warning(
            "ALERTS_INSTRUMENT_TOKENS is empty/absent: no instruments can "
            "resolve — no ticks will be consumed and no candle history can "
            "be warmed. Set ALERTS_INSTRUMENT_TOKENS to a JSON object like "
            '{"NSE:RELIANCE": 738561}.'
        )


def delivery_enabled() -> bool:
    """``ALERTS_DELIVERY_ENABLED`` kill switch; delivery is default-on."""
    return os.environ.get("ALERTS_DELIVERY_ENABLED", "1").strip().lower() not in _OFF_VALUES


def build_subscription_loader(session_factory: Callable[[], Any]) -> Callable[[str], Optional[dict]]:
    """Build the pinned ``subscription_loader`` for ``make_resolver``.

    ``subscription_loader(subscription_id) -> Optional[dict]`` returning
    ``{"instrument_key", "alert_id", "message", "expires_at",
    "workflow_name"}`` (from alert_subscriptions joined with the revision's
    workflow name), or None when the subscription is gone.
    """
    from sqlalchemy import select

    from backend.workflows.repository import (
        AlertSubscription,
        Workflow,
        WorkflowRevision,
    )

    def load(subscription_id: str) -> Optional[dict]:
        try:
            with session_factory() as session:
                row = session.execute(
                    select(AlertSubscription, Workflow.name)
                    .join(WorkflowRevision, AlertSubscription.revision_id == WorkflowRevision.id)
                    .join(Workflow, WorkflowRevision.workflow_id == Workflow.id)
                    .where(AlertSubscription.id == str(subscription_id))
                ).first()
                if row is None:
                    return None
                subscription, workflow_name = row
                config = subscription.config or {}
                return {
                    "instrument_key": subscription.instrument_key,
                    "alert_id": subscription.alert_id,
                    "message": config.get("message"),
                    "expires_at": config.get("expires_at"),
                    "workflow_name": workflow_name,
                }
        except Exception:
            logger.error(
                "subscription loader failed for %s", subscription_id, exc_info=True,
            )
            return None

    return load


def _fallback_make_resolver(
    notification_repo: Any, subscription_loader: Callable[[str], Optional[dict]]
) -> Callable[[str], Optional[dict]]:
    """Minimal stand-in for the pinned ``make_resolver`` contract.

    ``resolver(delivery_id) -> {"provider", "destination", "subject", "body",
    "expires_at"}`` or None: the send context comes from the delivery's
    channel plus the originating subscription (via the subscription loader).
    Replaced by the real implementation as soon as
    ``backend.notifications.worker.make_resolver`` exists.
    """
    from sqlalchemy import select

    from backend.notifications.repository import Delivery
    from backend.workflows.repository import SignalEvent

    def resolve(delivery_id: str) -> Optional[dict]:
        with notification_repo.session_factory() as session:
            row = session.execute(
                select(Delivery, SignalEvent.subscription_id).where(
                    Delivery.id == str(delivery_id),
                    SignalEvent.id == Delivery.event_id,
                )
            ).first()
            if row is None:
                return None
            delivery, subscription_id = row
            channel = notification_repo.get_channel(delivery.channel_id, db=session)
            subscription_context = subscription_loader(subscription_id) or {}

        if channel is None:
            return None
        workflow_name = subscription_context.get("workflow_name") or "alert"
        instrument_key = subscription_context.get("instrument_key") or "-"
        message = subscription_context.get("message")
        body = str(message) if message else (
            f"{workflow_name}: {instrument_key} alert fired"
        )
        return {
            "provider": channel.provider,
            "destination": dict(channel.destination or {}),
            "subject": workflow_name,
            "body": body,
            "expires_at": subscription_context.get("expires_at"),
        }

    return resolve


def build_delivery_resolver(
    notification_repo: Any, subscription_loader: Callable[[str], Optional[dict]]
) -> Callable[[str], Optional[dict]]:
    """Resolver for the DeliveryWorker honoring the pinned make_resolver."""
    if _pinned_make_resolver is not None:
        return _pinned_make_resolver(notification_repo, subscription_loader)
    logger.info(
        "notifications.worker.make_resolver not available yet; "
        "using the local fallback delivery resolver"
    )
    return _fallback_make_resolver(notification_repo, subscription_loader)


def build_renewal(
    client: Any, owner_id: str, tokens: Dict[str, int]
) -> Callable[[], Any]:
    """Ownership renewal callable for EvaluationWorker (same client as start)."""

    async def renew() -> None:
        await client.set_owner_subscriptions(
            owner_id, {int(token): "full" for token in tokens.values()}
        )

    return renew


async def supervise(
    worker: Any,
    delivery_worker: Any,
    *,
    stop: Any,
    delivery_poll_interval_s: float = 2.0,
) -> list:
    """Run the evaluation (and delivery) tasks until ``stop`` is set.

    Both tasks are cancelled cleanly on shutdown; a task that crashes before
    the stop signal also triggers shutdown and its error is logged.
    """
    tasks = [asyncio.create_task(worker.run(), name="evaluation-worker")]
    if delivery_worker is not None and delivery_enabled():
        tasks.append(
            asyncio.create_task(
                delivery_worker.run_forever(delivery_poll_interval_s),
                name="delivery-worker",
            )
        )
    elif delivery_worker is not None:
        logger.warning(
            "ALERTS_DELIVERY_ENABLED is off: delivery task not started; "
            "pending deliveries will NOT be sent"
        )
    if hasattr(stop, "wait"):
        watcher = asyncio.create_task(stop.wait(), name="stop-watcher")
    else:  # Future-like (tests): already scheduled on the loop
        watcher = asyncio.ensure_future(stop)
    try:
        await asyncio.wait(tasks + [watcher], return_when=asyncio.FIRST_COMPLETED)
    finally:
        watcher.cancel()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(watcher, *tasks, return_exceptions=True)
    for name, result in zip(["stop-watcher"] + [t.get_name() for t in tasks], results):
        if isinstance(result, asyncio.CancelledError):
            continue
        if isinstance(result, BaseException):
            logger.error("worker task %s ended with error", name, exc_info=result)
    logger.info("all worker tasks stopped")
    return results


async def _sync_market_runtime_subscriptions(owner_id: str, tokens: Dict[str, int]) -> None:
    """Best-effort dynamic subscribe on the market-runtime owner registry."""
    if not tokens:
        return
    try:
        from backend.broker_api.orders.market_runtime_client import (
            get_market_runtime_client,
        )

        client = await get_market_runtime_client()
        await client.set_owner_subscriptions(
            owner_id, {int(token): "full" for token in tokens.values()}
        )
        logger.info("subscribed %d token(s) on market-runtime as %s", len(tokens), owner_id)
    except Exception:
        logger.warning(
            "market-runtime subscribe failed; continuing without live tick push",
            exc_info=True,
        )


async def main(extra_tokens: Optional[Dict[str, int]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required (Postgres DSN for durable state)")
        return 2
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        logger.error("REDIS_URL is required (tick + completed-candle pub/sub)")
        return 2
    market_runtime_url = os.environ.get("MARKET_RUNTIME_URL", "").strip()
    if market_runtime_url:
        # Bridge onto the variable the market-runtime client actually reads.
        os.environ.setdefault("MARKET_RUNTIME_HTTP_URL", market_runtime_url)
        logger.info("market-runtime URL: %s", market_runtime_url)

    # Heavy imports stay inside main(): nothing above this line pulls in the
    # app package, redis, or the market-runtime client.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.notifications.repository import SqlAlchemyNotificationRepository
    from backend.notifications.worker import DeliveryWorker
    from backend.workflows.repository import SqlAlchemyWorkflowRepository
    from backend.workflows.runtime import (
        EvaluationWorker,
        MARKET_RUNTIME_OWNER_LEASE_TTL_S,
        PgCandleHistory,
        RedisCandleSource,
        RedisTickSource,
    )

    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    instrument_tokens = build_instrument_tokens()
    if extra_tokens:
        instrument_tokens.update(extra_tokens)
    warn_if_no_instruments(instrument_tokens)

    def channel_resolver(owner_id: str, names):
        """Channel-name -> channel-id mapping; missing names stay absent."""
        return {
            channel.name: channel.id
            for channel in notification_repo.list_channels(owner_id)
            if channel.enabled
        }

    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
    except Exception:
        logger.error("cannot reach Redis at %s", redis_url, exc_info=True)
        engine.dispose()
        return 2

    runtime_owner_id = f"alerts-worker:{uuid.uuid4()}"
    await _sync_market_runtime_subscriptions(runtime_owner_id, instrument_tokens)

    def tick_source_factory(instrument_key: str) -> RedisTickSource:
        token = instrument_tokens.get(instrument_key)
        mapping = {token: instrument_key} if token is not None else {}
        # each call returns a NEW source (fresh uuid epoch): the worker
        # relies on that to re-initialize ltp rules after a feed outage (D2)
        return RedisTickSource(redis_client, mapping)

    def candle_source_factory(instrument_key: str, timeframe: str) -> RedisCandleSource:
        token = instrument_tokens.get(instrument_key)
        mapping = {token: instrument_key} if token is not None else {}
        return RedisCandleSource(redis_client, mapping, interval=timeframe)

    # market-runtime owner leases expire after ~TTLs without renewal; renew
    # every TTL/3 with the SAME client used at startup.
    try:
        lease_ttl_s = float(os.environ.get(
            "MARKET_RUNTIME_OWNER_LEASE_TTL_SEC", MARKET_RUNTIME_OWNER_LEASE_TTL_S,
        ))
    except ValueError:
        lease_ttl_s = MARKET_RUNTIME_OWNER_LEASE_TTL_S
    renewal_interval_s = max(1.0, lease_ttl_s / 3.0)

    renewal = None
    if instrument_tokens:
        try:
            from backend.broker_api.orders.market_runtime_client import (
                get_market_runtime_client,
            )

            market_client = await get_market_runtime_client()
            renewal = build_renewal(market_client, runtime_owner_id, instrument_tokens)
        except Exception:
            logger.warning(
                "market-runtime renewal client unavailable; ownership "
                "renewal disabled", exc_info=True,
            )

    worker = EvaluationWorker(
        workflow_repo,
        session_factory,
        channel_resolver,
        tick_source_factory,
        candle_source_factory,
        candle_history=PgCandleHistory(engine, instrument_tokens),
        poll_interval_s=float(os.environ.get("ALERTS_POLL_INTERVAL_S", "2.0")),
        refresh_interval_s=float(os.environ.get("ALERTS_REFRESH_INTERVAL_S", "10")),
        renewal=renewal,
        renewal_interval_s=renewal_interval_s,
        health_interval_s=float(os.environ.get("ALERTS_HEALTH_INTERVAL_S", "30")),
    )

    # Supervised delivery task: drains the signal outbox so emitted alerts
    # actually reach their channels (default-on; env kill-switch).
    delivery_worker = None
    delivery_stats: Dict[str, Any] = {}

    def track_delivery_stats() -> Dict[str, Any]:
        return {"deliveries": dict(delivery_stats)}

    if delivery_enabled():
        subscription_loader = build_subscription_loader(session_factory)
        resolver = build_delivery_resolver(notification_repo, subscription_loader)
        delivery_worker = DeliveryWorker(notification_repo, resolver=resolver)
        original_run_once = delivery_worker.run_once

        async def counted_run_once(*args, **kwargs):
            try:
                summary = await original_run_once(*args, **kwargs)
            except Exception:
                delivery_stats["poll_errors"] = delivery_stats.get("poll_errors", 0) + 1
                raise
            for key, value in (summary or {}).items():
                delivery_stats[key] = delivery_stats.get(key, 0) + value
            return summary

        delivery_worker.run_once = counted_run_once
        worker.health_extra = track_delivery_stats
    else:
        logger.warning("ALERTS_DELIVERY_ENABLED is off: pending deliveries will NOT be sent")

    loop = asyncio.get_running_loop()
    stop_signal = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_signal.set)
        except (NotImplementedError, RuntimeError):
            pass  # non-main loop / platform without signal handlers

    await supervise(
        worker,
        delivery_worker,
        stop=stop_signal,
        delivery_poll_interval_s=float(os.environ.get("ALERTS_DELIVERY_POLL_INTERVAL_S", "2.0")),
    )
    await _sync_market_runtime_subscriptions_runtime_cleanup(runtime_owner_id)
    engine.dispose()
    logger.info("evaluation worker stopped")
    return 0


async def _sync_market_runtime_subscriptions_runtime_cleanup(owner_id: str) -> None:
    """Best-effort deregistration of this worker's market-runtime owner."""
    try:
        from backend.broker_api.orders.market_runtime_client import (
            get_market_runtime_client,
        )

        client = await get_market_runtime_client()
        await client.delete_owner(owner_id)
    except Exception:
        logger.warning("market-runtime owner cleanup failed", exc_info=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
