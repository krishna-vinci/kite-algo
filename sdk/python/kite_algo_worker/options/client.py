from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from .models import (
    OptionEntryPreviewRequest,
    OptionExecutionLeg,
    OptionExpirySnapshot,
    OptionRunActionRequest,
    OptionRunCreateRequest,
    SpreadSpec,
)
from .resolvers import resolve_option_contracts as _resolve_option_contracts
from .resolvers import resolve_option_leg as _resolve_option_leg
from .resolvers import resolve_offset_leg as _resolve_offset_leg
from .resolvers import resolve_delta_leg as _resolve_delta_leg
from .resolvers import resolve_spread as _resolve_spread


class OptionWorkerClient:
    """Namespaced option helpers layered on top of generic worker primitives."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _options_path(underlying: str, suffix: str) -> str:
        return f"/worker/options/underlyings/{str(underlying).upper()}/{suffix.lstrip('/')}"

    def ensure_session(self, underlying: str) -> dict[str, Any]:
        return self._client._request("GET", self._options_path(underlying, "session"))

    def list_expiries(self, underlying: str) -> dict[str, Any]:
        payload = self._client._request("GET", self._options_path(underlying, "expiries"))
        return OptionExpirySnapshot.model_validate(payload).model_dump()

    def get_chain(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return self._client._request(
            "GET",
            self._options_path(underlying, "chain"),
            params={"expiry": expiry},
        )

    def get_mini_chain(self, underlying: str, *, expiry: str | None = None, window: int = 5) -> dict[str, Any]:
        return self._client._request(
            "GET",
            self._options_path(underlying, "mini-chain"),
            params={"expiry": expiry, "window": window},
        )

    def get_greeks(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return self._client._request(
            "GET",
            self._options_path(underlying, "greeks"),
            params={"expiry": expiry},
        )

    def resolve_contracts(self, underlying: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._client._request(
            "POST",
            self._options_path(underlying, "selection/resolve"),
            json=payload,
        )

    def resolve_option_contracts(self, *, underlying: str, selection_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _resolve_option_contracts(self, underlying=underlying, selection_payload=selection_payload)

    def resolve_option_leg(
        self,
        *,
        underlying: str,
        product: str,
        expiry: str,
        selection: Mapping[str, Any],
        transaction_type: str,
        lots: int = 1,
        order_type: str = "MARKET",
        price: float | None = None,
        trigger_price: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> OptionExecutionLeg:
        return _resolve_option_leg(
            self,
            underlying=underlying,
            product=product,
            expiry=expiry,
            selection=selection,
            transaction_type=transaction_type,
            lots=lots,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            metadata=metadata,
        )

    def resolve_offset_leg(
        self,
        *,
        underlying: str,
        product: str,
        expiry: str,
        option_type: str,
        offset: str,
        transaction_type: str,
        lots: int = 1,
        order_type: str = "MARKET",
        price: float | None = None,
        trigger_price: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> OptionExecutionLeg:
        return _resolve_offset_leg(
            self,
            underlying=underlying,
            product=product,
            expiry=expiry,
            option_type=option_type,
            offset=offset,
            transaction_type=transaction_type,
            lots=lots,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            metadata=metadata,
        )

    def resolve_delta_leg(
        self,
        *,
        underlying: str,
        product: str,
        expiry: str,
        option_type: str,
        delta_target: float,
        transaction_type: str,
        lots: int = 1,
        order_type: str = "MARKET",
        price: float | None = None,
        trigger_price: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> OptionExecutionLeg:
        return _resolve_delta_leg(
            self,
            underlying=underlying,
            product=product,
            expiry=expiry,
            option_type=option_type,
            delta_target=delta_target,
            transaction_type=transaction_type,
            lots=lots,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            metadata=metadata,
        )

    def resolve_spread(self, *, underlying: str, product: str, spec: SpreadSpec) -> list[OptionExecutionLeg]:
        return _resolve_spread(self, underlying=underlying, product=product, spec=spec)

    def get_pcr(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return self._client._request(
            "GET",
            self._options_path(underlying, "analytics/pcr"),
            params={"expiry": expiry},
        )

    def get_max_pain(self, underlying: str, *, expiry: str | None = None) -> dict[str, Any]:
        return self._client._request(
            "GET",
            self._options_path(underlying, "analytics/max-pain"),
            params={"expiry": expiry},
        )

    def preview_entry(
        self,
        strategy_run_id: str,
        orders: Optional[Iterable[Mapping[str, Any]]] = None,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        all_or_none: bool = False,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        if orders is None and payload is None:
            return self._client._request("POST", f"/worker/options/runs/{strategy_run_id}/preview-entry", json={})
        if payload is not None:
            return self._client._request("POST", f"/worker/options/runs/{strategy_run_id}/preview-entry", json=dict(payload))

        request_payload = OptionEntryPreviewRequest(
            strategy_run_id=strategy_run_id,
            orders=[dict(order) for order in (orders or [])],
            metadata=dict(metadata or {}),
            all_or_none=all_or_none,
        )
        return self._client.preview_basket(
            request_payload.strategy_run_id,
            request_payload.orders,
            metadata=request_payload.metadata,
            all_or_none=request_payload.all_or_none,
        )

    def preview_strategy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._client._request("POST", "/worker/options/strategies/preview", json=dict(payload))

    def create_run(
        self,
        *,
        strategy_name: str,
        product: str,
        legs: Iterable[Mapping[str, Any]],
        protection: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        request_payload = OptionRunCreateRequest(
            strategy_name=strategy_name,
            product=product,
            legs=[
                leg if isinstance(leg, OptionExecutionLeg) else OptionExecutionLeg.model_validate(dict(leg))
                for leg in legs
            ],
            protection=dict(protection) if protection is not None else None,
            metadata=dict(metadata or {}),
        )
        payload = request_payload.model_dump(exclude_none=True)
        if request_payload.protection is None:
            payload.pop("protection", None)
        return self._client._request("POST", "/worker/options/runs", json=payload)

    def preview_run_entry(self, strategy_run_id: str, payload: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self._client._request("POST", f"/worker/options/runs/{strategy_run_id}/preview-entry", json=dict(payload or {}))

    def enter(
        self,
        strategy_run_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        safety_token: str | None = None,
        session_nonce: str | None = None,
    ) -> dict[str, Any]:
        request_payload = OptionRunActionRequest.model_validate(
            {
                **dict(payload or {}),
                **({"safety_token": safety_token} if safety_token is not None else {}),
            }
        )
        return self._client._request(
            "POST",
            f"/worker/options/runs/{strategy_run_id}/enter",
            json=request_payload.model_dump(exclude_none=True),
            headers={"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None,
        )

    def preview_exit(self, strategy_run_id: str, payload: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self._client._request("POST", f"/worker/options/runs/{strategy_run_id}/preview-exit", json=dict(payload or {}))

    def exit(
        self,
        strategy_run_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        safety_token: str | None = None,
        session_nonce: str | None = None,
    ) -> dict[str, Any]:
        request_payload = OptionRunActionRequest.model_validate(
            {
                **dict(payload or {}),
                **({"safety_token": safety_token} if safety_token is not None else {}),
            }
        )
        return self._client._request(
            "POST",
            f"/worker/options/runs/{strategy_run_id}/exit",
            json=request_payload.model_dump(exclude_none=True),
            headers={"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None,
        )

    def get_run_state(self, strategy_run_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"/worker/options/runs/{strategy_run_id}/state")

    def update_protection(
        self,
        strategy_run_id: str,
        protection: Mapping[str, Any],
        *,
        session_nonce: str | None = None,
    ) -> dict[str, Any]:
        return self._client._request(
            "PUT",
            f"/worker/options/runs/{strategy_run_id}/protection",
            json=dict(protection),
            headers={"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None,
        )

    def get_protection_state(self, strategy_run_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"/worker/options/runs/{strategy_run_id}/protection/state")

    def replay_protection(
        self,
        strategy_run_id: str,
        metric_snapshots: Iterable[Mapping[str, Any]],
        protection: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"metric_snapshots": [dict(item) for item in metric_snapshots]}
        if protection is not None:
            payload["protection"] = dict(protection)
        return self._client._request("POST", f"/worker/options/runs/{strategy_run_id}/protection/replay", json=payload)


__all__ = ["OptionWorkerClient"]
