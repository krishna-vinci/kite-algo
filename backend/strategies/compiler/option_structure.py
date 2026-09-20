"""``option_structure``: pinned leg resolution and a frozen expiry policy (D-1, D-7).

An option structure only exists as a *set*. The legs are chosen together, and their
combined identity is what makes an iron condor an iron condor rather than four
unrelated positions — so every leg resolves against the pinned catalog generation
and the resolved legs freeze into the immutable plan. Chain data at runtime feeds
evaluation metrics; it never re-resolves identity. A structure that could be
redefined by a moving chain is not frozen at all.

The expiry policy is frozen for the same reason, and it is where the money is. A
short leg cannot be discovered at 15:20 on expiry day to have been physical all
along: by then the platform either has the capability to take delivery or it does
not. So the policy is chosen at plan time, shorts default to exiting before the
cutoff, and ``allow_physical_settlement`` without capability evidence is refused
**then** — which is the only moment at which refusing is cheap.

Selection is delegated, not duplicated: a leg that names a moneyness and offset is
resolved through an injected chain resolver (the existing option-chain surfaces),
and a resolver that cannot match refuses rather than guessing a strike. Guessing
would silently build a different structure from the one the proposal described.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from backend.strategies.compiler.base import (
    PinnedCatalogRead,
    ResolvedPlan,
    TargetCompiler,
    ValidationRefusal,
    canonical_json,
    sha256_text,
)

#: Catalog instrument types that make a leg an option.
OPTION_INSTRUMENT_TYPES = ("CE", "PE")

#: The frozen expiry policies (mirrors ``ck_option_run_states_expiry_policy``).
EXPIRY_POLICIES = (
    "exit_before_cutoff",
    "allow_cash_settlement",
    "allow_physical_settlement",
)

#: Policies that settle physically, and therefore need capability evidence.
PHYSICAL_POLICIES = ("allow_physical_settlement",)

#: The chain resolver shape: keyword selection in, one contract or ``None`` out.
ChainResolver = Callable[..., Optional[Mapping[str, Any]]]


def _as_ratio(value: Any) -> int:
    try:
        numeric = float(value if value is not None else 1)
    except (TypeError, ValueError) as exc:
        raise ValidationRefusal("PAYLOAD_INVALID", {"reason": str(exc)}) from exc
    if numeric != int(numeric) or int(numeric) <= 0:
        raise ValidationRefusal(
            "PAYLOAD_INVALID",
            {"ratio": value, "message": "A leg ratio must be a positive whole number"},
        )
    return int(numeric)


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_lot_size(value: Any) -> Optional[int]:
    try:
        lot = int(value)
    except (TypeError, ValueError):
        return None
    return lot if lot > 0 else None


def _expiry_iso(value: Any) -> Optional[str]:
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value in (None, ""):
        return None
    return str(value)


class OptionStructureCompiler(TargetCompiler):
    target_kind = "option_structure"

    def compile(
        self,
        payload: Mapping[str, Any],
        pinned: PinnedCatalogRead,
        *,
        chain_resolver: Optional[ChainResolver] = None,
    ) -> ResolvedPlan:
        legs_payload = payload.get("legs")
        if not isinstance(legs_payload, list) or not legs_payload:
            raise ValidationRefusal(
                "PAYLOAD_INVALID",
                {"missing_fields": ["legs"], "message": "A structure requires at least one leg"},
            )

        underlying = str(payload.get("underlying") or "").upper()
        structure_expiry = _expiry_iso(payload.get("expiry"))
        product = str(payload.get("product") or "NRML").upper()

        resolved_legs: List[Dict[str, Any]] = []
        for index, leg_payload in enumerate(legs_payload):
            if not isinstance(leg_payload, Mapping):
                raise ValidationRefusal(
                    "PAYLOAD_INVALID", {"reason": f"legs[{index}] is not an object"}
                )
            resolved_legs.append(
                self._resolve_leg(
                    index=index,
                    leg=leg_payload,
                    pinned=pinned,
                    product=product,
                    underlying=underlying,
                    structure_expiry=structure_expiry,
                    chain_resolver=chain_resolver,
                )
            )

        expiry_policy = self._expiry_policy(payload, legs=resolved_legs)
        digest = self._structure_digest(
            underlying=underlying, expiry=structure_expiry, legs=resolved_legs
        )

        logical: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "underlying": underlying,
            "expiry": structure_expiry,
            "product": product,
            "structure_id": str(payload.get("structure_id") or ""),
            "expiry_policy": expiry_policy,
            "legs": [
                {
                    "option_type": leg["option_type"],
                    "strike": leg["strike"],
                    "side": leg["side"],
                    "ratio": leg["ratio"],
                }
                for leg in resolved_legs
            ],
        }
        resolved: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "catalog_generation": pinned.pin(),
            "underlying": underlying,
            "expiry": structure_expiry,
            "structure_id": str(payload.get("structure_id") or ""),
            "structure_digest": digest,
            "expiry_policy": expiry_policy,
            "legs": resolved_legs,
        }
        return ResolvedPlan(target_kind=self.target_kind, logical=logical, resolved=resolved)

    # -- legs ---------------------------------------------------------------

    def _resolve_leg(
        self,
        *,
        index: int,
        leg: Mapping[str, Any],
        pinned: PinnedCatalogRead,
        product: str,
        underlying: str,
        structure_expiry: Optional[str],
        chain_resolver: Optional[ChainResolver],
    ) -> Dict[str, Any]:
        selection = leg.get("selection")
        if selection is not None:
            mapping = self._resolve_selection(
                index=index, selection=selection, pinned=pinned,
                underlying=underlying, structure_expiry=structure_expiry,
                chain_resolver=chain_resolver,
            )
        else:
            mapping = self._resolve_direct(index=index, leg=leg, pinned=pinned)

        option_type = str(mapping.get("option_type") or "").upper()
        instrument_type = str(mapping.get("instrument_type") or option_type).upper()
        if instrument_type not in OPTION_INSTRUMENT_TYPES:
            raise ValidationRefusal(
                "OPTION_LEG_UNRESOLVED",
                {
                    "leg_index": index,
                    "instrument_id": mapping.get("instrument_id"),
                    "instrument_type": instrument_type,
                    "message": "This instrument is not an option leg",
                },
            )

        expiry = _expiry_iso(mapping.get("expiry"))
        if not expiry:
            raise ValidationRefusal(
                "EXPIRY_UNAVAILABLE",
                {
                    "leg_index": index,
                    "instrument_id": mapping.get("instrument_id"),
                    "message": "An option leg requires an expiry to be settlable",
                },
            )

        lot_size = _as_lot_size(mapping.get("lot_size"))
        if not lot_size:
            raise ValidationRefusal(
                "OPTION_LEG_UNRESOLVED",
                {
                    "leg_index": index,
                    "instrument_id": mapping.get("instrument_id"),
                    "lot_size": mapping.get("lot_size"),
                    "message": "The catalog provides no lot size, so the leg cannot be sized",
                },
            )

        ratio = _as_ratio(leg.get("ratio"))
        side = str(leg.get("side") or "BUY").upper()
        if side not in ("BUY", "SELL"):
            raise ValidationRefusal("PAYLOAD_INVALID", {"leg_index": index, "side": side})
        quantity = lot_size * ratio
        reference_price = _as_optional_float(leg.get("reference_price"))
        if reference_price is None:
            reference_price = _as_optional_float(mapping.get("reference_price"))

        return {
            "instrument_id": str(mapping["instrument_id"]),
            "exchange": str(mapping.get("exchange") or ""),
            "tradingsymbol": str(mapping.get("tradingsymbol") or ""),
            "broker_exchange": str(mapping.get("broker_exchange") or ""),
            "broker_symbol": str(mapping.get("broker_symbol") or ""),
            "broker_token": mapping.get("broker_token"),
            "product": product,
            "instrument_type": instrument_type,
            "option_type": option_type,
            "strike": _as_optional_float(mapping.get("strike")),
            "expiry": expiry,
            "lot_size": lot_size,
            "ratio": ratio,
            "side": side,
            "quantity": quantity,
            "signed_quantity": quantity if side == "BUY" else -quantity,
            "reference_price": reference_price,
            "selection": dict(selection) if selection else None,
        }

    def _resolve_direct(
        self, *, index: int, leg: Mapping[str, Any], pinned: PinnedCatalogRead
    ) -> Dict[str, Any]:
        token = leg.get("instrument_token")
        if token is None:
            raise ValidationRefusal(
                "PAYLOAD_INVALID",
                {
                    "leg_index": index,
                    "missing_fields": ["instrument_token or selection"],
                },
            )
        try:
            broker_token = int(token)
        except (TypeError, ValueError) as exc:
            raise ValidationRefusal("PAYLOAD_INVALID", {"leg_index": index, "reason": str(exc)}) from exc
        mapping = pinned.resolve_token(
            broker_token,
            exchange=str(leg.get("exchange") or "NFO"),
            symbol=str(leg.get("tradingsymbol") or ""),
        )
        if mapping is None:
            raise ValidationRefusal(
                "OPTION_LEG_UNRESOLVED",
                {
                    "leg_index": index,
                    "instrument_token": broker_token,
                    "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                    "catalog_generation": pinned.pin(),
                },
            )
        return dict(mapping)

    def _resolve_selection(
        self,
        *,
        index: int,
        selection: Mapping[str, Any],
        pinned: PinnedCatalogRead,
        underlying: str,
        structure_expiry: Optional[str],
        chain_resolver: Optional[ChainResolver],
    ) -> Dict[str, Any]:
        option_type = str(selection.get("option_type") or "").upper()
        if option_type not in OPTION_INSTRUMENT_TYPES:
            raise ValidationRefusal(
                "SELECTION_POLICY_UNRESOLVABLE",
                {
                    "leg_index": index,
                    "option_type": str(selection.get("option_type") or ""),
                    "allowed": list(OPTION_INSTRUMENT_TYPES),
                },
            )
        if chain_resolver is None:
            # No chain access and no direct contract: refusing beats guessing a
            # strike, because a guess silently builds a different structure.
            raise ValidationRefusal(
                "SELECTION_POLICY_UNRESOLVABLE",
                {
                    "leg_index": index,
                    "message": "No chain resolver is available to satisfy this selection",
                },
            )
        try:
            chosen = chain_resolver(
                underlying=underlying,
                expiry=structure_expiry,
                option_type=option_type,
                moneyness=str(selection.get("moneyness") or "ATM").upper(),
                offset=int(selection.get("offset") or 0),
            )
        except Exception:  # noqa: BLE001 - a failing chain is an unresolvable policy
            chosen = None
        if not chosen:
            raise ValidationRefusal(
                "SELECTION_POLICY_UNRESOLVABLE",
                {
                    "leg_index": index,
                    "selection": dict(selection),
                    "underlying": underlying,
                    "expiry": structure_expiry,
                },
            )
        # The selection names a contract; the PIN still decides its identity.
        token = chosen.get("instrument_token")
        if token is None:
            return dict(chosen)
        mapping = pinned.resolve_token(
            int(token),
            exchange=str(chosen.get("exchange") or "NFO"),
            symbol=str(chosen.get("tradingsymbol") or ""),
        )
        if mapping is None:
            raise ValidationRefusal(
                "SELECTION_POLICY_UNRESOLVABLE",
                {
                    "leg_index": index,
                    "instrument_token": int(token),
                    "catalog_generation": pinned.pin(),
                    "message": "The selected contract is not in the pinned generation",
                },
            )
        return dict(mapping)

    # -- expiry policy ------------------------------------------------------

    @staticmethod
    def _expiry_policy(
        payload: Mapping[str, Any], *, legs: Sequence[Mapping[str, Any]]
    ) -> str:
        declared = payload.get("expiry_policy")
        if declared is not None:
            policy = str(declared)
            if policy not in EXPIRY_POLICIES:
                raise ValidationRefusal(
                    "PAYLOAD_INVALID",
                    {"expiry_policy": policy, "allowed": list(EXPIRY_POLICIES)},
                )
        else:
            # Short legs are the ones with a liability to close, so a structure
            # holding one exits before the cutoff unless it says otherwise.
            has_short = any(str(leg.get("side")) == "SELL" for leg in legs)
            policy = "exit_before_cutoff" if has_short else "allow_cash_settlement"

        if policy in PHYSICAL_POLICIES:
            capability = payload.get("settlement_capability")
            if not isinstance(capability, Mapping) or not capability:
                # Refused at PLAN time: by the last session it is not a choice.
                raise ValidationRefusal(
                    "PHYSICAL_SETTLEMENT_CAPABILITY_REQUIRED",
                    {
                        "expiry_policy": policy,
                        "message": (
                            "Physical settlement requires delivery and funding capability "
                            "evidence at plan time; without it the structure must exit "
                            "before the cutoff"
                        ),
                    },
                )
        return policy

    @staticmethod
    def _structure_digest(
        *,
        underlying: str,
        expiry: Optional[str],
        legs: Sequence[Mapping[str, Any]],
    ) -> str:
        """A stable identity for the frozen structure.

        Sorted so leg ORDER cannot change the digest, and covering side, strike and
        option type so a different structure never shares one.
        """
        payload = {
            "underlying": underlying,
            "expiry": expiry,
            "legs": sorted(
                (
                    {
                        "instrument_id": str(leg.get("instrument_id") or ""),
                        "option_type": str(leg.get("option_type") or ""),
                        "strike": leg.get("strike"),
                        "side": str(leg.get("side") or ""),
                        "ratio": int(leg.get("ratio") or 0),
                        "product": str(leg.get("product") or ""),
                    }
                    for leg in legs
                ),
                key=lambda item: (item["instrument_id"], item["side"], item["ratio"]),
            ),
        }
        return sha256_text(canonical_json(payload))
