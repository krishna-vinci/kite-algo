"""Server-side credential for the hosted-strategy supervisor lifecycle API.

This is a **narrow service credential** for the dedicated ``strategy-runner``
supervisor. It is deliberately none of the following:

- an app/browser session cookie (a cookie authenticates a human operator);
- an ordinary child worker token (that token is minted *for* a run and is handed
  to the child, which must never hold lifecycle authority);
- a database credential (the supervisor holds none and reaches state only
  through this API).

**Configuration.** ``HOSTED_SUPERVISOR_CREDENTIALS`` is a comma-separated list
of accepted credentials. One entry is the normal case; several entries exist so
a credential can be **rotated without downtime**: add the new value, roll the
supervisor, then remove the old value. ``HOSTED_SUPERVISOR_CREDENTIAL`` (single)
is also honoured for a simple install. When neither is set the surface is
**default-deny** (401), so an unconfigured deployment cannot expose job details.

**Transport.** The credential travels in the ``X-Hosted-Supervisor-Credential``
header. It is compared with :func:`hmac.compare_digest` so a wrong guess cannot
be timed. It is never returned to a caller, never written to a response, and
never logged. The lifecycle router is under ``/api/hosted-supervisor`` and is
exempt from the cookie middleware because it authenticates with this credential
instead.
"""

from __future__ import annotations

import hmac
import os
from typing import List

from fastapi import HTTPException, Request

__all__ = [
    "HOSTED_SUPERVISOR_CREDENTIALS_ENV",
    "HOSTED_SUPERVISOR_CREDENTIAL_ENV",
    "HEADER_NAME",
    "accepted_supervisor_credentials",
    "require_supervisor",
    "supervisor_is_configured",
]

HOSTED_SUPERVISOR_CREDENTIALS_ENV = "HOSTED_SUPERVISOR_CREDENTIALS"
HOSTED_SUPERVISOR_CREDENTIAL_ENV = "HOSTED_SUPERVISOR_CREDENTIAL"
HEADER_NAME = "X-Hosted-Supervisor-Credential"


def _split(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def accepted_supervisor_credentials() -> List[str]:
    """Accepted credentials, in a stable order. Empty ⇒ default-deny."""
    values = _split(os.environ.get(HOSTED_SUPERVISOR_CREDENTIALS_ENV, ""))
    single = (os.environ.get(HOSTED_SUPERVISOR_CREDENTIAL_ENV) or "").strip()
    if single:
        values.append(single)
    return list(dict.fromkeys(values))


def supervisor_is_configured() -> bool:
    return bool(accepted_supervisor_credentials())


def _supplied_credential(request: Request) -> str:
    return str(request.headers.get(HEADER_NAME) or "").strip()


def require_supervisor(request: Request) -> None:
    """Reject a missing/invalid supervisor credential before any job state.

    Raises 401 for both "not configured" and "wrong credential" so the surface
    never reveals whether a supervisor credential exists. Non-supervisor
    credentials (cookies, worker bearer tokens) are never accepted here.
    """
    supplied = _supplied_credential(request)
    accepted = accepted_supervisor_credentials()
    if not supplied or not accepted:
        raise HTTPException(status_code=401, detail="Supervisor authentication required")
    for candidate in accepted:
        if hmac.compare_digest(supplied, candidate):
            return
    raise HTTPException(status_code=401, detail="Supervisor authentication required")
