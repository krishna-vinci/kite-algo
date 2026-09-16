"""Redact known credentials from child logs before persistence or presentation.

The supervisor ships bounded log chunks to the API; this is the single place
that masks secrets, so nothing secret is stored in ``strategy_job_logs`` or
returned to a browser. It masks:

- worker child tokens (``kwa_…``), session nonces (``wsn_…``), producer secrets;
- authorization headers (``Bearer …`` / ``...-Credential: …``);
- the configured supervisor credential value, if present in the environment.

Patterns are intentionally broad (mask the whole token-shaped run) so a partial
secret cannot survive; over-redaction is acceptable, under-redaction is not.
"""

from __future__ import annotations

import os
import re

__all__ = ["redact_text"]

_REDACTED = "[redacted]"

_PATTERNS = (
    re.compile(r"\bkwa_[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bwsn_[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(X-Hosted-Supervisor-Credential\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(KITE_ALGO_WORKER_TOKEN\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
)


def _configured_secrets() -> list:
    values = []
    for name in (
        "HOSTED_SUPERVISOR_CREDENTIAL",
        "HOSTED_SUPERVISOR_CREDENTIALS",
        "APP_JWT_SECRET",
        "JWT_SECRET",
        "WORKER_SAFETY_TOKEN_SECRET",
    ):
        raw = os.environ.get(name)
        if not raw:
            continue
        for part in str(raw).split(","):
            cleaned = part.strip()
            if len(cleaned) >= 6:
                values.append(cleaned)
    return values


def redact_text(text: str) -> str:
    """Return ``text`` with known credentials masked. Never raises."""
    if text is None:
        return ""
    redacted = str(text)
    for pattern in _PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
        else:
            redacted = pattern.sub(_REDACTED, redacted)
    for secret in _configured_secrets():
        redacted = redacted.replace(secret, _REDACTED)
    return redacted
