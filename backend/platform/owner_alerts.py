"""Direct owner risk alerts through ntfy, independent of the alerts-worker outbox.

Alerts are best-effort and never raise into their callers.  Each stable alert key
is deduplicated for a configurable cooldown so repeated state observations do
not page the owner repeatedly while still allowing a later reminder.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

_dedupe: dict[str, float] = {}
_state_lock = threading.Lock()
_background_threads: set[threading.Thread] = set()
_background_tasks: set[asyncio.Task[Any]] = set()


def _cooldown() -> float:
    try:
        return max(0.0, float(os.getenv("OWNER_ALERT_COOLDOWN_SECONDS", "300")))
    except (TypeError, ValueError):
        return 300.0


def _claim(key: str) -> bool:
    now = time.monotonic()
    with _state_lock:
        previous = _dedupe.get(key)
        if previous is not None and now - previous < _cooldown():
            logger.debug("Owner alert deduped for key %s", key)
            return False
        _dedupe[key] = now
    return True


def _bounded(title: str, message: str) -> tuple[str, str]:
    return str(title)[:200], str(message)[:4096]


async def _send(message: str, title: str = "", tags: Sequence[str] | None = None) -> None:
    """POST one alert to ntfy; raises on any failure so the caller can report it.

    Self-contained (httpx + the config reader) rather than importing
    ``broker_api``: that module has an import cycle when loaded cold, and an
    alert path must not depend on import order.
    """
    import httpx

    from backend.app.config import get_scheduler_ntfy_url

    url = get_scheduler_ntfy_url()
    if not url:
        raise RuntimeError("SCHEDULER_NTFY_URL is unset")
    headers = {"Title": title or "kite-algo"}
    if tags:
        headers["Tags"] = ",".join(str(tag) for tag in tags)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, content=message.encode("utf-8"), headers=headers)
        response.raise_for_status()


async def _deliver(*, title: str, message: str, tags: Sequence[str]) -> None:
    try:
        await _send(message, title=title, tags=tags)
    except Exception:
        logger.warning("Owner alert transport failed for title %s", title, exc_info=True)


async def alert_owner(
    *,
    key: str,
    title: str,
    message: str,
    tags: Sequence[str] = (),
) -> bool:
    """Send one owner alert, returning whether transport completed."""

    if not _claim(key):
        return False
    bounded_title, bounded_message = _bounded(title, message)
    try:
        await _send(bounded_message, title=bounded_title, tags=tags)
        return True
    except Exception:
        logger.warning("Owner alert transport failed for title %s", bounded_title, exc_info=True)
        return False


def _thread_deliver(*, title: str, message: str, tags: Sequence[str]) -> None:
    try:
        asyncio.run(_deliver(title=title, message=message, tags=tags))
    finally:
        _background_threads.discard(threading.current_thread())


def alert_owner_nowait(
    *,
    key: str,
    title: str,
    message: str,
    tags: Sequence[str] = (),
) -> None:
    """Schedule an owner alert without blocking the caller or raising."""

    if not _claim(key):
        return
    bounded_title, bounded_message = _bounded(title, message)
    bounded_tags = tuple(tags)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        thread = threading.Thread(
            target=_thread_deliver,
            kwargs={"title": bounded_title, "message": bounded_message, "tags": bounded_tags},
            daemon=True,
            name="owner-alert",
        )
        _background_threads.add(thread)
        thread.start()
        return

    task = loop.create_task(_deliver(title=bounded_title, message=bounded_message, tags=bounded_tags))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def join_background_for_tests(timeout: float = 2.0) -> None:
    """Wait for background alert work started by ``alert_owner_nowait``."""

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        threads = list(_background_threads)
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        for task in list(_background_tasks):
            if task.done():
                _background_tasks.discard(task)
        if not _background_threads and not _background_tasks:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.001)


def reset_for_tests() -> None:
    with _state_lock:
        _dedupe.clear()
    for task in list(_background_tasks):
        if task.done():
            _background_tasks.discard(task)
    for thread in list(_background_threads):
        if not thread.is_alive():
            _background_threads.discard(thread)
