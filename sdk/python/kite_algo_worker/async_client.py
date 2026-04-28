from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Dict, Mapping, Optional

from .client import AlgoWorkerConfig, JsonDict
from .exceptions import error_for_status


@dataclass(frozen=True)
class AsyncKiteAlgoWorkerClient:
    config: AlgoWorkerConfig
    client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.config.base_url:
            raise ValueError("base_url is required")
        if not self.config.token:
            raise ValueError("token is required")
        httpx = import_module("httpx")
        object.__setattr__(self, "client", httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.config.timeout,
        ))

    async def __aenter__(self) -> "AsyncKiteAlgoWorkerClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def get_run(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}")

    async def preview_order(self, strategy_run_id: str, order: Mapping[str, Any], *, metadata: Optional[Mapping[str, Any]] = None) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/preview/order",
            json={"order": dict(order), "metadata": dict(metadata or {})},
        )

    def _url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        prefix = "/" + self.config.api_prefix.strip("/")
        suffix = "/" + path.strip("/")
        return f"{base}{prefix}{suffix}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        response = await self.client.request(method, self._url(path), **kwargs)
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        try:
            body: Any = response.json()
        except ValueError:
            body = {"raw": response.text}
        raise error_for_status(response.status_code, body, fallback=f"Worker API returned {response.status_code} for {method} {path}")


__all__ = ["AsyncKiteAlgoWorkerClient"]
