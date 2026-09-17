"""Target compilers: logical payload → resolved plan against a pinned catalog.

Phase 3 ships exactly two reference compilers. Futures and option-structure
compilers belong to Projects 9/10 and are deliberately **not** stubbed: an
unknown ``target_kind`` is refused (``TARGET_KIND_UNKNOWN``) rather than silently
resolved by a placeholder that could later be mistaken for a real contract.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from backend.strategies.compiler.base import (
    REFUSAL_REASONS,
    PinnedCatalogRead,
    ResolvedPlan,
    TargetCompiler,
    ValidationRefusal,
    canonical_json,
    compute_plan_hash,
    member_hash,
    plan_pin,
    sha256_text,
)
from backend.strategies.compiler.single_instrument import SingleInstrumentCompiler

__all__ = [
    "REFUSAL_REASONS",
    "PinnedCatalogRead",
    "ResolvedPlan",
    "TargetCompiler",
    "ValidationRefusal",
    "canonical_json",
    "compile_plan",
    "compiler_for",
    "compute_plan_hash",
    "member_hash",
    "plan_pin",
    "sha256_text",
]


def _registry() -> Dict[str, TargetCompiler]:
    """Every shipped compiler, keyed by target kind.

    Extended by later phases (futures Project 9, option structures Project 10).
    Keeping this a plain dict makes an unshipped kind a refusal rather than a
    silently missing feature.
    """
    return {
        SingleInstrumentCompiler.target_kind: SingleInstrumentCompiler(),
    }


def compiler_for(target_kind: str) -> TargetCompiler:
    """The compiler for a target kind, or ``TARGET_KIND_UNKNOWN``."""
    kind = str(target_kind or "").strip()
    compiler = _registry().get(kind)
    if compiler is None:
        raise ValidationRefusal("TARGET_KIND_UNKNOWN", {"target_kind": kind})
    return compiler


def compile_plan(
    target_kind: str, payload: Mapping[str, Any], pinned: PinnedCatalogRead
) -> Dict[str, Any]:
    """Compile a payload to its resolved representation.

    Returns the *resolved* dict only. The caller assembles the frozen plan (both
    representations plus the pin, hashed) so compilation stays a pure function of
    payload and pin — the same inputs must always produce the same plan.
    """
    return compiler_for(target_kind).compile(payload, pinned).resolved


def compile_resolved_plan(
    target_kind: str, payload: Mapping[str, Any], pinned: PinnedCatalogRead
) -> ResolvedPlan:
    """Compile and return both representations plus any scope the compiler pinned."""
    return compiler_for(target_kind).compile(payload, pinned)
