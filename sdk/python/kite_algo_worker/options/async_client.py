"""Async counterpart of the public worker options HTTP namespace."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from .._shared import session_headers
from .models import (
    OptionEntryPreviewRequest,
    OptionExecutionLeg,
    OptionExpirySnapshot,
    OptionRunActionRequest,
    OptionRunCreateRequest,
)


class AsyncOptionWorkerClient:
    """Namespaced async option helpers layered on an async worker client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _options_path(underlying: str, suffix: str) -> str:
        return f"/worker/options/underlyings/{str(underlying).upper()}/{suffix.lstrip('/')}"

    async def ensure_session(self, underlying: str) -> dict[str, Any]:
        return await self._client._request("GET", self._options_path(underlying, "session"))

    async def list_expiries(self, underlying: str) -> dict[str, Any]:
        payload = await self._client._request("GET", self._options_path(underlying, "expiries"))
        return OptionExpirySnapshot.model_validate(payload).model_dump()

    async def get_chain(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return await self._client._request(
            "GET", self._options_path(underlying, "chain"), params={"expiry": expiry}
        )

    async def get_mini_chain(
        self, underlying: str, *, expiry: str | None = None, window: int = 5
    ) -> dict[str, Any]:
        return await self._client._request(
            "GET",
            self._options_path(underlying, "mini-chain"),
            params={"expiry": expiry, "window": window},
        )

    async def get_greeks(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return await self._client._request(
            "GET", self._options_path(underlying, "greeks"), params={"expiry": expiry}
        )

    async def resolve_contracts(self, underlying: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._client._request(
            "POST", self._options_path(underlying, "selection/resolve"), json=dict(payload)
        )

    async def get_pcr(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return await self._client._request(
            "GET", self._options_path(underlying, "analytics/pcr"), params={"expiry": expiry}
        )

    async def get_max_pain(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return await self._client._request(
            "GET", self._options_path(underlying, "analytics/max-pain"), params={"expiry": expiry}
        )

    async def preview_strategy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._client._request(
            "POST", "/worker/options/strategies/preview", json=dict(payload)
        )

    async def preview_entry(
        self,
        strategy_run_id: str,
        orders: Optional[Iterable[Mapping[str, Any]]] = None,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        all_or_none: bool = False,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        if payload is not None:
            request_payload = dict(payload)
            return await self._client._request(
                "POST", f"/worker/options/runs/{strategy_run_id}/preview-entry", json=request_payload
            )
        if orders is None:
            return await self._client._request(
                "POST", f"/worker/options/runs/{strategy_run_id}/preview-entry", json={}
            )

        request = OptionEntryPreviewRequest(
            strategy_run_id=strategy_run_id,
            orders=[dict(order) for order in orders],
            metadata=dict(metadata or {}),
            all_or_none=all_or_none,
        )
        return await self._client.preview_basket(
            request.strategy_run_id,
            request.orders,
            metadata=request.metadata,
            all_or_none=request.all_or_none,
        )

    async def create_run(
        self,
        *,
        strategy_name: str,
        product: str,
        legs: Iterable[Mapping[str, Any]],
        strategy_run_id: str | None = None,
        protection: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        session_nonce: str | None = None,
    ) -> dict[str, Any]:
        request = OptionRunCreateRequest(
            strategy_name=strategy_name,
            product=product,
            strategy_run_id=strategy_run_id,
            legs=[
                leg if isinstance(leg, OptionExecutionLeg) else OptionExecutionLeg.model_validate(dict(leg))
                for leg in legs
            ],
            protection=dict(protection) if protection is not None else None,
            metadata=dict(metadata or {}),
        )
        payload = request.model_dump(exclude_none=True)
        if request.protection is None:
            payload.pop("protection", None)
        return await self._client._request(
            "POST",
            "/worker/options/runs",
            json=payload,
            headers=session_headers(session_nonce),
        )

    async def preview_run_entry(
        self, strategy_run_id: str, payload: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        return await self._client._request(
            "POST", f"/worker/options/runs/{strategy_run_id}/preview-entry", json=dict(payload or {})
        )

    async def enter(
        self,
        strategy_run_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        safety_token: str | None = None,
        session_nonce: str | None = None,
    ) -> dict[str, Any]:
        request = OptionRunActionRequest.model_validate(
            {
                **dict(payload or {}),
                **({"safety_token": safety_token} if safety_token is not None else {}),
            }
        )
        return await self._client._request(
            "POST",
            f"/worker/options/runs/{strategy_run_id}/enter",
            json=request.model_dump(exclude_none=True),
            headers=session_headers(session_nonce),
        )

    async def preview_exit(
        self, strategy_run_id: str, payload: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        return await self._client._request(
            "POST", f"/worker/options/runs/{strategy_run_id}/preview-exit", json=dict(payload or {})
        )

    async def exit(
        self,
        strategy_run_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        safety_token: str | None = None,
        session_nonce: str | None = None,
    ) -> dict[str, Any]:
        request = OptionRunActionRequest.model_validate(
            {
                **dict(payload or {}),
                **({"safety_token": safety_token} if safety_token is not None else {}),
            }
        )
        return await self._client._request(
            "POST",
            f"/worker/options/runs/{strategy_run_id}/exit",
            json=request.model_dump(exclude_none=True),
            headers=session_headers(session_nonce),
        )

    async def get_run_state(self, strategy_run_id: str) -> dict[str, Any]:
        return await self._client._request("GET", f"/worker/options/runs/{strategy_run_id}/state")

    async def update_protection(
        self,
        strategy_run_id: str,
        protection: Mapping[str, Any],
        *,
        session_nonce: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._request(
            "PUT",
            f"/worker/options/runs/{strategy_run_id}/protection",
            json=dict(protection),
            headers=session_headers(session_nonce),
        )

    async def get_protection_state(self, strategy_run_id: str) -> dict[str, Any]:
        return await self._client._request(
            "GET", f"/worker/options/runs/{strategy_run_id}/protection/state"
        )

    async def replay_protection(
        self,
        strategy_run_id: str,
        metric_snapshots: Iterable[Mapping[str, Any]],
        protection: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"metric_snapshots": [dict(item) for item in metric_snapshots]}
        if protection is not None:
            payload["protection"] = dict(protection)
        return await self._client._request(
            "POST", f"/worker/options/runs/{strategy_run_id}/protection/replay", json=payload
        )


__all__ = ["AsyncOptionWorkerClient"]
