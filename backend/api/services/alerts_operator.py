"""Shared service layer for the app-authenticated alerts operator API.

Two responsibilities live here, both of which the operator router (and only the
operator router) depends on:

1. **Server-side scope authorization.** The alerts platform keys every row by an
   ``owner_id`` derived from a WORKER TOKEN's ``account_scope``
   (``_owner_id_for_token``). The browser authenticates with an app cookie
   instead, so it has no such scope — and a scope arriving from the browser must
   never be trusted, because it would let any authenticated session read any
   owner's alerts. :func:`authorize_scope` is the single place that decides: the
   client's requested scope is a SELECTION, the server's allowlist is the
   AUTHORITY, and anything outside it is refused outright.

2. **Read models** the operator UI needs that do not exist on the worker
   surface (delivery history with attempt outcomes, workflow list enrichment).

Keeping this out of the router makes it testable without HTTP and keeps the
worker routers untouched — the SDK/MCP contract does not move.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException, Request
from sqlalchemy import func, select

from backend.app.auth import AppUser, require_app_user

__all__ = [
    "ALERTS_OPERATOR_OWNER_ENV",
    "ALERTS_OPERATOR_SCOPES_ENV",
    "OperatorScope",
    "allowed_scopes",
    "authorize_scope",
    "default_scope",
    "list_scope_options",
    "require_operator_scope",
]


ALERTS_OPERATOR_SCOPES_ENV = "ALERTS_OPERATOR_SCOPES"
ALERTS_OPERATOR_OWNER_ENV = "ALERTS_OPERATOR_OWNER"


def _split_scopes(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _app_scope(user: AppUser) -> str:
    """The scope an app user owns by default.

    ``app:<username>`` is deliberately distinct from any token scope: a browser
    session and a worker token are different identities, and silently treating
    them as the same owner would hand the browser a token's data without an
    explicit decision.
    """
    return f"app:{user.username or 'operator'}"


def allowed_scopes(user: AppUser) -> List[str]:
    """Every owner_id this app user may act as, in a stable order.

    Configured via ``ALERTS_OPERATOR_SCOPES`` (comma-separated). When unset, the
    only scope is the user's own ``app:<username>`` — so the default is
    least-privilege, and reading SDK-created alerts under a token scope requires
    an explicit configuration decision rather than happening by accident.
    """
    configured = _split_scopes(os.environ.get(ALERTS_OPERATOR_SCOPES_ENV, ""))
    if configured:
        return list(dict.fromkeys(configured))
    return [_app_scope(user)]


def default_scope(user: AppUser) -> str:
    """The scope used when the client expresses no preference.

    ``ALERTS_OPERATOR_OWNER`` wins when set (and is required to be authorized —
    a misconfigured default must not silently widen access). Otherwise the first
    authorized scope.
    """
    authorized = allowed_scopes(user)
    configured_default = (os.environ.get(ALERTS_OPERATOR_OWNER_ENV) or "").strip()
    if configured_default and configured_default in authorized:
        return configured_default
    return authorized[0]


def authorize_scope(user: AppUser, requested: Optional[str]) -> str:
    """The authorized owner_id for this request, or 403.

    ``requested`` is whatever the client sent (query/header/body). It is a
    preference, never a credential: outside the allowlist it is refused with 403
    even when the scope exists and holds data, because "there is data there" is
    not a reason a session may read it.
    """
    authorized = allowed_scopes(user)
    candidate = (requested or "").strip()
    if not candidate:
        return default_scope(user)
    if candidate not in authorized:
        raise HTTPException(
            status_code=403,
            detail=(
                f"scope {candidate!r} is not authorized for this operator; "
                f"allowed: {', '.join(authorized)}"
            ),
        )
    return candidate


async def require_operator_scope(request: Request) -> str:
    """FastAPI dependency: app-cookie auth + authorized scope resolution.

    Used as a DEPENDENCY (not called inside handlers) so it runs before request
    validation and cannot be forgotten by a new route. The scope may be supplied
    per request as ``?scope=``; anything else is refused.
    """
    user = require_app_user(request)
    requested = request.query_params.get("scope")
    if requested is None:
        requested = request.headers.get("x-alerts-scope")
    return authorize_scope(user, requested)


@dataclass(frozen=True)
class OperatorScope:
    """One selectable scope and whether it currently holds alerts data."""

    scope: str
    is_default: bool
    has_data: bool


def list_scope_options(
    user: AppUser, session_factory: Any
) -> List[OperatorScope]:
    """The AUTHORIZED scopes plus which of them hold data.

    Only authorized scopes are ever returned, so the UI cannot learn about data
    it may not read — the picker exists to avoid a confusingly empty page for
    SDK-created resources, not to widen access.
    """
    from backend.notifications.repository import ChannelReference
    from backend.workflows.repository import Workflow as WorkflowModel

    authorized = allowed_scopes(user)
    default = default_scope(user)
    populated: set = set()
    with session_factory() as session:
        for column in (
            WorkflowModel.owner_id,
            ChannelReference.owner_id,
        ):
            rows = session.execute(
                select(column).where(column.in_(authorized)).distinct()
            ).scalars().all()
            populated.update(str(row) for row in rows)
    return [
        OperatorScope(
            scope=scope, is_default=(scope == default), has_data=scope in populated
        )
        for scope in authorized
    ]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
