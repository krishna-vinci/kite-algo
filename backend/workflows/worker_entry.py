"""Runnable skeleton entrypoint for the evaluation worker (Task 7).

Usage::

    python -m backend.workflows.worker_entry

Environment:
- ``DATABASE_URL`` (required) — Postgres (or SQLite) DSN for durable state
- ``REDIS_URL`` (required) — Redis for the tick / completed-candle pub/sub
- ``MARKET_RUNTIME_URL`` (optional) — market-runtime HTTP base URL; forwarded
  to the client's own ``MARKET_RUNTIME_HTTP_URL`` variable when set
- ``ALERTS_INSTRUMENT_TOKENS`` (optional compatibility override) — JSON mapping of
  ``"EXCHANGE:SYMBOL"`` to numeric instrument tokens, e.g.
  ``{"NSE:RELIANCE": 738561}``. The worker first resolves active workflow
  symbols through the shared PostgreSQL catalog. This mapping is a
  compatibility fallback ONLY when the catalog is uninitialized
  (bootstrap/development). Once the catalog is initialized, authoritative
  rejections (retired/expired/ambiguous/not-found) are never bypassed unless
  ``ALERTS_INSTRUMENT_TOKEN_FALLBACK=always`` is explicitly set (loud,
  logged, and still never applied to retired/expired records).
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
import inspect
import json
import logging
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


from backend.database_url import resolve_database_url as _resolve_database_url

from backend.shared.runtime_stats import LagRecorder, run_stats_sampler

logger = logging.getLogger("backend.workflows.worker_entry")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ``make_resolver`` is a PINNED contract being added to
# backend.notifications.worker by a parallel change. Import it when present;
# otherwise the minimal local fallback below keeps this entrypoint (and its
# tests) working until the real resolver lands.
try:  # pragma: no cover - depends on the parallel agent's landing order
    from backend.notifications.worker import make_resolver as _pinned_make_resolver
except ImportError:  # pragma: no cover
    _pinned_make_resolver = None

_OFF_VALUES = {"0", "false", "no", "off"}


def resolve_database_url(environ: Optional[dict[str, str]] = None) -> str:
    """Resolve the alerts DB exactly like the Compose/API settings.

    ``DATABASE_URL`` wins. Otherwise the conventional ``DB_*`` variables are
    assembled into a URL with credentials percent-encoded, so passwords such
    as ``p@ss/word`` cannot change the host, port, or database path.
    """
    return _resolve_database_url(
        environ,
        require_all=True,
        driver="postgresql+psycopg2",
    )


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
        tokens: Dict[str, int] = {}
        for key, value in data.items():
            instrument_key = str(key).strip()
            if instrument_key.count(":") != 1 or any(not part.strip() for part in instrument_key.split(":", 1)):
                logger.error("invalid instrument key in ALERTS_INSTRUMENT_TOKENS: %r", key)
                continue
            token = int(value)
            if token <= 0:
                logger.error("instrument token must be positive for %s", instrument_key)
                continue
            tokens[instrument_key] = token
        return tokens
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


def resolve_catalog_instrument_tokens(
    instrument_keys: set[str],
    session_factory: Callable[[], Any],
    *,
    fallback_tokens: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Resolve active alert symbols through the shared catalog.

    Rejection classes (C2):

    - ``active`` catalog record -> the resolved broker token.
    - ``expired`` / ``retired`` / ambiguous record -> authoritative rejection;
      the environment map is NEVER applied.
    - not found while the catalog IS initialized -> authoritative rejection by
      default. The environment map applies only when
      ``ALERTS_INSTRUMENT_TOKEN_FALLBACK=always`` (explicit, loud compat mode).
    - not found while the catalog is UNINITIALIZED (bootstrap/development) ->
      the environment map applies with a warning.
    - catalog unavailable (database down) -> raises ``CatalogUnavailableError``
      unless the explicit ``always`` fallback is set; callers must keep their
      current bindings and retry on the next refresh pass.

    Returns ``(resolved, rejected)`` where ``rejected`` maps instrument keys
    to a machine-readable reason.
    """
    from backend.broker_api.instruments.catalog import (
        AmbiguousInstrumentError,
        CatalogUnavailableError,
        InstrumentCatalog,
        InstrumentNotFoundError,
    )

    fallback_tokens = {
        str(key).strip().upper(): int(token)
        for key, token in (fallback_tokens or {}).items()
    }
    policy = os.environ.get("ALERTS_INSTRUMENT_TOKEN_FALLBACK", "").strip().lower()
    catalog = InstrumentCatalog(db=session_factory)
    resolved: Dict[str, int] = {}
    rejected: Dict[str, str] = {}
    catalog_initialized: Optional[bool] = None

    def _initialized() -> bool:
        nonlocal catalog_initialized
        if catalog_initialized is None:
            try:
                catalog_initialized = catalog.health().get("status") != "uninitialized"
            except CatalogUnavailableError:
                raise
        return catalog_initialized

    def _fallback_allowed() -> bool:
        return policy == "always"

    for instrument_key in sorted({str(key).strip().upper() for key in instrument_keys if str(key).strip()}):
        try:
            descriptor = catalog.resolve_public_key(instrument_key)
        except CatalogUnavailableError:
            if _fallback_allowed() and instrument_key in fallback_tokens:
                logger.warning(
                    "catalog unavailable; ALERTS_INSTRUMENT_TOKEN_FALLBACK=always "
                    "applies compatibility token for %s",
                    instrument_key,
                )
                resolved[instrument_key] = fallback_tokens[instrument_key]
                continue
            raise
        except AmbiguousInstrumentError as exc:
            logger.error(
                "catalog returned ambiguous active alert instrument %s: %s",
                instrument_key, exc,
            )
            rejected[instrument_key] = "ambiguous"
            continue
        except InstrumentNotFoundError:
            lifecycle = None
            try:
                lifecycle = catalog.lifecycle_for_public_key(instrument_key)
            except CatalogUnavailableError:
                lifecycle = None
            if lifecycle == "retired":
                logger.error(
                    "catalog instrument %s is retired; refusing compatibility token",
                    instrument_key,
                )
                rejected[instrument_key] = "retired"
                continue
            if lifecycle == "expired":
                logger.error(
                    "catalog instrument %s is expired; refusing compatibility token",
                    instrument_key,
                )
                rejected[instrument_key] = "expired"
                continue
            if lifecycle is not None:
                logger.error(
                    "catalog instrument %s has non-active lifecycle %s",
                    instrument_key, lifecycle,
                )
                rejected[instrument_key] = lifecycle
                continue
            # Genuinely absent from the records table.
            if _fallback_allowed() and instrument_key in fallback_tokens:
                logger.warning(
                    "catalog has no record for %s; "
                    "ALERTS_INSTRUMENT_TOKEN_FALLBACK=always applies compatibility token",
                    instrument_key,
                )
                resolved[instrument_key] = fallback_tokens[instrument_key]
            elif _fallback_allowed():
                logger.error("catalog could not resolve active alert instrument %s: not found", instrument_key)
                rejected[instrument_key] = "not_found"
            elif not _initialized():
                if instrument_key in fallback_tokens:
                    logger.warning(
                        "catalog is uninitialized; applying ALERTS_INSTRUMENT_TOKENS "
                        "compatibility fallback for %s",
                        instrument_key,
                    )
                    resolved[instrument_key] = fallback_tokens[instrument_key]
                else:
                    logger.error("catalog could not resolve active alert instrument %s: not found", instrument_key)
                    rejected[instrument_key] = "not_found"
            else:
                logger.error(
                    "catalog could not resolve active alert instrument %s: not found "
                    "(initialized catalog; compatibility fallback refused)",
                    instrument_key,
                )
                rejected[instrument_key] = "not_found"
            continue
        if descriptor.lifecycle_status != "active":
            logger.error(
                "catalog instrument %s is not active: %s",
                instrument_key,
                descriptor.lifecycle_status,
            )
            rejected[instrument_key] = descriptor.lifecycle_status
            continue
        resolved[instrument_key] = descriptor.broker_token
    return resolved, rejected


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
        except Exception as exc:
            logger.error(
                "subscription loader failed for %s", subscription_id, exc_info=True,
            )
            # A missing row is a permanent context problem. A database or
            # transaction failure is not: let the delivery worker record an
            # unknown attempt and retry without losing the outbox row.
            raise RuntimeError("temporary subscription context failure") from exc

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


def build_renewal(client: Any, owner_id: str, bindings: Any) -> Callable[[], Any]:
    """Ownership renewal callable for EvaluationWorker (same client as start).

    ``bindings`` is an :class:`InstrumentBindingRegistry`: every renewal reads
    the CURRENT accepted snapshot, so a binding added after startup is
    subscribed on market-runtime within one renewal interval (C1).
    """

    async def renew() -> None:
        snapshot = bindings.snapshot() if hasattr(bindings, "snapshot") else dict(bindings)
        if not snapshot:
            return
        await client.set_owner_subscriptions(
            owner_id, {int(token): "full" for token in snapshot.values()}
        )

    return renew


@dataclass
class _SupervisedTask:
    """One required long-running task under supervision."""

    name: str
    factory: Any                      # () -> awaitable
    teardown: Any = None              # () -> awaitable, run before a restart
    required: bool = True


async def supervise(
    worker: Any,
    delivery_worker: Any,
    *,
    stop: Any,
    delivery_poll_interval_s: float = 2.0,
    screener_scheduler: Any = None,
    restart_backoff_s: Optional[float] = None,
    max_consecutive_failures: Optional[int] = None,
    release_owner: Any = None,
) -> list:
    """Run the required tasks until ``stop``, restarting crashes with backoff.

    Phase 6 6A.0 replaces the previous behavior, which cancelled EVERY task as
    soon as any one finished and only logged the error. That meant a single
    malformed workflow could stop evaluation, delivery and screeners together,
    and the process still exited 0 — so the crash looked like a clean shutdown.

    Now a crashed task is contained: its failure is recorded, its resources are
    torn down (the worker's own ``stop()`` plus the market-runtime owner
    release, so the replacement does not leave a second owner streaming the
    same tokens), and a replacement starts after capped exponential backoff.
    Restarts stop only after ``max_consecutive_failures`` in a row, at which
    point the task is reported as failed and the caller can exit non-zero.
    Required-task liveness is published into worker health throughout.
    """
    if restart_backoff_s is None:
        try:
            restart_backoff_s = float(os.environ.get("ALERTS_TASK_RESTART_BACKOFF_S", "5"))
        except ValueError:
            restart_backoff_s = 5.0
    if max_consecutive_failures is None:
        try:
            max_consecutive_failures = int(
                os.environ.get("ALERTS_TASK_MAX_CONSECUTIVE_FAILURES", "3")
            )
        except ValueError:
            max_consecutive_failures = 3

    specs: List[_SupervisedTask] = [
        _SupervisedTask(
            "evaluation-worker",
            worker.run,
            getattr(worker, "stop", None),
            required=True,
        )
    ]
    if screener_scheduler is not None:
        specs.append(
            _SupervisedTask(
                "screener-scheduler",
                screener_scheduler.run,
                getattr(screener_scheduler, "stop", None),
                required=True,
            )
        )
    if delivery_worker is not None and delivery_enabled():
        specs.append(
            _SupervisedTask(
                "delivery-worker",
                lambda: delivery_worker.run_forever(delivery_poll_interval_s),
                getattr(delivery_worker, "stop", None),
                required=True,
            )
        )
    elif delivery_worker is not None:
        logger.warning(
            "ALERTS_DELIVERY_ENABLED is off: delivery task not started; "
            "pending deliveries will NOT be sent"
        )

    live: Dict[asyncio.Task, _SupervisedTask] = {}
    failures: Dict[str, int] = {spec.name: 0 for spec in specs}
    exit_reasons: Dict[str, Any] = {}

    def _launch(spec: _SupervisedTask) -> None:
        _schedule_liveness(
            worker, spec.name,
            alive=True,
            state="running",
            last_started_at=_utcnow().isoformat(),
            restarts=failures[spec.name],
        )
        task = asyncio.create_task(spec.factory(), name=spec.name)
        live[task] = spec

    for spec in specs:
        _launch(spec)

    if hasattr(stop, "wait"):
        watcher = asyncio.create_task(stop.wait(), name="stop-watcher")
    else:  # Future-like (tests): already scheduled on the loop
        watcher = asyncio.ensure_future(stop)

    stopping = False
    try:
        while not stopping and live:
            done, _pending = await asyncio.wait(
                list(live.keys()) + [watcher], return_when=asyncio.FIRST_COMPLETED
            )
            if watcher in done:
                stopping = True
                break
            # Snapshot before mutating: a restart adds a new task to ``live``.
            for task in [t for t in done if t in live]:
                spec = live.pop(task)
                if task.cancelled():
                    reason = "cancelled"
                    exc: Optional[BaseException] = None
                else:
                    exc = task.exception()
                    reason = (
                        f"{type(exc).__name__}: {exc}" if exc else "ended unexpectedly"
                    )
                exit_reasons[spec.name] = reason
                failures[spec.name] += 1
                logger.error(
                    "worker task %s ended (%s); failure %d of %d",
                    spec.name, reason, failures[spec.name], max_consecutive_failures,
                    exc_info=exc,
                )
                # Tear the failed instance down BEFORE any replacement starts:
                # stop its feed sources and release the market-runtime owner,
                # so a restart never leaves the previous owner streaming the
                # same tokens for the remainder of its lease.
                for teardown in (
                    spec.teardown,
                    release_owner if spec.name == "evaluation-worker" else None,
                ):
                    if teardown is None:
                        continue
                    try:
                        result = teardown()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.warning(
                            "teardown for %s failed", spec.name, exc_info=True
                        )
                if failures[spec.name] > max_consecutive_failures:
                    logger.error(
                        "worker task %s exceeded %d consecutive failures; not restarting",
                        spec.name, max_consecutive_failures,
                    )
                    _schedule_liveness(
                        worker, spec.name,
                        alive=False, state="failed",
                        restarts=failures[spec.name],
                        last_exit_reason=reason,
                    )
                    continue
                backoff = min(
                    restart_backoff_s * (2 ** (failures[spec.name] - 1)), 300.0
                )
                _schedule_liveness(
                    worker, spec.name,
                    alive=False, state="backing_off",
                    backoff_s=backoff,
                    restarts=failures[spec.name],
                    last_exit_reason=reason,
                )
                logger.warning(
                    "restarting %s in %.1fs", spec.name, backoff
                )
                try:
                    # Interruptible: a stop during backoff must not wait it out.
                    await asyncio.wait_for(asyncio.shield(watcher), timeout=backoff)
                    stopping = True
                    break
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                _launch(spec)
    finally:
        watcher.cancel()
        for task in list(live.keys()):
            task.cancel()
        await asyncio.gather(watcher, *list(live.keys()), return_exceptions=True)
        for spec in specs:
            _schedule_liveness(
                worker, spec.name,
                alive=False,
                state="stopped" if not exit_reasons.get(spec.name) else "failed",
                restarts=failures[spec.name],
                last_exit_reason=exit_reasons.get(spec.name),
            )
    logger.info("all worker tasks stopped")
    return [
        (spec.name, exit_reasons.get(spec.name)) for spec in specs
    ]


def _schedule_liveness(worker: Any, name: str, **fields: Any) -> None:
    """Best-effort synchronous publish of task liveness into health."""
    state = getattr(worker, "_task_state", None)
    if not isinstance(state, dict):
        return
    entry = dict(state.get(name) or {})
    entry.update(fields)
    state[name] = entry


def task_failures(results: Any) -> List[str]:
    """Names of supervised tasks that ended with a failure (for the exit code)."""
    failed: List[str] = []
    for item in results or ():
        try:
            name, reason = item
        except (TypeError, ValueError):
            continue
        if reason:
            failed.append(str(name))
    return failed


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


async def _log_market_runtime_cache_generation() -> None:
    """Log the generation Go's cache currently serves (C3 observability).

    A stale Go cache means new bindings subscribe on a store that cannot
    route their ticks yet; surfacing the generation beside the binding
    revision makes that mismatch explicit instead of silent.
    """
    try:
        import httpx

        base = os.environ.get("MARKET_RUNTIME_URL", "").strip()
        if not base:
            return
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/internal/market-runtime/instruments/health")
            if resp.status_code == 200:
                payload = resp.json()
                logger.info(
                    "market-runtime instrument cache: generation=%s count=%s",
                    payload.get("generation"), payload.get("count"),
                )
    except Exception:
        logger.debug("market-runtime instrument health check unavailable", exc_info=True)


async def main(extra_tokens: Optional[Dict[str, int]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        database_url = resolve_database_url()
    except ValueError as exc:
        logger.error("invalid alerts database configuration: %s", exc)
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
    from backend.workflows.external_context import ExternalSignalLoader
    from backend.workflows.fundamentals_context import FundamentalsLoader
    from backend.workflows.instrument_bindings import InstrumentBindingRegistry
    from backend.workflows.repository import SqlAlchemyWorkflowRepository
    from backend.workflows.runtime import (
        EvaluationWorker,
        MARKET_RUNTIME_OWNER_LEASE_TTL_S,
        PgCandleHistory,
        RedisCandleSource,
        RedisTickSource,
        build_market_session_provider,
    )

    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    workflow_repo = SqlAlchemyWorkflowRepository(session_factory)
    notification_repo = SqlAlchemyNotificationRepository(session_factory)
    configured_tokens = build_instrument_tokens()
    # C1: the registry is the ONLY mutable binding state. It starts from the
    # accepted catalog resolutions (env map only per the C2 fallback policy),
    # never from a pre-seeded environment dump, so a stale environment token
    # cannot silently bind an instrument the catalog rejects.
    bindings = InstrumentBindingRegistry()
    active_keys = {
        str(sub.instrument_key).strip().upper()
        for sub in workflow_repo.list_active_subscriptions()
        if str(sub.instrument_key).strip()
    }
    resolved_tokens, rejected_tokens = resolve_catalog_instrument_tokens(
        active_keys,
        session_factory,
        fallback_tokens=configured_tokens,
    )
    bindings.apply(resolved_tokens, set(rejected_tokens))
    for reason_key, reason in sorted(rejected_tokens.items()):
        logger.warning(
            "instrument %s rejected by catalog (%s); its rules stay silent",
            reason_key, reason,
        )
    if extra_tokens:
        bindings.apply(extra_tokens, set())
    instrument_tokens = bindings.snapshot()
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

    async def sync_market_runtime_snapshot(_change: Any = None) -> None:
        """Best-effort (re)registration of the CURRENT binding snapshot."""
        await _sync_market_runtime_subscriptions(runtime_owner_id, bindings.snapshot())
        await _log_market_runtime_cache_generation()

    await sync_market_runtime_snapshot()

    # Phase 6 6A.0 LTP freshness bounds. Defaults live HERE (not in the source)
    # so a directly-constructed RedisTickSource keeps its historical behavior,
    # while production always gets the bounds. ALERTS_LTP_FRESHNESS_ENABLED=false
    # is the kill switch: it disables the tick-level bounds AND the service-side
    # silence-gap invalidation without a redeploy.
    ltp_freshness_enabled = (
        os.environ.get("ALERTS_LTP_FRESHNESS_ENABLED", "true").strip().lower()
        not in ("0", "false", "no", "off")
    )
    try:
        ltp_max_tick_age_s = float(os.environ.get("ALERTS_LTP_MAX_TICK_AGE_S", "300"))
    except ValueError:
        ltp_max_tick_age_s = 300.0
    try:
        ltp_max_future_skew_s = float(
            os.environ.get("ALERTS_LTP_MAX_FUTURE_SKEW_S", "300")
        )
    except ValueError:
        ltp_max_future_skew_s = 300.0
    try:
        ltp_max_gap_s = float(os.environ.get("ALERTS_LTP_MAX_GAP_S", "300"))
    except ValueError:
        ltp_max_gap_s = 300.0
    if not ltp_freshness_enabled:
        ltp_max_tick_age_s = 0.0
        ltp_max_future_skew_s = 0.0

    tick_lag = LagRecorder()

    def _stats_extras() -> Dict[str, Any]:
        return {"tick_lag": tick_lag.snapshot()}

    def tick_source_factory(instrument_key: str) -> RedisTickSource:
        token = bindings.get(instrument_key)
        mapping = {token: instrument_key} if token is not None else {}
        # each call returns a NEW source (fresh uuid epoch): the worker
        # relies on that to re-initialize ltp rules after a feed outage (D2)
        return RedisTickSource(
            redis_client,
            mapping,
            max_tick_age_s=ltp_max_tick_age_s or None,
            max_future_skew_s=ltp_max_future_skew_s or None,
            lag_recorder=tick_lag,
        )

    def candle_source_factory(instrument_key: str, timeframe: str) -> RedisCandleSource:
        token = bindings.get(instrument_key)
        mapping = {token: instrument_key} if token is not None else {}
        return RedisCandleSource(redis_client, mapping, interval=timeframe)

    # market-runtime owner leases expire after ~TTLs without renewal; renew
    # every TTL/3 with the SAME client used at startup. The renewal is built
    # even with zero bindings: the first later activation must establish
    # subscriptions and renewal without a process restart (C1).
    try:
        lease_ttl_s = float(os.environ.get(
            "MARKET_RUNTIME_OWNER_LEASE_TTL_SEC", MARKET_RUNTIME_OWNER_LEASE_TTL_S,
        ))
    except ValueError:
        lease_ttl_s = MARKET_RUNTIME_OWNER_LEASE_TTL_S
    renewal_interval_s = max(1.0, lease_ttl_s / 3.0)

    renewal = None
    try:
        from backend.broker_api.orders.market_runtime_client import (
            get_market_runtime_client,
        )

        market_client = await get_market_runtime_client()
        renewal = build_renewal(market_client, runtime_owner_id, bindings)
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
        candle_history=PgCandleHistory(engine, bindings),
        poll_interval_s=float(os.environ.get("ALERTS_POLL_INTERVAL_S", "2.0")),
        refresh_interval_s=float(os.environ.get("ALERTS_REFRESH_INTERVAL_S", "10")),
        renewal=renewal,
        renewal_interval_s=renewal_interval_s,
        health_interval_s=float(os.environ.get("ALERTS_HEALTH_INTERVAL_S", "30")),
        session_provider=build_market_session_provider(engine),
        owner_id=runtime_owner_id,
        ownership_lease_s=lease_ttl_s,
        binding_registry=bindings,
        bindings_changed=sync_market_runtime_snapshot,
        instrument_resolver=lambda keys: resolve_catalog_instrument_tokens(
            keys,
            session_factory,
            fallback_tokens=configured_tokens,
        ),
        fundamentals_loader=FundamentalsLoader(session_factory),
        fundamentals_stale_hours=float(
            os.environ.get("ALERTS_FUNDAMENTALS_STALE_HOURS", "168")
        ),
        # Phase 4 F10: registered external producer values, sampled at each
        # observation's event time (never pushed).
        external_loader=ExternalSignalLoader(session_factory),
        pair_max_bar_age_s=float(
            os.environ.get("ALERTS_PAIR_MAX_BAR_AGE_S", "0")
        ),
        # Phase 6 6A.0: service-side half of the LTP freshness policy (the
        # silence-gap continuity invalidation).
        ltp_freshness_enabled=ltp_freshness_enabled,
        ltp_max_gap_s=ltp_max_gap_s,
    )

    # Phase 2 (F7): universe membership resolution wired into the refresh
    # pass; resolution cadence is decoupled from subscription refresh.
    try:
        from backend.workflows.universes import UniverseService

        worker.universe_service = UniverseService(session_factory)
        worker.universe_resolve_interval_s = max(
            30.0, float(os.environ.get("ALERTS_UNIVERSE_RESOLVE_INTERVAL_S", "300"))
        )
    except Exception:
        logger.warning("universe service unavailable; universe workflows inert", exc_info=True)

    # Phase 3 (F9): scheduled screener execution. Shares the universe
    # service, fundamentals loader and catalog-aware candle history with the
    # evaluation worker; occurrences are claimed in Postgres so concurrent
    # workers never duplicate a logical run.
    screener_scheduler = None
    try:
        from backend.screeners.candle_warming import build_screener_warmer
        from backend.screeners.runner import ScreenerPipeline
        from backend.screeners.scheduler import ScreenerScheduler
        from backend.workflows.screener_repository import ScreenerRunRepository

        fundamentals_loader = FundamentalsLoader(session_factory)
        screener_window_bars = int(os.environ.get("ALERTS_SCREENER_WINDOW_BARS", "120"))
        screener_history = PgCandleHistory(engine, bindings)
        screener_scheduler = ScreenerScheduler(
            session_factory=session_factory,
            workflow_repo=workflow_repo,
            run_repo=ScreenerRunRepository(session_factory),
            pipeline=ScreenerPipeline(
                candle_history=screener_history,
                window_bars=screener_window_bars,
                # Scheduled runs warm their own universe members, bounded, so a
                # universe whose daily history was never ingested converges
                # instead of reporting "unavailable" forever.
                warmer=build_screener_warmer(
                    screener_history, required_bars=screener_window_bars
                ),
            ),
            universe_service=worker.universe_service,
            fundamentals_loader=fundamentals_loader,
            channel_resolver=channel_resolver,
            session_gate=(
                lambda at, _provider=build_market_session_provider(engine): _provider(
                    "nse_equity", "NSE:SCREENER", at
                )
            ),
            owner_id=f"{runtime_owner_id}:screener",
            poll_interval_s=float(os.environ.get("ALERTS_SCREENER_POLL_INTERVAL_S", "30")),
            lease_ttl_s=float(os.environ.get("ALERTS_SCREENER_LEASE_TTL_S", "300")),
            max_events_per_attachment=int(
                os.environ.get("ALERTS_SCREENER_MAX_ATTACHMENT_EVENTS", "100")
            ),
        )
    except Exception:
        logger.warning("screener scheduler unavailable; screeners inert", exc_info=True)

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

    stats_task = asyncio.create_task(
        run_stats_sampler(
            logger,
            interval_s=float(os.environ.get("ALERTS_STATS_INTERVAL_S", "60")),
            stop=stop_signal,
            extras=_stats_extras,
            component="alerts-worker",
        ),
        name="runtime-stats-sampler",
    )
    results = await supervise(
        worker,
        delivery_worker,
        stop=stop_signal,
        delivery_poll_interval_s=float(os.environ.get("ALERTS_DELIVERY_POLL_INTERVAL_S", "2.0")),
        screener_scheduler=screener_scheduler,
        # A restart must release this worker's market-runtime owner before the
        # replacement subscribes: otherwise the old owner keeps streaming the
        # same tokens until its lease expires (~90 s), doubling feed load.
        release_owner=lambda: _sync_market_runtime_subscriptions_runtime_cleanup(
            runtime_owner_id
        ),
    )
    stats_task.cancel()
    try:
        await stats_task
    except asyncio.CancelledError:
        pass
    await _sync_market_runtime_subscriptions_runtime_cleanup(runtime_owner_id)
    engine.dispose()
    # Phase 6 6A.0: a task that ended in failure must NOT look like a clean
    # shutdown. Exiting 0 here is what let a crash-looping worker be reported as
    # healthy by an orchestrator that only inspects the exit code.
    failed = task_failures(results)
    if failed:
        logger.error("evaluation worker stopped with failed task(s): %s", ", ".join(failed))
        return 1
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
