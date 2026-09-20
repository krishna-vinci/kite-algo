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

    Extended by later phases (option structures Project 10). Keeping this a plain
    dict makes an unshipped kind a refusal rather than a silently missing feature.
    """
    from backend.strategies.compiler.futures import FuturesCompiler
    from backend.strategies.compiler.intent_bundle import IntentBundleCompiler
    from backend.strategies.compiler.option_structure import OptionStructureCompiler
    from backend.strategies.compiler.target_weights import TargetWeightsCompiler

    return {
        SingleInstrumentCompiler.target_kind: SingleInstrumentCompiler(),
        TargetWeightsCompiler.target_kind: TargetWeightsCompiler(),
        IntentBundleCompiler.target_kind: IntentBundleCompiler(),
        FuturesCompiler.target_kind: FuturesCompiler(),
        OptionStructureCompiler.target_kind: OptionStructureCompiler(),
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
    target_kind: str,
    payload: Mapping[str, Any],
    pinned: PinnedCatalogRead,
    *,
    chain_resolver: Any = None,
) -> ResolvedPlan:
    """Compile and return both representations plus any scope the compiler pinned.

    ``chain_resolver`` is passed only to compilers that accept one (option
    structures need chain access for selection policies); the others are called
    with the two arguments they have always taken.
    """
    compiler = compiler_for(target_kind)
    if chain_resolver is not None:
        import inspect

        if "chain_resolver" in inspect.signature(compiler.compile).parameters:
            return compiler.compile(payload, pinned, chain_resolver=chain_resolver)
    return compiler.compile(payload, pinned)
