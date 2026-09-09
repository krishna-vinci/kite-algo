"""Delivery worker (Task 6): outbox claim -> send -> record -> backoff loop.

Consumes the delivery outbox owned by
:class:`backend.notifications.repository.SqlAlchemyNotificationRepository`:

1. claim due deliveries under a time-based lease (``claim_deliveries`` —
   pending/retrying rows past their backoff plus ``delivering`` rows whose
   lease expired, i.e. crashed workers are reclaimed);
2. resolve the per-delivery send context through ``resolver``
   (production wiring: :func:`make_resolver`);
3. a resolver returning ``None`` means unresolvable context — the delivery is
   marked terminal ``failed`` with ``last_error='unresolvable_context'`` and
   the adapter is never called;
4. short-circuit expired events without contacting the provider
   (spec E-13) — recorded as terminal ``expired``;
5. send via the channel's adapter and classify the outcome:
   accepted -> delivered, retryable/unknown -> retrying with jittered
   exponential backoff (``default_backoff_s * 2^(attempt-1)`` capped at
   :data:`MAX_BACKOFF_S` = 900s, provider ``retry_after_s`` hints win,
   E-21/E-22), permanent -> failed (E-20);
6. ambiguous sends (``unknown`` outcomes) may not retry forever: once
   ``max_unknown_retries`` unknown attempts exist, the delivery is failed
   terminally with ``last_error='unknown retry limit reached'``;
7. when a retrying classification reaches ``max_attempts`` attempts, fail
   terminally with ``last_error='max attempts exceeded'``;
8. one delivery's failure (adapter or resolver raising) never blocks its
   siblings — the failure is recorded as an ``unknown`` attempt and the
   loop moves on.

Completion is lease-fenced: attempts are recorded through a guarded UPDATE
that only applies while the worker still owns the delivery (fresh lease and
unchanged lease identity). When the lease was lost — expired, or the row
reclaimed by another worker — the repository raises
:class:`backend.workflows.repository.LeaseConflict`; the worker logs it,
counts it as ``fenced`` in the ``run_once`` summary, and moves on.

Secret handling (pinned contract): channel rows carry ``secret_env`` — the
name of the env var holding the secret. The worker merges that pointer into
the destination before send (``token_env`` for telegram, ``url_env`` for
ntfy) so the real send always uses the channel's secret; destination
overrides beat the provider default, and the channel ``secret_env`` beats a
destination override. Detail strings never contain secret values.

The adapter is resolved through the ``backend.notifications.adapters``
registry by default (``adapter_factory=get_adapter``) so tests (and
deployments) can inject fakes via ``register_adapter``. The ``resolver``
is a callable ``delivery_id -> dict`` with keys ``rule_name``,
``instrument_key``, ``template``, ``expires_at`` and optionally
precomputed ``subject``/``body`` (plus optional ``evidence``/``fired_at``
when the worker should render the message); it may also supply
``provider``/``destination`` which win over the channel row fields.
``resolver=None`` renders from an empty context with placeholder defaults.

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
    merge_secret_env,
    truncate_text,
)
from backend.notifications.message import build_message
from backend.notifications.repository import Delivery, SqlAlchemyNotificationRepository
from backend.workflows.repository import LeaseConflict, SignalEvent

logger = logging.getLogger(__name__)

DETAIL_MAX_CHARS = 500

# Exponential backoff ceiling (fault 4): default_backoff_s * 2^(attempt-1)
# never exceeds this, jitter may still be added on top.
MAX_BACKOFF_S = 900.0

UNRESOLVABLE_CONTEXT_DETAIL = "unresolvable_context"
UNKNOWN_RETRY_LIMIT_DETAIL = "unknown retry limit reached"
MAX_ATTEMPTS_DETAIL = "max attempts exceeded"

# provider outcome -> delivery status (unknown/unrecognized -> retrying, E-21/E-22)
_STATUS_FOR_OUTCOME = {
    "accepted": "delivered",
    "retryable": "retrying",
    "permanent": "failed",
    "unknown": "retrying",
}

__all__ = ["DeliveryWorker", "make_resolver", "main", "MAX_BACKOFF_S"]


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


def make_resolver(
    notification_repo: SqlAlchemyNotificationRepository,
    subscription_loader: Callable[[str], Optional[dict]],
) -> Callable[[str], Optional[dict]]:
    """Production message resolver (pinned contract).

    ``subscription_loader(subscription_id)`` returns
    ``{"instrument_key", "alert_id", "message", "expires_at",
    "workflow_name"}`` or ``None``. The returned resolver maps a delivery id
    to::

        {
            "provider": channel.provider,
            "destination": channel.destination merged with the channel
                           ``secret_env`` override (telegram -> ``token_env``,
                           ntfy -> ``url_env``),
            "subject": ..., "body": ...,   # rendered via build_message
            "expires_at": subscription expires_at,
        }

    or ``None`` when the delivery, its event, its channel or the
    subscription context cannot be resolved (the worker then marks the
    delivery ``failed`` with ``'unresolvable_context'``). Loader errors are
    deliberately not swallowed: the worker records them as ``unknown``
    attempts so transient storage problems retry instead of failing the
    delivery terminally.
    """

    def resolve(delivery_id: str) -> Optional[dict]:
        delivery = notification_repo.get_delivery(delivery_id)
        if delivery is None:
            return None
        channel = notification_repo.get_channel(delivery.channel_id)
        if channel is None:
            return None
        session = notification_repo.session_factory()
        try:
            event = session.get(SignalEvent, delivery.event_id)
        finally:
            session.close()
        if event is None:
            return None

        subscription = subscription_loader(event.subscription_id)
        if not subscription:
            return None

        workflow_name = subscription.get("workflow_name")
        alert_id = subscription.get("alert_id")
        rule_name = f"{workflow_name}:{alert_id}" if workflow_name else alert_id

        subject, body = build_message(
            rule_name=str(rule_name or "alert"),
            instrument_key=str(subscription.get("instrument_key") or "-"),
            evidence=dict(event.evidence or {}),
            fired_at=event.fired_at,
            template=subscription.get("message"),
            event_id=event.id,
        )
        return {
            "provider": channel.provider,
            "destination": merge_secret_env(
                channel.provider, channel.destination, channel.secret_env
            ),
            "subject": subject,
            "body": body,
            "expires_at": subscription.get("expires_at"),
        }

    return resolve


class DeliveryWorker:
    """Claim due deliveries, send them, and record classified attempts."""

    def __init__(
        self,
        notification_repo: SqlAlchemyNotificationRepository,
        adapter_factory: Callable[[str], NotificationAdapter] = get_adapter,
        resolver: Optional[Callable[[str], Optional[dict]]] = None,
        *,
        max_attempts: int = 8,
        max_unknown_retries: int = 3,
        default_backoff_s: float = 60.0,
        jitter_fraction: float = 0.2,
        clock: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self.notification_repo = notification_repo
        self.adapter_factory = adapter_factory
        self.resolver: Callable[[str], Optional[dict]] = resolver or (lambda delivery_id: {})
        self.max_attempts = max(1, int(max_attempts))
        self.max_unknown_retries = max(0, int(max_unknown_retries))
        self.default_backoff_s = max(0.0, float(default_backoff_s))
        self.jitter_fraction = max(0.0, float(jitter_fraction))
        self.clock = clock

    # -- loop ---------------------------------------------------------------

    async def run_once(self, now: Optional[dt.datetime] = None, limit: int = 10) -> dict:
        """Process up to ``limit`` due deliveries; return a summary counter dict."""
        explicit_now = now is not None
        now = _as_utc(now) if explicit_now else self._now()
        claimed = self.notification_repo.claim_deliveries(now, limit=limit)
        summary = {
            "claimed": len(claimed),
            "delivered": 0,
            "retrying": 0,
            "failed": 0,
            "expired": 0,
            "fenced": 0,
        }
        for delivery in claimed:
            # A batch claim is only a snapshot. Slow provider calls, large
            # batches, and fake-clock tests must not reuse the batch-start
            # timestamp for expiry, lease fencing, or retry scheduling.
            item_now = now if explicit_now else self._now()
            try:
                try:
                    status = await self._process_one(delivery, item_now, live_clock=not explicit_now)
                except LeaseConflict:
                    raise
                except Exception as exc:
                    # sibling isolation: one bad delivery never stops the batch
                    logger.exception("delivery %s failed unexpectedly", delivery.id)
                    status = self._record_unexpected(
                        delivery,
                        exc,
                        self._now() if not explicit_now else item_now,
                    )
            except LeaseConflict:
                # Lease-fenced completion (fault 2): our lease expired and the
                # delivery was (or is about to be) reclaimed elsewhere — the
                # stale attempt is discarded and the new holder decides.
                logger.warning(
                    "delivery %s fenced: lease lost before completion; attempt discarded",
                    delivery.id,
                )
                summary["fenced"] += 1
                continue
            if status in summary:
                summary[status] += 1
        return summary

    def _now(self) -> dt.datetime:
        return _as_utc(self.clock())

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

    async def _process_one(
        self, delivery: Delivery, now: dt.datetime, *, live_clock: bool = False
    ) -> str:
        """Send one claimed delivery and record the attempt; returns the new status."""
        resolved = self.resolver(delivery.id)
        if resolved is None:
            # unresolvable context (fault 3): never call the adapter
            self.notification_repo.record_attempt(
                delivery.id,
                int(delivery.attempts or 0) + 1,
                "permanent",
                UNRESOLVABLE_CONTEXT_DETAIL,
                "failed",
                last_error=UNRESOLVABLE_CONTEXT_DETAIL,
                lease_until=delivery.lease_until,
                now=now,
            )
            return "failed"
        context = dict(resolved)

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
                lease_until=delivery.lease_until,
                now=now,
            )
            return "expired"

        channel = self.notification_repo.get_channel(delivery.channel_id)
        if channel is None:
            outcome = DeliveryOutcome(
                status="permanent", detail=f"channel {delivery.channel_id} not found"
            )
        else:
            # Re-read the claim immediately before provider I/O. The row may
            # have been reclaimed while resolving context or rendering a
            # message. This check is advisory to the final lease-fenced write
            # (a provider can still outlive a lease), but prevents avoidable
            # sends after a known expiry/loss.
            if live_clock:
                now = self._now()
            self._assert_claim_is_current(delivery, now)
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
                    lease_until=delivery.lease_until,
                    now=now,
                )
                return "expired"
            subject, body = self._render(delivery, context, now)
            # resolver-provided provider/destination win; channel row fields
            # are the fallback, and the channel secret_env pointer is always
            # merged into the destination before send (fault 1).
            provider = str(context.get("provider") or channel.provider)
            destination = dict(channel.destination or {})
            override = context.get("destination")
            if isinstance(override, dict):
                destination.update(override)
            destination = merge_secret_env(provider, destination, channel.secret_env)
            adapter = self.adapter_factory(provider)
            outcome = await adapter.send(destination, subject, body)

        completion_now = self._now() if live_clock else now
        return self._record_outcome(delivery, outcome, completion_now)

    def _assert_claim_is_current(self, delivery: Delivery, now: dt.datetime) -> None:
        current = self.notification_repo.get_delivery(delivery.id)
        if current is None:
            raise LeaseConflict(f"delivery {delivery.id}: row disappeared")
        expected = delivery.lease_until
        actual = current.lease_until
        if current.status != "delivering" or (
            expected is not None and actual != expected
        ):
            raise LeaseConflict(f"delivery {delivery.id}: claim was reclaimed")
        if actual is not None and _as_utc(actual) <= now:
            raise LeaseConflict(f"delivery {delivery.id}: lease expired before send")

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
            # ambiguous sends must not retry forever (spec E-22, fault 5)
            if outcome_status == "unknown" and self._unknown_budget_exhausted(delivery):
                new_status = "failed"
                detail = UNKNOWN_RETRY_LIMIT_DETAIL
                last_error = detail
            if new_status == "retrying":
                hint = getattr(outcome, "retry_after_s", None)
                if hint is not None:
                    delay = max(0.0, float(hint))
                else:
                    delay = min(
                        self.default_backoff_s * (2 ** max(0, attempt_no - 1)),
                        MAX_BACKOFF_S,
                    )
                if attempt_no >= self.max_attempts:
                    new_status = "failed"
                    last_error = MAX_ATTEMPTS_DETAIL
                    detail = truncate_text(f"{MAX_ATTEMPTS_DETAIL}; {detail}", DETAIL_MAX_CHARS)
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
            lease_until=delivery.lease_until,
            now=now,
        )
        return new_status

    def _unknown_budget_exhausted(self, delivery: Delivery) -> bool:
        """True when ``max_unknown_retries`` unknown attempts were already recorded."""
        prior_unknowns = self.notification_repo.count_attempts(delivery.id, outcome="unknown")
        return prior_unknowns >= self.max_unknown_retries

    def _record_unexpected(self, delivery: Delivery, exc: Exception, now: dt.datetime) -> str:
        """Best-effort record of an unexpected exception as an unknown outcome."""
        try:
            return self._record_outcome(
                delivery,
                DeliveryOutcome(status="unknown", detail=str(exc) or type(exc).__name__),
                now,
            )
        except LeaseConflict:
            raise  # fenced: counted by run_once
        except Exception:
            logger.exception("delivery %s: could not record unexpected failure", delivery.id)
            return "retrying"  # lease will expire and the row will be retried


def main() -> None:
    """Run the delivery worker with the same production wiring as evaluation.

    The standalone entry point is intentionally configuration-compatible with
    ``backend.workflows.worker_entry``.  It does not create tables or fall
    back to a local SQLite file: schema creation belongs to migrations and a
    missing/invalid production database configuration is an operator error.
    """

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from .repository import SqlAlchemyNotificationRepository
    from backend.workflows.worker_entry import (
        build_delivery_resolver,
        build_subscription_loader,
        resolve_database_url,
    )

    database_url = resolve_database_url()
    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        notification_repo = SqlAlchemyNotificationRepository(session_factory)
        subscription_loader = build_subscription_loader(session_factory)
        resolver = build_delivery_resolver(notification_repo, subscription_loader)
        worker = DeliveryWorker(notification_repo, resolver=resolver)
        asyncio.run(worker.run_forever())
    finally:
        engine.dispose()
