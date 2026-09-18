"""``intent_bundle``: bounded bundles of explicit per-leg actions (D-6).

A bundle payload carries a list of single-instrument legs; each resolves
EXACTLY like a ``single_instrument`` leg (same coordinate resolution, same
pinned generation, same signed-quantity semantics), so the executor consumes
bundle legs and standalone legs through one code path. The bundle adds no new
execution semantics: per-leg outcomes are events, and all-or-nothing submission
is deliberately NOT implied.

Leg kinds are the fail-closed gate of this phase: futures and option
structures belong to Projects 9/10, so a leg whose catalog record is not a
cash-equity instrument refuses ``LEG_KIND_UNSUPPORTED``. An unknown
``instrument_type`` is not evidence of cash equity and refuses the same way —
unknown evidence never compiles into a plan the executor could act on.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from sqlalchemy import text

from backend.strategies.compiler.base import (
    PinnedCatalogRead,
    ResolvedPlan,
    TargetCompiler,
    ValidationRefusal,
)
from backend.strategies.compiler.single_instrument import SingleInstrumentCompiler

#: The only instrument kind this phase's paper execution can carry.
SUPPORTED_INSTRUMENT_KINDS = ("EQ",)


class IntentBundleCompiler(TargetCompiler):
    target_kind = "intent_bundle"

    def compile(self, payload: Mapping[str, Any], pinned: PinnedCatalogRead) -> ResolvedPlan:
        legs_payload = payload.get("legs")
        if not isinstance(legs_payload, list) or not legs_payload:
            raise ValidationRefusal(
                "PAYLOAD_INVALID",
                {
                    "missing_fields": ["legs"],
                    "message": "An intent bundle requires a non-empty legs list",
                },
            )

        single = SingleInstrumentCompiler()
        resolved_legs: list = []
        logical_legs: list = []
        for index, leg_payload in enumerate(legs_payload):
            if not isinstance(leg_payload, Mapping):
                raise ValidationRefusal(
                    "PAYLOAD_INVALID", {"reason": f"legs[{index}] is not an object"}
                )
            compiled = single.compile(leg_payload, pinned)
            leg = compiled.resolved["legs"][0]
            kind = self._instrument_kind(pinned, str(leg.get("instrument_id") or ""))
            if kind not in SUPPORTED_INSTRUMENT_KINDS:
                raise ValidationRefusal(
                    "LEG_KIND_UNSUPPORTED",
                    {
                        "leg_index": index,
                        "instrument_id": leg.get("instrument_id"),
                        "tradingsymbol": leg.get("tradingsymbol"),
                        "instrument_type": kind,
                        "message": (
                            "Only cash-equity legs execute in this phase; futures and "
                            "option structures are separate projects"
                        ),
                    },
                )
            resolved_legs.append(leg)
            logical_legs.append(compiled.logical)

        resolved: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "catalog_generation": pinned.pin(),
            "legs": resolved_legs,
        }
        logical: Dict[str, Any] = {
            "target_kind": self.target_kind,
            "legs": logical_legs,
        }
        return ResolvedPlan(target_kind=self.target_kind, logical=logical, resolved=resolved)

    @staticmethod
    def _instrument_kind(pinned: PinnedCatalogRead, instrument_id: str) -> str:
        """The catalog record's instrument type, or ``""`` when unknown.

        ``instrument_catalog_records`` is a pre-existing platform table without
        an ORM model, so the read is ``public.``-qualified SQL (established
        pattern); an absent row or a NULL kind is unknown evidence and the
        caller refuses rather than guesses.
        """
        if not instrument_id:
            return ""
        session_factory = getattr(pinned, "session_factory", None)
        if session_factory is None:
            return ""
        try:
            with session_factory() as db:
                row = db.execute(
                    text(
                        "SELECT instrument_type FROM public.instrument_catalog_records "
                        "WHERE instrument_id = :iid"
                    ),
                    {"iid": instrument_id},
                ).fetchone()
        except Exception:  # noqa: BLE001 - unavailable evidence is not equity
            return ""
        if row is None:
            return ""
        value = row[0]
        return str(value or "").strip().upper()
