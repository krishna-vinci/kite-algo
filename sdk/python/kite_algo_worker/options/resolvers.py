from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

from ..exceptions import KiteAlgoWorkerError
from .models import OptionExecutionLeg, SpreadSpec

if TYPE_CHECKING:
    from .client import OptionWorkerClient


def resolve_option_contracts(
    options_client: "OptionWorkerClient",
    *,
    underlying: str,
    selection_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    payload = dict(selection_payload)
    response = options_client.resolve_contracts(underlying, payload)
    resolved = response.get("resolved") if isinstance(response, Mapping) else None
    if resolved is None and isinstance(response, Mapping):
        resolved = response.get("contracts")
    return [dict(item) for item in list(resolved or [])]


def _build_option_execution_leg(
    contract: Mapping[str, Any],
    *,
    product: str,
    transaction_type: str,
    lots: int,
    order_type: str = "MARKET",
    price: float | None = None,
    trigger_price: float | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> OptionExecutionLeg:
    lot_size = int(contract.get("lot_size") or 0)
    if lot_size <= 0:
        raise KiteAlgoWorkerError("Resolved contract missing valid lot_size", status_code=422)
    merged_metadata = dict(metadata or {})
    if contract.get("resolver") is not None:
        merged_metadata.setdefault("resolver", contract.get("resolver"))
    if contract.get("resolution_meta") is not None:
        merged_metadata.setdefault("resolution_meta", dict(contract.get("resolution_meta") or {}))
    return OptionExecutionLeg(
        tradingsymbol=str(contract.get("tradingsymbol") or ""),
        instrument_token=contract.get("instrument_token"),
        strike=contract.get("strike"),
        option_type=contract.get("option_type"),
        expiry_key=contract.get("expiry_key"),
        lot_size=lot_size,
        lots=lots,
        ltp=contract.get("ltp"),
        transaction_type=transaction_type,
        quantity=lot_size * int(lots),
        product=product,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        metadata=merged_metadata,
    )


def resolve_option_leg(
    options_client: "OptionWorkerClient",
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
    contracts = resolve_option_contracts(
        options_client,
        underlying=underlying,
        selection_payload={"expiry": expiry, "legs": [dict(selection)]},
    )
    if not contracts:
        raise KiteAlgoWorkerError("Expected one resolved contract but received none", status_code=422)
    return _build_option_execution_leg(
        contracts[0],
        product=product,
        transaction_type=transaction_type,
        lots=max(1, int(lots)),
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        metadata=metadata,
    )


def resolve_offset_leg(
    options_client: "OptionWorkerClient",
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
    return resolve_option_leg(
        options_client,
        underlying=underlying,
        product=product,
        expiry=expiry,
        selection={"option_type": option_type, "offset": offset},
        transaction_type=transaction_type,
        lots=lots,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        metadata=metadata,
    )


def resolve_delta_leg(
    options_client: "OptionWorkerClient",
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
    return resolve_option_leg(
        options_client,
        underlying=underlying,
        product=product,
        expiry=expiry,
        selection={"option_type": option_type, "delta_target": delta_target},
        transaction_type=transaction_type,
        lots=lots,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        metadata=metadata,
    )


def resolve_spread(
    options_client: "OptionWorkerClient",
    *,
    underlying: str,
    product: str,
    spec: SpreadSpec,
) -> list[OptionExecutionLeg]:
    contracts = resolve_option_contracts(
        options_client,
        underlying=underlying,
        selection_payload={"expiry": spec.expiry, "legs": [dict(leg.selection) for leg in spec.legs]},
    )
    if len(contracts) < len(spec.legs):
        raise KiteAlgoWorkerError(
            f"Expected {len(spec.legs)} resolved contracts but received {len(contracts)}",
            status_code=422,
        )

    resolved_legs: list[OptionExecutionLeg] = []
    for index, (leg, contract) in enumerate(zip(spec.legs, contracts), start=1):
        lot_size = int(contract.get("lot_size") or 0)
        if lot_size <= 0:
            raise KiteAlgoWorkerError(
                f"Resolved contract missing valid lot_size for spread leg {index}",
                status_code=422,
            )

        resolved_legs.append(
            OptionExecutionLeg(
                tradingsymbol=str(contract.get("tradingsymbol") or ""),
                instrument_token=contract.get("instrument_token"),
                strike=contract.get("strike"),
                option_type=contract.get("option_type"),
                expiry_key=contract.get("expiry_key"),
                lot_size=lot_size,
                lots=leg.lots,
                ltp=contract.get("ltp"),
                transaction_type=leg.transaction_type,
                quantity=lot_size * int(leg.lots),
                product=product,
                order_type=leg.order_type,
                price=leg.price,
                trigger_price=leg.trigger_price,
                metadata=dict(leg.metadata),
            )
        )
    return resolved_legs


__all__ = [
    "SpreadSpec",
    "resolve_delta_leg",
    "resolve_offset_leg",
    "resolve_option_contracts",
    "resolve_option_leg",
    "resolve_spread",
]
