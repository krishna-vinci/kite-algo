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
  market-runtime and uses them to resolve candle history
- ``ALERTS_POLL_INTERVAL_S`` (optional, default 2.0)

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
from typing import Dict

logger = logging.getLogger("backend.workflows.worker_entry")


def build_instrument_tokens() -> Dict[str, int]:
    """Parse ``ALERTS_INSTRUMENT_TOKENS`` (JSON: instrument key -> token)."""
    raw = os.environ.get("ALERTS_INSTRUMENT_TOKENS", "").strip()
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


async def main() -> int:
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
    from backend.workflows.repository import SqlAlchemyWorkflowRepository
    from backend.workflows.runtime import (
        EvaluationWorker,
        PgCandleHistory,
        RedisCandleSource,
        RedisTickSource,
    )

    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    instrument_tokens = build_instrument_tokens()

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
        return RedisTickSource(redis_client, mapping)

    def candle_source_factory(instrument_key: str, timeframe: str) -> RedisCandleSource:
        token = instrument_tokens.get(instrument_key)
        mapping = {token: instrument_key} if token is not None else {}
        return RedisCandleSource(redis_client, mapping, interval=timeframe)

    worker = EvaluationWorker(
        workflow_repo,
        session_factory,
        channel_resolver,
        tick_source_factory,
        candle_source_factory,
        candle_history=PgCandleHistory(engine, instrument_tokens),
        poll_interval_s=float(os.environ.get("ALERTS_POLL_INTERVAL_S", "2.0")),
    )

    loop = asyncio.get_running_loop()
    stop_signal = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_signal.set)
        except (NotImplementedError, RuntimeError):
            pass  # non-main loop / platform without signal handlers

    runner = asyncio.create_task(worker.run(), name="evaluation-worker")
    await stop_signal.wait()
    logger.info("shutdown signal received; stopping evaluation worker")
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
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
