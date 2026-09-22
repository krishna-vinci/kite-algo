"""``target_weights``: a full-snapshot portfolio target over a pinned universe.

Full-snapshot semantics (R3 §7, D-6) are the whole point: the scope is the pinned
``(universe_revision_id, member_hash, catalog_generation)``, and **every** member
of the pinned revision appears in the resolved plan with an explicit target
weight. An instrument omitted from the payload *inside* the scope means **target
zero**, and an instrument *outside* the scope is untouched — no row is invented
for it.

That asymmetry is what makes the kind safe to re-submit: two submissions against
the same revision always describe the same set of instruments, so a target that
disappears from the payload cannot be mistaken for "leave it alone".

Weight→share/capital quantity math is deliberately absent: sizing is admission
(Project 4) and execution is Project 6+.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import text

from backend.strategies.admission import VALID_PRODUCTS
from backend.strategies.compiler.base import (
    pinned_units,
    PinnedCatalogRead,
    ResolvedPlan,
    TargetCompiler,
    ValidationRefusal,
    member_hash,
)

#: Default weight resolution: a member missing from the payload targets zero.
DEFAULT_WEIGHT = 0.0


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: A full-snapshot portfolio target is cash-equity unless the payload says
#: otherwise; the frozen product travels with the plan so execution never
#: re-decides it.
DEFAULT_PRODUCT = "CNC"


class TargetWeightsCompiler(TargetCompiler):
    target_kind = "target_weights"

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        self._session_factory = session_factory

    # -- scope --------------------------------------------------------------

    def _load_revision(
        self,
        revision_id: str,
        session_factory: Optional[Callable[[], Any]],
        pinned_session_factory: Optional[Callable[[], Any]] = None,
    ) -> Dict[str, Any]:
        """The persisted revision the ``worker_universes`` resolve path produced.

        Membership is read from the revision row rather than recomputed: the
        revision *is* the frozen scope, and recomputing it would resolve the
        universe against today's catalog — exactly the reinterpretation the pin
        exists to prevent.
        """
        factory = session_factory or self._session_factory or pinned_session_factory
        if factory is None:
            raise ValidationRefusal(
                "UNIVERSE_REVISION_UNKNOWN",
                {"universe_revision_id": str(revision_id), "reason": "no universe store configured"},
            )
        session = factory()
        try:
            row = session.execute(
                text(
                    "SELECT id, universe_id, revision, members, member_count, source_generation "
                    "FROM public.universe_revisions WHERE id = :revision_id"
                ),
                {"revision_id": str(revision_id)},
            ).mappings().first()
        finally:
            session.close()
        if row is None:
            raise ValidationRefusal(
                "UNIVERSE_REVISION_UNKNOWN", {"universe_revision_id": str(revision_id)}
            )
        import json as _json

        raw = row["members"]
        if isinstance(raw, str):
            raw = _json.loads(raw or "[]")
        members = [str(item).upper() for item in (raw or [])]
        return {
            "universe_revision_id": str(row["id"]),
            "universe_id": str(row["universe_id"]),
            "revision": int(row["revision"]),
            "members": members,
            "source_generation": str(row["source_generation"]) if row["source_generation"] else None,
        }

    # -- compile ------------------------------------------------------------

    def compile(
        self,
        payload: Mapping[str, Any],
        pinned: PinnedCatalogRead,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> ResolvedPlan:
        revision_id = payload.get("universe_revision_id")
        if not revision_id:
            raise ValidationRefusal(
                "PAYLOAD_INVALID", {"missing_fields": ["universe_revision_id"]}
            )
        weights = payload.get("target_weights")
        if weights is None or not isinstance(weights, Mapping):
            raise ValidationRefusal("PAYLOAD_INVALID", {"missing_fields": ["target_weights"]})

        revision = self._load_revision(
            str(revision_id), session_factory, pinned.session_factory
        )
        resolved_hash = member_hash(revision["members"])
        claimed = payload.get("member_hash")
        if claimed is not None and str(claimed) != resolved_hash:
            # The payload was built against a different membership than the
            # revision holds: refuse rather than resolve a target set that does
            # not match the scope it claims.
            raise ValidationRefusal(
                "UNIVERSE_MEMBER_UNRESOLVED",
                {
                    "universe_revision_id": revision["universe_revision_id"],
                    "claimed_member_hash": str(claimed),
                    "resolved_member_hash": resolved_hash,
                },
            )
        if payload.get("member_hash") is None:
            # The payload may name members directly instead of hashing them.
            declared = payload.get("members")
            if declared is not None:
                if sorted({str(item).upper() for item in declared}) != sorted(revision["members"]):
                    raise ValidationRefusal(
                        "UNIVERSE_MEMBER_UNRESOLVED",
                        {
                            "universe_revision_id": revision["universe_revision_id"],
                            "resolved_member_hash": resolved_hash,
                        },
                    )

        # The executed PRODUCT is frozen with the plan, exactly like the unit and
        # the price: the paper/live order payload needs one, and re-deciding it at
        # execution would make the same frozen plan orderable under a different
        # product. A full-snapshot portfolio target is cash-equity (CNC) unless the
        # payload names another valid product, which admission then re-validates.
        product = str(payload.get("product") or DEFAULT_PRODUCT).upper()
        if product not in VALID_PRODUCTS:
            raise ValidationRefusal(
                "PAYLOAD_INVALID",
                {"product": product, "valid_products": sorted(VALID_PRODUCTS)},
            )

        reference_prices = payload.get("reference_prices")
        if reference_prices is not None and not isinstance(reference_prices, Mapping):
            raise ValidationRefusal("PAYLOAD_INVALID", {"missing_fields": ["reference_prices"]})

        # The capital basis is FROZEN with the plan (the platform resolves it at
        # submission time and the compiler records it). Sizing against the live
        # admission policy at execution time would let a later policy change
        # silently re-size an already-approved target, which is exactly what a
        # frozen plan exists to prevent.
        try:
            capital_basis = float(payload.get("capital_basis_inr"))
        except (TypeError, ValueError) as exc:
            raise ValidationRefusal(
                "CAPITAL_BASIS_UNAVAILABLE",
                {"capital_basis_inr": payload.get("capital_basis_inr")},
            ) from exc
        if capital_basis <= 0:
            raise ValidationRefusal(
                "CAPITAL_BASIS_UNAVAILABLE", {"capital_basis_inr": capital_basis}
            )
        cash_buffer_pct = payload.get("cash_buffer_pct")
        try:
            cash_buffer_pct = 0.0 if cash_buffer_pct is None else float(cash_buffer_pct)
        except (TypeError, ValueError) as exc:
            raise ValidationRefusal(
                "PAYLOAD_INVALID", {"cash_buffer_pct": payload.get("cash_buffer_pct")}
            ) from exc
        if cash_buffer_pct < 0 or cash_buffer_pct >= 1:
            raise ValidationRefusal(
                "PAYLOAD_INVALID", {"cash_buffer_pct": cash_buffer_pct}
            )

        normalized: Dict[str, float] = {}
        for key, value in weights.items():
            try:
                normalized[str(key).upper()] = float(value)
            except (TypeError, ValueError) as exc:
                raise ValidationRefusal(
                    "PAYLOAD_INVALID", {"key": str(key), "reason": str(exc)}
                ) from exc

        outside = sorted(set(normalized) - set(revision["members"]))
        if outside:
            # A weight for an instrument outside the scope has no defined meaning:
            # the scope is the snapshot, so this is a malformed payload.
            raise ValidationRefusal("UNIVERSE_MEMBER_UNRESOLVED", {"outside_scope": outside})

        legs: List[Dict[str, Any]] = []
        for member in revision["members"]:
            weight = float(normalized.get(member, DEFAULT_WEIGHT))
            mapping = pinned.resolve_symbol("NSE", member)
            if mapping is None:
                raise ValidationRefusal(
                    "UNIVERSE_MEMBER_UNRESOLVED",
                    {"member": member, "catalog_generation": pinned.pin()},
                )
            if str(mapping.get("lifecycle_status") or "") != "active":
                raise ValidationRefusal(
                    "UNIVERSE_MEMBER_UNRESOLVED",
                    {"member": member, "lifecycle_status": str(mapping.get("lifecycle_status") or "")},
                )
            legs.append(
                {
                    "instrument_id": mapping["instrument_id"],
                    "exchange": mapping["exchange"],
                    "tradingsymbol": mapping["tradingsymbol"],
                    # The full broker coordinate, not just the symbol: derived
                    # invalidation resolves each leg's coordinate against the
                    # newest generation, and a leg missing its exchange cannot be
                    # compared at all — it would read as "unmapped" and invalidate
                    # a plan that nothing had actually changed.
                    "broker_exchange": mapping["broker_exchange"],
                    "broker_symbol": mapping["broker_symbol"],
                    "broker_token": mapping["broker_token"],
                    "product": product,
                    "target_weight": weight,
                    # Executed unit frozen with the plan (never re-read at
                    # execution): weights size to a quantity, which needs a lot.
                    **pinned_units(mapping),
                    # Per-member price for admission's notional arithmetic.
                    "reference_price": (
                        None
                        if not reference_prices
                        else _as_optional_float(reference_prices.get(member))
                    ),
                    # Omission inside the scope is an explicit zero, not an absence.
                    "explicit_zero": weight == 0.0,
                }
            )

        logical: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "universe_revision_id": revision["universe_revision_id"],
            "target_weights": {member: float(normalized.get(member, DEFAULT_WEIGHT)) for member in revision["members"]},
            # Frozen sizing inputs: the executable target is a pure function of
            # the plan (weight x basis x buffer / pinned price, floored to the
            # pinned lot), never of a later policy change.
            "capital_basis_inr": capital_basis,
            "cash_buffer_pct": cash_buffer_pct,
        }
        resolved: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "catalog_generation": pinned.pin(),
            "product": product,
            "universe_revision_id": revision["universe_revision_id"],
            "member_hash": resolved_hash,
            "capital_basis_inr": capital_basis,
            "cash_buffer_pct": cash_buffer_pct,
            "legs": legs,
        }
        return ResolvedPlan(
            target_kind=self.target_kind,
            logical=logical,
            resolved=resolved,
            universe_revision_id=revision["universe_revision_id"],
            member_hash=resolved_hash,
        )
