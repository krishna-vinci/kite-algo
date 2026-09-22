"""Deployment setting that gates hosted live execution.

``HOSTED_LIVE_ENABLED`` is **off by default**: persisted mode constraints and
API/SDK validation may admit ``live``, but admission, launch and submission also
enforce this deployment setting. Removing a CHECK constraint or widening a
validation vocabulary is never sufficient to execute live.

The reader uses an explicit TRUTHY allowlist. A "not false" test would treat a
typo (``flase``, ``ture``, ``enabled?``) as ENABLED, which is the one mistake a
safety switch must never make.

Existing live operations outside hosted strategies are *not* affected: the
setting only gates the hosted live execution path.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

HOSTED_LIVE_ENABLED_ENV = "HOSTED_LIVE_ENABLED"

#: The ONLY values that turn hosted live execution on.
_TRUTHY = ("1", "true", "yes", "on")


def hosted_live_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True only when ``HOSTED_LIVE_ENABLED`` is one of the truthy spellings.

    Everything else -- unset, empty, ``0``/``false``/``no``/``off``/``disabled``,
    or an unrecognised string such as ``flase`` -- is **False**. A deployment
    that mistypes the flag stays safe rather than accidentally arming live
    trading.
    """
    source = os.environ if environ is None else environ
    raw = str(source.get(HOSTED_LIVE_ENABLED_ENV, "") or "").strip().lower()
    return raw in _TRUTHY


def hosted_live_disabled_detail(*, plan_id: str = "", surface: str = "") -> dict:
    return {
        "plan_id": str(plan_id or ""),
        "surface": str(surface or ""),
        "setting": HOSTED_LIVE_ENABLED_ENV,
        "message": (
            "hosted live execution is disabled in this deployment; "
            f"set {HOSTED_LIVE_ENABLED_ENV}=true after acceptance to enable it"
        ),
    }
