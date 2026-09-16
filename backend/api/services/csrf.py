"""CSRF / cross-origin protection for cookie-authenticated mutation routes.

The app's session cookies are the credential for every ``/api/alerts/*`` route,
so a cross-site request that carries them would otherwise be able to create,
activate or archive an operator's alerts.

Two layers already exist and are preserved rather than replaced:

- the explicit CORS allowlist (``APP_ALLOWED_CORS_ORIGINS``) with
  ``allow_credentials=True`` — never a wildcard;
- JSON-only request bodies, which force a browser preflight that a
  non-allowlisted origin cannot pass.

Those are not sufficient on their own in the common HTTPS deployment, because
cookies are then issued ``SameSite=None`` (see
``backend.app.auth.issue_auth_cookies``) and the browser stops contributing the
SameSite defense. This module adds the missing server-side check: on an UNSAFE
method, ``Origin`` (falling back to ``Referer``) must match the allowlist when
present.

Deliberately permissive about ABSENCE: a non-browser client (curl, the SDK, a
test harness) sends no ``Origin`` at all, and refusing those would break every
scripted caller without adding protection — the attack this defends against is
a BROWSER being made to send cookies cross-site, and a browser always sends
``Origin`` on an unsafe cross-origin request.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from backend.app.config import get_allowed_cors_origins

__all__ = ["UNSAFE_METHODS", "enforce_same_origin"]

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_of(value: str) -> Optional[str]:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _allowed_origins() -> set:
    allowed = set()
    for entry in get_allowed_cors_origins():
        origin = _origin_of(entry)
        if origin:
            allowed.add(origin.lower())
    return allowed


def enforce_same_origin(request: Request) -> None:
    """Reject a cross-origin unsafe request; allow same-origin and non-browser.

    Raises 403 when an ``Origin``/``Referer`` is present and not allowlisted.
    """
    if request.method.upper() not in UNSAFE_METHODS:
        return
    supplied = request.headers.get("origin") or request.headers.get("referer")
    origin = _origin_of(supplied or "")
    if origin is None:
        # No browser-supplied origin: a scripted client. The cookie is still
        # required, so this is not an auth bypass.
        return
    if origin.lower() in _allowed_origins():
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"cross-origin request refused for this cookie-authenticated route: "
            f"{origin}"
        ),
    )
