"""Delivery worker (Task 6): outbox claim -> send -> record -> backoff loop.

Consumes the delivery outbox owned by
:class:`backend.notifications.repository.SqlAlchemyNotificationRepository`:

1. claim due deliveries under a time-based lease (``claim_deliveries`` —
   pending/retrying rows past their backoff plus ``delivering`` rows whose
   lease expired, i.e. crashed workers are reclaimed);
2. resolve the per-delivery send context through ``resolver``;
3. short-circuit expired events without contacting the provider
   (spec E-13) — recorded as terminal ``expired``;
4. send via the channel's adapter and classify the outcome:
   accepted -> delivered, retryable/unknown -> retrying with jittered
   backoff (default 60s, E-21/E-22), permanent -> failed (E-20);
5. when a retrying classification reaches ``max_attempts`` attempts, fail
   terminally with ``last_error='max attempts exceeded'``;
6. one delivery's failure (adapter or resolver raising) never blocks its
   siblings — the failure is recorded as an ``unknown`` attempt and the
   loop moves on.

The adapter is resolved through the ``backend.notifications.adapters``
registry by default (``adapter_factory=get_adapter``) so tests (and
deployments) can inject fakes via ``register_adapter``. The ``resolver``
is a callable ``delivery_id -> dict`` with keys ``rule_name``,
``instrument_key``, ``template``, ``expires_at`` and optionally
precomputed ``subject``/``body`` (plus optional ``evidence``/``fired_at``
when the worker should render the message). ``resolver=None`` renders
from an empty context with placeholder defaults.

Stdlib + SQLAlchemy imports only — no redis; safe to import anywhere.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
from typing import Callable, Optional

from backend.notifications.adapters import (
    DeliveryOutcome,
    NotificationAdapter,
    get_adapter,
    truncate_text,
)
from backend.notifications.message import build_message
from backend.notifications.repository import Delivery, SqlAlchemyNotificationRepository

logger = logging.getLogger(__name__)

DETAIL_MAX_CHARS = 500

# provider outcome -> delivery status (unknown/unrecognized -> retrying, E-21/E-22)
_STATUS_FOR_OUTCOME = {
    "accepted": "delivered",
    "retryable": "retrying",
    "permanent": "failed",
    "unknown": "retrying",
}

__all__ = ["DeliveryWorker", "main"]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def _parse_moment(value) -> Optional[dt.datetime]:
    """Accept ISO-8601 strings or datetimes; ``None``/empty -> ``None``."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


class DeliveryWorker:
    """Claim due deliveries, send them, and record classified attempts."""

    def __init__(
        self,
        notification_repo: SqlAlchemyNotificationRepository,
        adapter_factory: Callable[[str], NotificationAdapter] = get_adapter,
        resolver: Optional[Callable[[str], dict]] = None,
        *,
        max_attempts: int = 8,
        default_backoff_s: float = 60.0,
        jitter_fraction: float = 0.2,
    ) -> None:
        self.notification_repo = notification_repo
        self.adapter_factory = adapter_factory
        self.resolver: Callable[[str], dict] = resolver or (lambda delivery_id: {})
        self.max_attempts = max(1, int(max_attempts))
        self.default_backoff_s = max(0.0, float(default_backoff_s))
        self.jitter_fraction = max(0.0, float(jitter_fraction))

    # -- loop ---------------------------------------------------------------

    async def run_once(self, now: Optional[dt.datetime] = None, limit: int = 10) -> dict:
        """Process up to ``limit`` due deliveries; return a summary counter dict."""
        now = _as_utc(now) if now is not None else _utcnow()
        claimed = self.notification_repo.claim_deliveries(now, limit=limit)
        summary = {"claimed": len(claimed), "delivered": 0, "retrying": 0, "failed": 0, "expired": 0}
        for delivery in claimed:
            try:
                status = await self._process_one(delivery, now)
            except Exception as exc:  # sibling isolation: one bad delivery never stops the batch
                logger.exception("delivery %s failed unexpectedly", delivery.id)
                status = self._record_unexpected(delivery, exc, now)
            if status in summary:
                summary[status] += 1
        return summary

    async def run_forever(self, poll_interval_s: float = 2.0) -> None:
        """Poll ``run_once`` forever; returns promptly on cancellation."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("delivery poll iteration failed; continuing")
            try:
                await asyncio.sleep(poll_interval_s)
            except asyncio.CancelledError:
                return

    # -- one delivery -------------------------------------------------------

    async def _process_one(self, delivery: Delivery, now: dt.datetime) -> str:
        """Send one claimed delivery and record the attempt; returns the new status."""
        context = dict(self.resolver(delivery.id) or {})

        expires_at = _parse_moment(context.get("expires_at"))
        if expires_at is not None and _as_utc(expires_at) <= now:
            detail = f"event expired at {_as_utc(expires_at).isoformat()}"
            self.notification_repo.record_attempt(
                delivery.id,
                int(delivery.attempts or 0) + 1,
                "expired",
                detail,
                "expired",
                last_error=detail,
                now=now,
            )
            return "expired"

        channel = self.notification_repo.get_channel(delivery.channel_id)
        if channel is None:
            outcome = DeliveryOutcome(
                status="permanent", detail=f"channel {delivery.channel_id} not found"
            )
        else:
            subject, body = self._render(delivery, context, now)
            adapter = self.adapter_factory(channel.provider)
            outcome = await adapter.send(dict(channel.destination or {}), subject, body)

        return self._record_outcome(delivery, outcome, now)

    def _render(self, delivery: Delivery, context: dict, now: dt.datetime) -> tuple[str, str]:
        subject = context.get("subject")
        body = context.get("body")
        if subject is not None and body is not None:
            return str(subject), str(body)
        return build_message(
            rule_name=str(context.get("rule_name") or "alert"),
            instrument_key=str(context.get("instrument_key") or "-"),
            evidence=dict(context.get("evidence") or {}),
            fired_at=context.get("fired_at") or now,
            template=context.get("template"),
            event_id=delivery.event_id,
        )

    def _record_outcome(self, delivery: Delivery, outcome: DeliveryOutcome, now: dt.datetime) -> str:
        attempt_no = int(delivery.attempts or 0) + 1
        outcome_status = str(getattr(outcome, "status", "") or "unknown")
        detail = truncate_text(str(getattr(outcome, "detail", "") or ""), DETAIL_MAX_CHARS)
        new_status = _STATUS_FOR_OUTCOME.get(outcome_status, "retrying")

        next_attempt_at = None
        delivered_at = None
        last_error: Optional[str] = detail

        if new_status == "delivered":
            delivered_at = now
            last_error = None
        elif new_status == "retrying":
            delay = self.default_backoff_s
            if outcome_status == "retryable" and getattr(outcome, "retry_after_s", None) is not None:
                delay = max(0.0, float(outcome.retry_after_s))
            if attempt_no >= self.max_attempts:
                new_status = "failed"
                last_error = "max attempts exceeded"
                detail = truncate_text(f"max attempts exceeded; {detail}", DETAIL_MAX_CHARS)
            else:
                jitter = random.random() * self.jitter_fraction * delay
                next_attempt_at = now + dt.timedelta(seconds=delay + jitter)

        self.notification_repo.record_attempt(
            delivery.id,
            attempt_no,
            outcome_status,
            detail,
            new_status,
            next_attempt_at=next_attempt_at,
            delivered_at=delivered_at,
            last_error=last_error,
            now=now,
        )
        return new_status

    def _record_unexpected(self, delivery: Delivery, exc: Exception, now: dt.datetime) -> str:
        """Best-effort record of an unexpected exception as an unknown outcome."""
        try:
            return self._record_outcome(
                delivery,
                DeliveryOutcome(status="unknown", detail=str(exc) or type(exc).__name__),
                now,
            )
        except Exception:
            logger.exception("delivery %s: could not record unexpected failure", delivery.id)
            return "retrying"  # lease will expire and the row will be retried


def main() -> None:
    """Blocking helper entry point (production wiring lands with the worker service)."""
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.workflows.repository import Base

    from .repository import SqlAlchemyNotificationRepository

    database_url = os.environ.get("ALERTS_DATABASE_URL", "sqlite+pysqlite:///./alerts-deliveries.db")
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    worker = DeliveryWorker(SqlAlchemyNotificationRepository(session_factory))
    asyncio.run(worker.run_forever())
