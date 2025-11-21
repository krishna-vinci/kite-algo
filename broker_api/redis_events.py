import asyncio
import json
import logging
import os
from typing import AsyncIterator, Dict

import redis.asyncio as redis
from redis.asyncio import Redis as RedisClient
from redis.exceptions import ConnectionError as RedisConnectionError

logger = logging.getLogger(__name__)

_redis_client: RedisClient | None = None


def get_redis() -> RedisClient:
    """
    Returns a singleton Redis client instance.
    Initializes the client from the REDIS_URL environment variable on first call.
    """
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        logger.info(f"Initializing Redis client for URL: {redis_url}")
        try:
            _redis_client = redis.from_url(redis_url, decode_responses=True)
        except Exception as e:
            logger.exception(f"Failed to initialize Redis client: {e}")
            raise
    return _redis_client


async def publish_event(channel: str, payload: Dict):
    """
    Publishes a JSON-serialized event to the specified Redis channel.
    """
    try:
        redis_client = get_redis()
        await redis_client.publish(channel, json.dumps(payload))
        logger.info(
            f"[ALERTS-PUB] Published event to channel '{channel}': {payload.get('type')}"
        )
    except RedisConnectionError:
        logger.error(f"Failed to publish event to Redis channel '{channel}': Connection error")
    except Exception:
        logger.exception(f"An unexpected error occurred while publishing to Redis channel '{channel}'")


async def pubsub_iter(channel: str) -> AsyncIterator[Dict]:
    """
    An async iterator that subscribes to a Redis channel and yields JSON-decoded messages.
    Handles connection management and retries with exponential backoff.
    """
    redis_client = get_redis()
    pubsub = None
    retry_delay = 1

    while True:
        try:
            if pubsub is None:
                pubsub = redis_client.pubsub()
                await pubsub.subscribe(channel)
                logger.info(f"[ALERTS-SSE] Subscribed to Redis channel: {channel}")
                retry_delay = 1  # Reset delay on successful connection

            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
            if message and message.get("type") == "message":
                try:
                    yield json.loads(message["data"])
                except json.JSONDecodeError:
                    logger.warning(f"Failed to decode JSON from message: {message['data']}")
            else:
                # Yield a heartbeat comment to keep the SSE connection alive
                yield {"event": "heartbeat", "data": ""}

        except RedisConnectionError:
            logger.warning(
                f"Redis connection lost. Reconnecting in {retry_delay}s..."
            )
            if pubsub:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
                pubsub = None
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)  # Exponential backoff up to 30s
        except asyncio.CancelledError:
            logger.info("[ALERTS-SSE] Client disconnected, cleaning up pubsub.")
            if pubsub:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.aclose()
                except Exception:
                    pass
            break
        except Exception:
            logger.exception("An unexpected error occurred in pubsub iterator.")
            if pubsub:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
                pubsub = None
            await asyncio.sleep(5)
