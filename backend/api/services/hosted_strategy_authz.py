"""Server-side authorization for hosted-strategy account scopes.

An ``account_scope`` is which trading account a strategy would act on. It is
**not** owner identity: the owner is derived server-side from the authenticated
app session (``app:<username>``), while the account is a separate, explicitly
authorized selection. To avoid conflating the two, this policy is deliberately
distinct from the alerts operator's ``ALERTS_OPERATOR_SCOPES`` (which authorizes
*owner* scopes, not trading accounts).

Policy: ``HOSTED_STRATEGY_ACCOUNT_SCOPES`` — a comma-separated allowlist of
account scopes this single-operator install may configure a hosted strategy
against. **Default deny**: when unset, no account is authorized, so a
well-shaped but unlisted scope is refused with 403 (and therefore never wrote a
row). Shape/mode validation (``parse_account_scope``) happens first and yields
422 for a malformed scope; authorization is the second gate.
"""

from __future__ import annotations

from typing import List

from fastapi import HTTPException

__all__ = [
    "HOSTED_STRATEGY_ACCOUNT_SCOPES_ENV",
    "authorize_account_scope",
    "authorized_account_scopes",
    "is_account_authorized",
]

HOSTED_STRATEGY_ACCOUNT_SCOPES_ENV = "HOSTED_STRATEGY_ACCOUNT_SCOPES"


def _split(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def authorized_account_scopes() -> List[str]:
    """The configured allowlist, in a stable order. Empty means default-deny."""
    import os

    return list(dict.fromkeys(_split(os.environ.get(HOSTED_STRATEGY_ACCOUNT_SCOPES_ENV, ""))))


def is_account_authorized(account_scope: str) -> bool:
    candidate = (account_scope or "").strip()
    return bool(candidate) and candidate in authorized_account_scopes()


def authorize_account_scope(account_scope: str) -> str:
    """Return the authorized, normalized account scope, or raise 403.

    Uses the raw trimmed value for comparison (account scopes are exact
    identities). A scope outside the allowlist is refused even when it is
    well-formed, and the refusal does not reveal whether the account exists.
    """
    candidate = (account_scope or "").strip()
    if is_account_authorized(candidate):
        return candidate
    raise HTTPException(
        status_code=403,
        detail=(
            f"account_scope {candidate!r} is not authorized for hosted strategies; "
            "configure HOSTED_STRATEGY_ACCOUNT_SCOPES on the server"
        ),
    )
