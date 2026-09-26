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

#: The frozen per-leg roles (``side`` stays the authority; a role is coverage).
LEG_ROLES = ("hedge", "short", "naked")

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


def _as_structure_units(value: Any) -> int:
    """The size multiplier: a positive whole number, defaulting to one."""
    try:
        numeric = float(value if value is not None else 1)
    except (TypeError, ValueError) as exc:
        raise ValidationRefusal("PAYLOAD_INVALID", {"reason": str(exc)}) from exc
    if numeric != int(numeric) or int(numeric) <= 0:
        raise ValidationRefusal(
            "PAYLOAD_INVALID",
            {
                "structure_units": value,
                "message": "structure_units must be a positive whole number",
            },
        )
    return int(numeric)


def _as_positive_int(value: Any, *, field: str) -> int:
    """Required positive whole number (the adjust generation basis)."""
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationRefusal("PAYLOAD_INVALID", {field: value, "reason": str(exc)}) from exc
    if numeric != int(numeric) or int(numeric) <= 0:
        raise ValidationRefusal(
            "PAYLOAD_INVALID",
            {field: value, "message": f"{field} must be a positive whole number"},
        )
    return int(numeric)


def _as_optional_object(value: Any, *, field: str) -> Optional[Dict[str, Any]]:
    """An optional frozen sub-object (``protection_policy`` / ``max_loss``)."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationRefusal(
            "PAYLOAD_INVALID", {field: value, "message": f"{field} must be an object"}
        )
    return dict(value)


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
        structure_units = _as_structure_units(payload.get("structure_units"))
        protection_policy = _as_optional_object(
            payload.get("protection_policy"), field="protection_policy"
        )
        if protection_policy is not None and "naked" in protection_policy:
            # A naked declaration is a boolean; anything else would make the
            # naked gate read a value the compiler never validated.
            if not isinstance(protection_policy["naked"], bool):
                raise ValidationRefusal(
                    "PAYLOAD_INVALID",
                    {
                        "protection_policy": protection_policy,
                        "message": "protection_policy.naked must be a boolean",
                    },
                )
        max_loss = _as_optional_object(payload.get("max_loss"), field="max_loss")

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
                    structure_units=structure_units,
                    chain_resolver=chain_resolver,
                )
            )

        expiry_policy = self._expiry_policy(payload, legs=resolved_legs)
        phase, option_run_id, based_on_generation = self._option_run_binding(payload)
        if phase == "adjust":
            # The desired TARGET each leg must converge to. Sign comes from
            # ``side``: ``desired_quantity`` is the unsigned magnitude, exactly
            # like the legacy ``quantity`` field it mirrors.
            for leg in resolved_legs:
                leg["desired_quantity"] = int(leg["quantity"])
        digest = self._structure_digest(
            underlying=underlying, expiry=structure_expiry, legs=resolved_legs
        )

        option_run: Dict[str, Any] = {"phase": phase, "option_run_id": option_run_id}
        frozen_option_run = dict(option_run)
        if phase == "adjust":
            # The generation the strategy observed. Frozen in the RESOLVED block
            # so a stale basis refuses instead of being recomputed.
            frozen_option_run["based_on_generation"] = based_on_generation

        logical: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "underlying": underlying,
            "expiry": structure_expiry,
            "product": product,
            "structure_id": str(payload.get("structure_id") or ""),
            "expiry_policy": expiry_policy,
            "option_run": option_run,
            "legs": [self._logical_leg(leg) for leg in resolved_legs],
        }
        resolved: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "catalog_generation": pinned.pin(),
            "underlying": underlying,
            "expiry": structure_expiry,
            "structure_id": str(payload.get("structure_id") or ""),
            "structure_digest": digest,
            "expiry_policy": expiry_policy,
            # Frozen here so the EXECUTION-time binding is authoritative: an exit
            # plan carries the option-run reference it closes; an entry plan
            # creates the run. Re-deciding this at execution would let a caller
            # change which structure a plan closes.
            "option_run": frozen_option_run,
            "legs": resolved_legs,
        }
        # Legacy entry/exit payloads must freeze byte-identically, so a key with
        # a default is only written when the payload supplied it (or this is an
        # adjust, which has no legacy shape to preserve).
        if "structure_units" in payload or phase == "adjust":
            resolved["structure_units"] = structure_units
        if protection_policy is not None:
            logical["protection_policy"] = protection_policy
            resolved["protection_policy"] = protection_policy
        if max_loss is not None:
            logical["max_loss"] = max_loss
            resolved["max_loss"] = max_loss
        return ResolvedPlan(target_kind=self.target_kind, logical=logical, resolved=resolved)

    # -- run binding --------------------------------------------------------

    @staticmethod
    def _logical_leg(leg: Mapping[str, Any]) -> Dict[str, Any]:
        """The logical leg: identity, direction and size ratio (and role)."""
        logical_leg: Dict[str, Any] = {
            "option_type": leg["option_type"],
            "strike": leg["strike"],
            "side": leg["side"],
            "ratio": leg["ratio"],
        }
        if leg.get("role") is not None:
            logical_leg["role"] = leg["role"]
        return logical_leg

    @staticmethod
    def _option_run_binding(
        payload: Mapping[str, Any],
    ) -> tuple[str, Optional[str], Optional[int]]:
        """The frozen phase, the option-run reference it acts on, and the basis.

        The reference is a lookup key, never authority: the executor re-validates
        ownership, environment and leg identity against the durable run. But it
        must be frozen, because choosing it later would let a caller point a plan
        at a structure it never described.

        ``adjust`` names the run it mutates exactly as ``exit`` does, and freezes
        the generation it observed so a stale basis refuses instead of being
        recomputed against a newer run.
        """
        declared_phase = payload.get("phase")
        reference = payload.get("option_run_id")
        basis = payload.get("based_on_generation")
        block = payload.get("option_run")
        if isinstance(block, Mapping):
            declared_phase = block.get("phase", declared_phase)
            reference = block.get("option_run_id", reference)
            basis = block.get("based_on_generation", basis)
        phase = None if declared_phase in (None, "") else str(declared_phase).strip().lower()
        option_run_id = None if reference in (None, "") else str(reference).strip()
        if phase is None:
            # A reference implies the close it describes, none implies entry.
            # ``adjust`` is NEVER inferred: a resize must name its phase.
            phase = "exit" if option_run_id else "entry"
        if phase not in ("entry", "exit", "adjust"):
            raise ValidationRefusal(
                "PAYLOAD_INVALID",
                {"phase": phase, "allowed": ["entry", "exit", "adjust"]},
            )
        if phase == "exit" and not option_run_id:
            raise ValidationRefusal(
                "OPTION_EXIT_REFERENCE_REQUIRED",
                {
                    "message": (
                        "An exit plan must reference the option run it closes; the "
                        "reference is validated at execution, never trusted"
                    )
                },
            )
        if phase == "adjust" and not option_run_id:
            raise ValidationRefusal(
                "OPTION_ADJUSTMENT_REFERENCE_REQUIRED",
                {
                    "message": (
                        "An adjust plan must reference the option run it changes; the "
                        "reference is validated at execution, never trusted"
                    )
                },
            )
        based_on_generation: Optional[int] = None
        if phase == "adjust":
            if basis in (None, ""):
                raise ValidationRefusal(
                    "OPTION_ADJUSTMENT_BASIS_REQUIRED",
                    {
                        "message": (
                            "An adjust plan must freeze the option-run generation it "
                            "observed; the basis is what makes a stale adjustment refuse"
                        )
                    },
                )
            based_on_generation = _as_positive_int(basis, field="based_on_generation")
        return phase, option_run_id, based_on_generation

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
        structure_units: int,
        chain_resolver: Optional[ChainResolver],
    ) -> Dict[str, Any]:
        """Resolve one leg against the pin and size it.

        ``quantity`` is the UNSIGNED magnitude ``lot_size * ratio *
        structure_units``, and ``signed_quantity`` carries the sign of ``side``.
        The frozen ``desired_quantity`` of an adjust leg mirrors ``quantity`` for
        the same reason: ``side`` stays the authority on direction.
        """
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
        role_raw = leg.get("role")
        role: Optional[str] = None
        if role_raw not in (None, ""):
            role = str(role_raw).strip().lower()
            if role not in LEG_ROLES:
                raise ValidationRefusal(
                    "PAYLOAD_INVALID",
                    {"leg_index": index, "role": str(role_raw), "allowed": list(LEG_ROLES)},
                )
        quantity = lot_size * ratio * structure_units
        reference_price = _as_optional_float(leg.get("reference_price"))
        if reference_price is None:
            reference_price = _as_optional_float(mapping.get("reference_price"))

        resolved_leg: Dict[str, Any] = {
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
        if role is not None:
            resolved_leg["role"] = role
        return resolved_leg

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
        delta_target = selection.get("delta_target")
        if delta_target is None:
            delta_target = selection.get("target_delta")
        resolver_kwargs: Dict[str, Any] = {
            "underlying": underlying,
            "expiry": structure_expiry,
            "option_type": option_type,
            "moneyness": str(selection.get("moneyness") or "ATM").upper(),
        }
        if delta_target is not None:
            # A delta target names the contract by its Greek, not by strike
            # distance, so the offset the moneyness/offset pair would otherwise
            # carry is not sent -- the resolver picks one or the other.
            resolver_kwargs["delta_target"] = _as_optional_float(delta_target)
        else:
            resolver_kwargs["offset"] = int(selection.get("offset") or 0)
        try:
            chosen = chain_resolver(**resolver_kwargs)
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
