"""``single_instrument``: one canonical instrument, one signed target quantity.

The simplest compiler, and the one that pins the shared contract: the payload
names a broker coordinate, resolution maps it to a canonical ``instrument_id``
against the pinned generation, and the resolved leg carries the signed target
quantity. The sign *is* the direction — a negative target is a short, and zero is
a real instruction (flatten) rather than an absence.

Only refusals are raised here; nothing is placed, sized in capital, or admitted.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from backend.strategies.compiler.base import (
    PinnedCatalogRead,
    ResolvedPlan,
    TargetCompiler,
    ValidationRefusal,
)


class SingleInstrumentCompiler(TargetCompiler):
    target_kind = "single_instrument"

    def compile(self, payload: Mapping[str, Any], pinned: PinnedCatalogRead) -> ResolvedPlan:
        required = ("instrument_token", "exchange", "tradingsymbol", "product", "target_quantity")
        missing = [field for field in required if payload.get(field) is None]
        if missing:
            raise ValidationRefusal("PAYLOAD_INVALID", {"missing_fields": sorted(missing)})

        try:
            target_quantity = int(payload["target_quantity"])
            broker_token = int(payload["instrument_token"])
        except (TypeError, ValueError) as exc:
            raise ValidationRefusal("PAYLOAD_INVALID", {"reason": str(exc)}) from exc

        exchange = str(payload["exchange"]).upper()
        tradingsymbol = str(payload["tradingsymbol"]).upper()
        product = str(payload["product"]).upper()

        mapping = pinned.resolve_token(broker_token, exchange=exchange, symbol=tradingsymbol)
        if mapping is None:
            raise ValidationRefusal(
                "INSTRUMENT_UNRESOLVED",
                {
                    "instrument_token": broker_token,
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "catalog_generation": pinned.pin(),
                },
            )
        lifecycle = str(mapping.get("lifecycle_status") or "")
        if lifecycle != "active":
            # A retired or expired listing is not something to resolve a target
            # against: the coordinate no longer means what the payload assumed.
            raise ValidationRefusal(
                "INSTRUMENT_UNRESOLVED",
                {
                    "instrument_token": broker_token,
                    "tradingsymbol": tradingsymbol,
                    "lifecycle_status": lifecycle,
                    "catalog_generation": pinned.pin(),
                },
            )

        reference_price = payload.get("reference_price")
        try:
            reference_price = None if reference_price is None else float(reference_price)
        except (TypeError, ValueError) as exc:
            raise ValidationRefusal("PAYLOAD_INVALID", {"reason": str(exc)}) from exc

        logical: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "instrument_token": broker_token,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "product": product,
            "target_quantity": target_quantity,
        }
        resolved: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "catalog_generation": pinned.pin(),
            "legs": [
                {
                    "instrument_id": mapping["instrument_id"],
                    "exchange": mapping["exchange"],
                    "tradingsymbol": mapping["tradingsymbol"],
                    "broker_exchange": mapping["broker_exchange"],
                    "broker_symbol": mapping["broker_symbol"],
                    "broker_token": mapping["broker_token"],
                    "product": product,
                    "signed_quantity": target_quantity,
                    # Carried so admission's notional arithmetic has a price from
                    # the plan itself, with no new market-data dependency (D-2).
                    "reference_price": reference_price,
                }
            ],
        }
        return ResolvedPlan(
            target_kind=self.target_kind, logical=logical, resolved=resolved
        )
