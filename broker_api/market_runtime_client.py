import asyncio
import logging
import os
from typing import Dict, Any
from urllib.parse import quote

import httpx


logger = logging.getLogger(__name__)


def market_runtime_enabled() -> bool:
    value = os.getenv("MARKET_RUNTIME_ENABLED", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def market_runtime_http_url() -> str:
    return os.getenv("MARKET_RUNTIME_HTTP_URL", "http://localhost:8780").rstrip("/")


class MarketRuntimeClient:
    def __init__(self, base_url: str, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_seconds),
        )

    async def get_status(self) -> Dict[str, Any]:
        return await self._request_json("GET", "/internal/market-runtime/status")

    async def set_owner_subscriptions(self, owner_id: str, subscriptions: Dict[int, str]) -> Dict[str, Any]:
        payload = {"tokens": {str(int(token)): str(mode) for token, mode in subscriptions.items()}}
        return await self._request_json(
            "PUT",
            f"/internal/market-runtime/subscriptions/{quote(owner_id, safe='')}",
            json=payload,
        )

    async def get_owner_subscriptions(self, owner_id: str) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/internal/market-runtime/subscriptions/{quote(owner_id, safe='')}",
        )

    async def delete_owner(self, owner_id: str) -> Dict[str, Any]:
        return await self._request_json(
            "DELETE",
            f"/internal/market-runtime/subscriptions/{quote(owner_id, safe='')}",
        )

    async def _request_json(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


_client_lock = asyncio.Lock()
_client: MarketRuntimeClient | None = None


async def get_market_runtime_client() -> MarketRuntimeClient:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            _client = MarketRuntimeClient(market_runtime_http_url())
            logger.info("Initialized market runtime client for %s", _client.base_url)
    return _client
