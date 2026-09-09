"""Telegram Bot API adapter.

Destination contract: ``{"chat_id": "<chat id>", "token_env": "TELEGRAM_BOT_TOKEN"}``.
The bot token is read from the environment at call time (never stored, never
logged). The env var is resolved as destination override -> provider default:
``destination["token_env"]`` when present, else ``DEFAULT_TOKEN_ENV``
(``TELEGRAM_BOT_TOKEN``). The delivery worker merges the channel's
``secret_env`` into the destination before send, so a channel's secret
pointer always governs the real send. Sends POST
``https://api.telegram.org/bot<token>/sendMessage`` with JSON
``{"chat_id": ..., "text": ...}``.

Classification (spec F4 / E-21):
- 2xx -> accepted (provider_id from ``result.message_id`` when present)
- 429 -> retryable, retry_after_s from body ``parameters.retry_after`` or the
  ``Retry-After`` header (default 5)
- other 4xx -> permanent
- 5xx -> retryable
- timeout / connection error -> unknown
- missing token env var -> permanent (configuration error, E-24); never raises
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

from . import DEFAULT_TOKEN_ENV, DeliveryOutcome, truncate_text

MAX_TEXT_LEN = 4096
DEFAULT_RETRY_AFTER_S = 5
_API_BASE = "https://api.telegram.org"


class TelegramAdapter:
    provider = "telegram"

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        # Tests inject httpx.AsyncClient(transport=httpx.MockTransport(handler)).
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def send(self, destination: dict, subject: str, body: str) -> DeliveryOutcome:
        # resolution order: destination override -> provider default env name
        token_env = str(destination.get("token_env") or DEFAULT_TOKEN_ENV)
        chat_id = destination.get("chat_id")
        if not chat_id:
            return DeliveryOutcome(
                status="permanent",
                detail="missing chat_id in destination",
            )
        token = os.environ.get(token_env)
        if not token:
            return DeliveryOutcome(
                status="permanent",
                detail=f"missing env var {token_env}",
            )

        text = truncate_text(f"{subject}\n{body}" if subject else body, MAX_TEXT_LEN)
        url = f"{_API_BASE}/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}

        try:
            response = await self._get_client().post(url, json=payload)
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

        return self._classify(response, token)

    def _classify(self, response: httpx.Response, token: str) -> DeliveryOutcome:
        code = response.status_code
        description = self._error_description(response)

        if 200 <= code < 300:
            return DeliveryOutcome(
                status="accepted",
                provider_id=self._message_id(response),
                detail="accepted",
            )
        if code == 429:
            return DeliveryOutcome(
                status="retryable",
                retry_after_s=self._retry_after(response),
                detail=f"rate limited (HTTP 429){description}",
            )
        if 500 <= code < 600:
            return DeliveryOutcome(
                status="retryable",
                detail=f"server error (HTTP {code}){description}",
            )
        # 4xx (including 400/401/403/404) and anything else: will not succeed on retry.
        return DeliveryOutcome(
            status="permanent",
            detail=f"rejected (HTTP {code}){description}",
        )

    def _error_description(self, response: httpx.Response) -> str:
        """Short provider description for operator-facing detail (token-redacted)."""
        try:
            data = response.json()
        except Exception:
            return ""
        description = data.get("description") if isinstance(data, dict) else None
        if not description:
            return ""
        snippet = truncate_text(str(description), 200, "…")
        return f": {snippet}"

    @staticmethod
    def _message_id(response: httpx.Response) -> Optional[str]:
        try:
            result = response.json().get("result")
        except Exception:
            return None
        if isinstance(result, dict) and result.get("message_id") is not None:
            return str(result["message_id"])
        return None

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        try:
            params = response.json().get("parameters")
        except Exception:
            params = None
        if isinstance(params, dict):
            raw = params.get("retry_after")
            if raw is not None:
                try:
                    return max(1, int(raw))
                except (TypeError, ValueError):
                    pass
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(1, int(header))
            except (TypeError, ValueError):
                pass
        return DEFAULT_RETRY_AFTER_S
