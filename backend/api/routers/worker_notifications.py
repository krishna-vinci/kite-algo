"""Worker API for notification channels (Alerts Platform Phase 1, Task 8).

Endpoints under ``/worker/notification-channels`` (mounted at ``/api``).
- ``GET  /worker/notification-channels``            → workflows:read
- ``POST /worker/notification-channels``            → workflows:write (upsert)
- ``POST /worker/notification-channels/{id}/test``  → notifications:test

The test endpoint builds the destination through ONE code path
(:func:`build_test_destination`) implementing the pinned contract: a channel
with ``secret_env`` set overrides the provider's env-var slot in its own
destination — telegram gets ``{"token_env": channel.secret_env}``, ntfy gets
``{"url_env": channel.secret_env}`` — and the resolved variable is pre-checked
against ``os.environ`` so a test send fails with a 400 naming the exact
variable (never a provider default). The secret itself never enters the
database or the response; the send goes through the provider adapter from
``backend.notifications.adapters``. Adapter outcomes are classified values
(``accepted`` / ``retryable`` / ``permanent`` / ``unknown``), never raises for
provider problems.

Shares the injectable alerts sessionmaker dependency from
``backend.api.routers.worker_workflows`` so tests override a single point.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.routers.worker_shared import _require_action, require_worker_token
from backend.api.routers.worker_workflows import (
    _alerts_db,
    _authorize,
    _notification_repository,
    serialize_channel,
)
from backend.api.schemas.workflows import (
    ChannelCreateRequest,
    ChannelListResponse,
    ChannelResponse,
    ChannelTestRequest,
    ChannelTestResponse,
)
from backend.notifications.adapters import get_adapter
from backend.notifications.repository import SqlAlchemyNotificationRepository

router = APIRouter(prefix="/worker/notification-channels", tags=["Worker Notification Channels"])

__all__ = [
    "router",
    "build_test_destination",
]


def build_test_destination(channel: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """PINNED test-send destination contract (single code path).

    Returns ``(destination, secret_env)``: the stored destination with the
    channel's ``secret_env`` overriding the provider's env-var slot —
    ``token_env`` for telegram, ``url_env`` for ntfy — and the resolved env
    variable name (None when the channel carries no secret reference).
    """
    secret_env = str(getattr(channel, "secret_env", "") or "").strip() or None
    destination = dict(getattr(channel, "destination", None) or {})
    provider = str(getattr(channel, "provider", "") or "").strip().lower()
    if secret_env and provider == "telegram":
        destination["token_env"] = secret_env
    elif secret_env and provider == "ntfy":
        destination["url_env"] = secret_env
    return destination, secret_env


async def list_channels(
    request: Request,
    notification_repo: SqlAlchemyNotificationRepository = Depends(_notification_repository),
):
    _, owner_id = await _authorize(request, "workflows:read")
    channels = notification_repo.list_channels(owner_id)
    return ChannelListResponse(channels=[serialize_channel(channel) for channel in channels])


async def upsert_channel(
    request: Request,
    payload: ChannelCreateRequest,
    notification_repo: SqlAlchemyNotificationRepository = Depends(_notification_repository),
):
    _, owner_id = await _authorize(request, "workflows:write")
    name = str(payload.name or "").strip()
    provider = str(payload.provider or "").strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="channel name is required")
    if not provider:
        raise HTTPException(status_code=422, detail="channel provider is required")
    secret_env = (payload.secret_env or "").strip() or None
    channel = notification_repo.upsert_channel(
        owner_id,
        name,
        provider,
        dict(payload.destination or {}),
        secret_env,
        bool(payload.enabled),
    )
    return serialize_channel(channel)


async def test_channel(
    request: Request,
    channel_id: str,
    payload: Optional[ChannelTestRequest] = None,
    notification_repo: SqlAlchemyNotificationRepository = Depends(_notification_repository),
):
    _, owner_id = await _authorize(request, "notifications:test")
    channel = notification_repo.get_channel(channel_id)
    if channel is None or channel.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Notification channel not found")

    destination, secret_env = build_test_destination(channel)

    # Pre-check the SAME variable the destination now names (never a provider
    # default): a missing secret is a configuration error, reported as a 400
    # naming the variable — delivery would record `permanent` analogously
    # instead of dropping silently (E-24).
    if secret_env and os.environ.get(secret_env) is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_env_secret",
                "secret_env": secret_env,
                "message": f"environment variable '{secret_env}' is not set; set it before testing channel '{channel.name}'",
            },
        )

    try:
        adapter = get_adapter(str(channel.provider))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    body = (payload.message if payload is not None else None) or (
        f"[Test] channel '{channel.name}' is wired up correctly."
    )
    outcome = await adapter.send(destination, subject=f"[Test] {channel.name}", body=body)
    return ChannelTestResponse(status=outcome.status, provider_id=outcome.provider_id, detail=outcome.detail)


router.add_api_route("", list_channels, methods=["GET"], response_model=ChannelListResponse)
router.add_api_route("", upsert_channel, methods=["POST"], response_model=ChannelResponse)
router.add_api_route("/{channel_id}/test", test_channel, methods=["POST"], response_model=ChannelTestResponse)
