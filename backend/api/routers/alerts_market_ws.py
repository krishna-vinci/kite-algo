"""Cookie-authenticated operator live-price websocket for the alerts UI.

One socket per browser tab, authenticated exactly like the other operator routes
(app session cookie + authorized scope), but with the checks a websocket needs
and an HTTP dependency cannot give it:

* **Origin** — a websocket handshake carries cookies, so a foreign page could
  otherwise open an authenticated stream. The Origin must be one of the
  configured application origins; a missing Origin is refused too, because the
  only legitimate consumer is a browser.
* **Scope** — resolved through the same server-side allowlist the operator HTTP
  routes use (the client's value is a selection, never an authority), and a
  refusal never names what the caller may not see.

The route itself contains no market logic: it authenticates, then hands the
socket to :class:`~backend.alerts.market_stream.OperatorMarketStream`, which
registers a subscription owner with the running market-runtime (the single
holder of the broker connection) and streams the normalized ticks it publishes.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, WebSocket

from backend.alerts.market_stream import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    OperatorMarketStream,
    get_market_stream_hub,
)
from backend.api.services.alerts_operator import authorize_scope
from backend.app.auth import get_optional_app_user
from backend.app.config import get_allowed_cors_origins

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts/market", tags=["Alerts (operator)"])

__all__ = ["router", "origin_allowed"]


def _normalize_origin(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def origin_allowed(origin: Optional[str]) -> bool:
    """Whether a websocket handshake may proceed from this Origin.

    Mirrors ``backend.api.services.csrf`` (the same allowlist the unsafe-method
    check uses) so the browser has one rule to satisfy, not two.
    """
    normalized = _normalize_origin(origin)
    if normalized is None:
        return False
    allowed = {_normalize_origin(entry) for entry in get_allowed_cors_origins()}
    return normalized in allowed


@router.websocket("/ws")
async def alerts_market_ws(
    websocket: WebSocket,
    scope: Optional[str] = Query(None),
) -> None:
    """Stream live quotes for the instruments this browser currently shows."""
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin):
        # Refuse before the handshake completes: a foreign page learns nothing,
        # not even whether the app has a stream.
        logger.info("alerts_market_ws_origin_refused", extra={"origin": origin or "absent"})
        await websocket.close(code=CLOSE_FORBIDDEN, reason="origin not allowed")
        return

    user = get_optional_app_user(websocket)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="app authentication required")
        return

    try:
        authorized_scope = authorize_scope(user, scope)
    except HTTPException:
        await websocket.close(code=CLOSE_FORBIDDEN, reason="scope not authorized")
        return

    await websocket.accept()
    hub = await get_market_stream_hub()
    stream = OperatorMarketStream(websocket, hub=hub, scope=authorized_scope)
    try:
        await stream.run()
    except Exception:
        logger.exception("alerts_market_ws_failed", extra={"scope": authorized_scope})
        try:
            await websocket.close(code=1011, reason="stream failed")
        except Exception:
            pass
