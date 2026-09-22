"""Compiler primitives: pinned reads, refusals, hashing and the ``ResolvedPlan``.

Two things make a frozen plan frozen (R3 §7):

1. **Resolution happens once**, against a pinned catalog generation, so a plan
   records both the logical representation and the fully resolved execution
   representation. Downstream execution never reinterprets it against a newer
   generation.
2. **The pin is part of the plan's identity.** ``plan_hash`` covers the logical
   plan, the resolved plan *and* the pin, so a plan produced under one generation
   can never be confused with the same decision under another.

Nothing here places orders, computes capital or decides admission. A resolved
plan is inert by construction: it names canonical instruments and signed
quantities, and nothing in this phase consumes it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol

from backend.broker_api.instruments.catalog import PinnedInstrumentCatalog

#: Refusal vocabulary raised by validation and compilation. ``LEG_KIND_UNSUPPORTED``
#: arrived with the ``intent_bundle`` compiler (D-6): futures/option legs are
#: Projects 9/10 and refuse at validation, never at execution time.
REFUSAL_REASONS = (
    "CATALOG_GENERATION_NOT_PUBLISHED",
    "INSTRUMENT_UNRESOLVED",
    "UNIVERSE_MEMBER_UNRESOLVED",
    "UNIVERSE_REVISION_UNKNOWN",
    "TARGET_KIND_UNKNOWN",
    "PAYLOAD_INVALID",
    "LEG_KIND_UNSUPPORTED",
    "MIS_OVERNIGHT_REFUSED",
    "CONTRACT_UNRESOLVED",
    "EXPIRY_UNAVAILABLE",
    "FREEZE_LIMIT_EXCEEDED",
    "OPTION_LEG_UNRESOLVED",
    "SELECTION_POLICY_UNRESOLVABLE",
    "PHYSICAL_SETTLEMENT_CAPABILITY_REQUIRED",
)


class ValidationRefusal(Exception):
    """A validation refusal that ends the evaluation.

    Refusal is terminal by design (D-1): the envelope is journalled, marked
    ``refused``, and that evaluation identity is spent. A corrected submission
    requires a new ``evaluation_id`` — the platform never invents one, and it
    never converts a refusal into a silent retry.
    """

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"rejection_reason": self.reason_code}
        payload.update(self.detail)
        return payload


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, no NaN.

    The repo-wide convention for any hashed document (``identity_key`` in the
    catalog, ``canonical_json`` in the workflow compiler).
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pinned_units(mapping: Mapping[str, Any]) -> Dict[str, Any]:
    """The executed unit of a leg, frozen with the plan (R3 §7).

    Resolution reads the catalog once; the executed quantity must come from the
    same artifact. A catalog that provides no lot for a cash-equity listing means
    "one unit", and that decision is *recorded* (``lot_source: default``) rather
    than silently re-derived at execution time from a catalog that may have moved
    since the plan was frozen.
    """
    raw = mapping.get("lot_size")
    try:
        lot = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        lot = 1
    if lot <= 0:
        lot = 1
    return {"lot_size": lot, "lot_source": "catalog" if raw is not None else "default"}


def member_hash(members: List[str]) -> str:
    """Canonical hash of a resolved member set (sorted, deduplicated)."""
    return sha256_text(canonical_json(sorted({str(item).upper() for item in members})))


def plan_pin(
    *,
    pinned_catalog_generation: str,
    pinned_universe_revision_id: Optional[str] = None,
    pinned_member_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """The plan's input pin. Part of ``plan_hash``, so a pin change is a new plan."""
    pin: Dict[str, Any] = {"catalog_generation": str(pinned_catalog_generation)}
    if pinned_universe_revision_id is not None:
        pin["universe_revision_id"] = str(pinned_universe_revision_id)
    if pinned_member_hash is not None:
        pin["member_hash"] = str(pinned_member_hash)
    return pin


def compute_plan_hash(*, logical: Any, resolved: Any, pin: Mapping[str, Any]) -> str:
    """``sha256`` over the canonical JSON of logical + resolved + pin.

    Stable for identical inputs and discriminating for any change to any of the
    three — which is what lets Phase 4 bind an approval to "this exact plan" and
    what lets a rebuild prove it reproduced the same artifact.
    """
    return sha256_text(
        canonical_json(
            {
                "logical": logical,
                "resolved": resolved,
                "pin": dict(pin),
            }
        )
    )


@dataclass(frozen=True)
class ResolvedPlan:
    """The compiled artifact, before it is persisted and hashed."""

    target_kind: str
    logical: Dict[str, Any]
    resolved: Dict[str, Any]
    universe_revision_id: Optional[str] = None
    member_hash: Optional[str] = None
    fields: Dict[str, Any] = field(default_factory=dict)


class TargetCompiler(Protocol):
    """A ``target_kind`` → resolved plan compiler."""

    target_kind: str

    def compile(self, payload: Mapping[str, Any], pinned: "PinnedCatalogRead") -> ResolvedPlan:
        ...


class PinnedCatalogRead:
    """Resolution-time catalog access, bound to one published generation.

    ``generation=None`` means "pin whatever is published now" (D-3); an explicit
    generation must itself be published, so a plan can never be resolved against
    a staging catalog. The validation is cached: ``pin()`` is called during
    validation and again when the plan row is written, and it must not be a
    second, differently-timed lookup.
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
        *,
        generation: Optional[str] = None,
        catalog: Optional[PinnedInstrumentCatalog] = None,
        broker: str = "kite",
    ) -> None:
        self.session_factory = session_factory
        self._catalog = catalog or PinnedInstrumentCatalog(
            session_factory, broker=broker, generation=generation
        )
        self._requested = str(generation) if generation else None
        self._resolved_generation: Optional[str] = None
        self._published_at: Any = None

    # -- the pin ------------------------------------------------------------

    def current_published_generation(self) -> Optional[str]:
        return self._catalog.current_published_generation()

    def pin(self) -> str:
        """The generation this read resolves against, or a refusal."""
        if self._resolved_generation is not None:
            return self._resolved_generation

        candidate = self._requested or self.current_published_generation()
        if not candidate:
            raise ValidationRefusal(
                "CATALOG_GENERATION_NOT_PUBLISHED",
                {"catalog_generation": self._requested, "message": "No published catalog generation"},
            )
        row = self._catalog.generation_row(candidate)
        if row is None or str(row.get("status") or "") != "published":
            raise ValidationRefusal(
                "CATALOG_GENERATION_NOT_PUBLISHED",
                {
                    "catalog_generation": str(candidate),
                    "status": None if row is None else str(row.get("status") or ""),
                },
            )
        self._resolved_generation = str(candidate)
        self._published_at = row.get("published_at")
        return self._resolved_generation

    @property
    def generation_id(self) -> Optional[str]:
        """The pinned id, or ``None`` if it has not been resolved yet."""
        return self._resolved_generation or self._requested

    # -- reads --------------------------------------------------------------

    def resolve_token(
        self, broker_token: int, *, exchange: Optional[str] = None, symbol: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        return self._resolve(broker_token=broker_token, exchange=exchange, symbol=symbol)

    def resolve_symbol(self, exchange: str, symbol: str) -> Optional[Dict[str, Any]]:
        return self._resolve(exchange=exchange, symbol=symbol)

    def _resolve(
        self,
        *,
        broker_token: Optional[int] = None,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        self.pin()
        rows = self._catalog.mappings_as_of(
            self._published_at, broker_token=broker_token, broker_exchange=exchange,
            broker_symbol=symbol,
        )
        if len(rows) != 1:
            # Zero rows: nothing was mapped then. More than one: the pin does not
            # identify a single listing, which is not something to guess at.
            return None
        row = rows[0]
        return {
            "instrument_id": str(row["instrument_id"]),
            "exchange": str(row["exchange"] or ""),
            "tradingsymbol": str(row["tradingsymbol"] or ""),
            "broker_exchange": str(row["broker_exchange"] or ""),
            "broker_symbol": str(row["broker_symbol"] or ""),
            "broker_token": int(row["broker_token"]),
            "lifecycle_status": str(row["lifecycle_status"] or ""),
            # Derivative metadata, carried for the futures compiler. Additive: the
            # equity compilers simply never read these keys.
            "instrument_type": str(row.get("instrument_type") or ""),
            "expiry": row.get("expiry"),
            "lot_size": row.get("lot_size"),
            "tick_size": row.get("tick_size"),
            "underlying": str(row.get("underlying") or ""),
            "strike": row.get("strike"),
            "option_type": str(row.get("option_type") or ""),
            "catalog_generation": self.pin(),
        }

    def lifecycle(self, instrument_id: str) -> Optional[str]:
        """Lifecycle *now* — used by invalidation and by resolution's active check."""
        return self._catalog.lifecycle_for_instrument(str(instrument_id))

    def record(self, instrument_id: str) -> Optional[Mapping[str, Any]]:
        return self._catalog.lifecycle_row(str(instrument_id))
