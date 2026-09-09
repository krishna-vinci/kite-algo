"""Notification adapter registry and shared delivery contract.

`DeliveryOutcome` is the single result type every adapter returns — adapters
never raise for provider/transport problems; they classify instead:

- ``accepted``   provider took the message (2xx)
- ``retryable``  transient failure (429 with retry hint, 5xx)
- ``permanent``  will never succeed (bad destination, missing env/secret, 4xx)
- ``unknown``    outcome not observable (timeout / connection error) — the
                 provider may or may not have delivered (spec E-21/E-22)

Secrets (tokens, URLs containing credentials) must never appear in
`DeliveryOutcome.detail` or `provider_id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional, Protocol

TRUNCATION_MARKER = "…[truncated]"


@dataclass(frozen=True)
class DeliveryOutcome:
    status: Literal["accepted", "retryable", "permanent", "unknown"]
    provider_id: Optional[str] = None
    retry_after_s: Optional[int] = None
    detail: str = ""


class NotificationAdapter(Protocol):
    provider: str

    async def send(self, destination: dict, subject: str, body: str) -> DeliveryOutcome: ...


def truncate_text(text: str, limit: int, marker: str = TRUNCATION_MARKER) -> str:
    """Return `text` clipped so that len(result) <= limit.

    When clipping is needed, `marker` is appended and the visible prefix is
    shortened so the total stays within `limit`. No-op when text already fits.
    """
    if len(text) <= limit:
        return text
    keep = limit - len(marker)
    if keep <= 0:
        return text[:limit]
    return text[:keep] + marker


# Factory registry. Factories take no arguments and build an adapter that owns
# its own httpx.AsyncClient. Delivery workers may replace or extend entries via
# `register_adapter` (dependency injection point).
_FACTORIES: dict[str, Callable[[], NotificationAdapter]] = {}


def register_adapter(
    provider: str, factory: Optional[Callable[[], NotificationAdapter]]
) -> Optional[Callable[[], NotificationAdapter]]:
    """Register (or replace) the factory for `provider`.

    Returns the previously registered factory so callers can restore it;
    passing ``None`` as the factory removes the entry.
    """
    previous = _FACTORIES.get(provider)
    if factory is None:
        _FACTORIES.pop(provider, None)
    else:
        _FACTORIES[provider] = factory
    return previous


def get_adapter(provider: str) -> NotificationAdapter:
    factory = _FACTORIES.get(provider)
    if factory is None:
        raise ValueError(f"unknown notification provider: {provider!r}")
    return factory()


# Built-in providers. Imported last so the shared names above are already
# bound when these modules import them from this package.
from .ntfy import NtfyAdapter  # noqa: E402
from .telegram import TelegramAdapter  # noqa: E402

register_adapter("telegram", TelegramAdapter)
register_adapter("ntfy", NtfyAdapter)
