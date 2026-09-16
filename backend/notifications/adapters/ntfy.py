"""ntfy adapter.

Destination contract: ``{"url_env": "NTFY_PRIMARY_URL"}`` — the full topic URL
lives in the environment (never in stored channel rows). The env var is
resolved as destination override -> provider default: ``destination["url_env"]``
when present, else ``DEFAULT_URL_ENV`` (``NTFY_PRIMARY_URL``). The delivery
worker merges the channel's ``secret_env`` into the destination before send,
so a channel's secret pointer always governs the real send. Sends POST
``<url>`` with header ``Title: <subject>`` and the message body as the raw
request body (``text/plain``).

Limits (spec E-14): title <= 512 bytes, body <= 4096 chars — both truncated
with the ``…[truncated]`` marker.

Classification: 2xx accepted (``provider_id`` from the ``X-Ntfy-Id`` response
header when present); 429 retryable via ``Retry-After`` header (default 5);
5xx retryable; other 4xx permanent; timeout / connection error unknown;
missing URL env var permanent (E-24). Never raises.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

from . import DEFAULT_URL_ENV, DeliveryOutcome, truncate_text

MAX_BODY_LEN = 4096
MAX_TITLE_BYTES = 512
DEFAULT_RETRY_AFTER_S = 5


class NtfyAdapter:
    provider = "ntfy"

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def send(self, destination: dict, subject: str, body: str) -> DeliveryOutcome:
        # resolution order: destination override -> provider default env name
        url_env = str(destination.get("url_env") or DEFAULT_URL_ENV)
        url = os.environ.get(url_env)
        if not url:
            return DeliveryOutcome(
                status="permanent",
                detail=f"missing env var {url_env}",
            )

        title = truncate_text(subject, MAX_TITLE_BYTES)
        text = truncate_text(body, MAX_BODY_LEN)

        try:
            response = await self._get_client().post(
                url,
                headers={"Title": title, "Content-Type": "text/plain; charset=utf-8"},
                content=text.encode("utf-8"),
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return DeliveryOutcome(
                status="unknown",
                detail=f"network error: {type(exc).__name__}",
            )
        except Exception as exc:  # never raise out of an adapter
            return DeliveryOutcome(
                status="unknown",
                detail=f"unexpected error: {type(exc).__name__}",
            )
        finally:
            if self._owns_client and self._client is not None:
                await self._client.aclose()
                self._client = None

        return self._classify(response)

    def _classify(self, response: httpx.Response) -> DeliveryOutcome:
        code = response.status_code

        if 200 <= code < 300:
            provider_id = response.headers.get("X-Ntfy-Id")
            return DeliveryOutcome(
                status="accepted",
                provider_id=provider_id or None,
                detail="accepted",
            )
        if code == 429:
            return DeliveryOutcome(
                status="retryable",
                retry_after_s=self._retry_after(response),
                detail="rate limited (HTTP 429)",
            )
        if 500 <= code < 600:
            return DeliveryOutcome(
                status="retryable",
                detail=f"server error (HTTP {code})",
            )
        return DeliveryOutcome(
            status="permanent",
            detail=f"rejected (HTTP {code})",
        )

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(1, int(header))
            except (TypeError, ValueError):
                pass
        return DEFAULT_RETRY_AFTER_S
