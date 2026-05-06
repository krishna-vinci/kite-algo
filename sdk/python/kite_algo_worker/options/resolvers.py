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
    return [dict(item) for item in list(response.get("contracts") or [])]


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


__all__ = ["SpreadSpec", "resolve_option_contracts", "resolve_spread"]
