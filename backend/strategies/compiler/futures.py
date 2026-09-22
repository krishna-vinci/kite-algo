"""``target_futures``: contract resolution against the pinned generation (D-1).

A futures contract is not an equity with a different label. It has a lot size that
sizes the position, an expiry that gives it a deadline, a tick that constrains
price, and a lifecycle that can end — all of which come from the *pinned*
generation, so the contract a plan named is the contract it meant even after the
underlying rolls.

Three refusals here are not ceremony. A mapping that resolves to an equity is not a
futures contract, whatever the payload calls it, so the instrument type is checked
rather than trusted. A contract with no expiry cannot be rolled, and a position
nobody planned to hold is worse than a refusal. And a lot size of zero cannot size
a position at all — guessing 1 would silently trade the wrong quantity by a factor
of the lot.

The freeze axis is honest about a gap in source: the catalog has **no**
freeze-quantity column, so the limit can only come from the intent's own
declaration. When it is declared, exceeding it refuses; when it is absent, the
resolved leg records ``freeze_source: unavailable`` so the unchecked axis is
visible to whoever reads the plan rather than invisible.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Mapping

from backend.strategies.compiler.base import (
    PinnedCatalogRead,
    ResolvedPlan,
    TargetCompiler,
    ValidationRefusal,
)

#: The catalog instrument type that means "futures contract".
FUTURES_INSTRUMENT_TYPE = "FUT"


def _as_lots(value: Any) -> int:
    """Lots must be a positive whole number: half a contract is not a contract."""
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationRefusal("PAYLOAD_INVALID", {"reason": str(exc)}) from exc
    if numeric != int(numeric):
        raise ValidationRefusal(
            "PAYLOAD_INVALID",
            {"lots": value, "message": "Lots must be a whole number of contracts"},
        )
    lots = int(numeric)
    if lots <= 0:
        raise ValidationRefusal(
            "PAYLOAD_INVALID",
            {"lots": lots, "message": "A futures target requires at least one lot"},
        )
    return lots


def _as_optional_int(value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _expiry_iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value in (None, ""):
        return None
    return str(value)


class FuturesCompiler(TargetCompiler):
    target_kind = "target_futures"

    @staticmethod
    def _roll_binding(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """The roll this plan plays a role in, or ``None``.

        A futures plan that is part of a roll says so explicitly:
        ``{"roll": {"role": "open_new" | "close_old", "roll_id": <optional>}}``.
        The role is what the executor enforces (a close may not go out before the
        roll released it; a replacement fill is what proves the roll), so an
        unknown role is refused rather than ignored.
        """
        from backend.strategies.rolls import ROLL_PLAN_ROLES

        raw = payload.get("roll")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValidationRefusal("PAYLOAD_INVALID", {"reason": "roll must be an object"})
        role = str(raw.get("role") or "").strip().lower()
        if role not in ROLL_PLAN_ROLES:
            raise ValidationRefusal(
                "PAYLOAD_INVALID", {"roll_role": role, "allowed": list(ROLL_PLAN_ROLES)}
            )
        roll_id = raw.get("roll_id")
        if roll_id is not None and not str(roll_id).strip():
            raise ValidationRefusal("PAYLOAD_INVALID", {"roll_id": str(roll_id)})
        return {"roll_id": None if roll_id is None else str(roll_id), "role": role}

    def compile(self, payload: Mapping[str, Any], pinned: PinnedCatalogRead) -> ResolvedPlan:
        required = ("instrument_token", "exchange", "tradingsymbol", "lots")
        missing = [field for field in required if payload.get(field) is None]
        if missing:
            raise ValidationRefusal("PAYLOAD_INVALID", {"missing_fields": sorted(missing)})

        roll_binding = self._roll_binding(payload)

        try:
            broker_token = int(payload["instrument_token"])
        except (TypeError, ValueError) as exc:
            raise ValidationRefusal("PAYLOAD_INVALID", {"reason": str(exc)}) from exc

        exchange = str(payload["exchange"]).upper()
        tradingsymbol = str(payload["tradingsymbol"]).upper()
        product = str(payload.get("product") or "NRML").upper()
        lots = _as_lots(payload.get("lots"))

        mapping = pinned.resolve_token(
            broker_token, exchange=exchange, symbol=tradingsymbol
        )
        if mapping is None:
            raise ValidationRefusal(
                "CONTRACT_UNRESOLVED",
                {
                    "instrument_token": broker_token,
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "catalog_generation": pinned.pin(),
                },
            )

        lifecycle = str(mapping.get("lifecycle_status") or "")
        if lifecycle != "active":
            raise ValidationRefusal(
                "CONTRACT_UNRESOLVED",
                {
                    "tradingsymbol": tradingsymbol,
                    "lifecycle_status": lifecycle,
                    "catalog_generation": pinned.pin(),
                },
            )

        instrument_type = str(mapping.get("instrument_type") or "").upper()
        if instrument_type != FUTURES_INSTRUMENT_TYPE:
            # The payload naming it a future does not make it one.
            raise ValidationRefusal(
                "CONTRACT_UNRESOLVED",
                {
                    "tradingsymbol": tradingsymbol,
                    "instrument_type": instrument_type,
                    "expected_instrument_type": FUTURES_INSTRUMENT_TYPE,
                    "message": "This instrument is not a futures contract",
                },
            )

        expiry = _expiry_iso(mapping.get("expiry"))
        if not expiry:
            # A contract with no expiry cannot be rolled, and an unrollable position
            # is one nobody planned to hold.
            raise ValidationRefusal(
                "EXPIRY_UNAVAILABLE",
                {
                    "tradingsymbol": tradingsymbol,
                    "instrument_id": mapping["instrument_id"],
                    "message": "A futures contract requires an expiry to be rollable",
                },
            )

        lot_size = _as_optional_int(mapping.get("lot_size"))
        if not lot_size or lot_size <= 0:
            raise ValidationRefusal(
                "CONTRACT_UNRESOLVED",
                {
                    "tradingsymbol": tradingsymbol,
                    "lot_size": mapping.get("lot_size"),
                    "message": (
                        "The catalog provides no lot size for this contract, so the "
                        "position cannot be sized in lots"
                    ),
                },
            )

        quantity = lots * lot_size
        freeze_quantity = _as_optional_int(payload.get("freeze_quantity"))
        if freeze_quantity is not None and quantity > freeze_quantity:
            raise ValidationRefusal(
                "FREEZE_LIMIT_EXCEEDED",
                {
                    "tradingsymbol": tradingsymbol,
                    "quantity": quantity,
                    "freeze_quantity": freeze_quantity,
                    "message": (
                        "The order quantity exceeds the declared exchange freeze limit; "
                        "splitting it across orders would size the position differently "
                        "from what the strategy asked for"
                    ),
                },
            )

        side = str(payload.get("side") or "BUY").upper()
        signed_quantity = quantity if side != "SELL" else -quantity
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
            "lots": lots,
            "side": side,
            "roll": roll_binding,
        }
        resolved: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "catalog_generation": pinned.pin(),
            # The roll this plan plays a role in, frozen WITH the plan: the
            # executor enforces the contract from the artifact, never from a
            # caller's narration (``None`` when the plan is not part of a roll).
            "roll": roll_binding,
            "legs": [
                {
                    "instrument_id": mapping["instrument_id"],
                    "exchange": mapping["exchange"],
                    "tradingsymbol": mapping["tradingsymbol"],
                    "broker_exchange": mapping["broker_exchange"],
                    "broker_symbol": mapping["broker_symbol"],
                    "broker_token": mapping["broker_token"],
                    "product": product,
                    "instrument_type": instrument_type,
                    "underlying": mapping.get("underlying") or "",
                    "lots": lots,
                    "lot_size": lot_size,
                    "quantity": quantity,
                    "signed_quantity": signed_quantity,
                    "tick_size": mapping.get("tick_size"),
                    "expiry": expiry,
                    # Recorded rather than assumed: a consumer can see whether the
                    # freeze axis was actually checked.
                    "freeze_quantity": freeze_quantity,
                    "freeze_source": (
                        "intent" if freeze_quantity is not None else "unavailable"
                    ),
                    "reference_price": reference_price,
                }
            ],
        }
        return ResolvedPlan(target_kind=self.target_kind, logical=logical, resolved=resolved)
