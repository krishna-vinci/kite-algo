"""Per-subscription feature plans for layered evaluation (Phase 2 F8).

Given a parsed workflow document, a plan describes, for one alert
subscription:

- the ancestor filter stages whose conditions must hold for the alert to
  fire (the layered chain), each with its own evaluation timeframe;
- every declared feature dependency as ``(timeframe, FeatureSpec)`` so the
  shared FeatureEngine can compute each identical dependency exactly once
  per market event.

Stage references (``{indicator: "stage:ema20"}``) resolve to feature stages
and contribute their timeframe's snapshot to the dispatch features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from backend.workflows.feature_engine import FeatureSpec
from backend.workflows.models import Stage, WorkflowDocument

__all__ = ["SubscriptionPlan", "build_subscription_plan", "stage_chain"]

_STAGE_REF_PREFIX = "stage:"


@dataclass(frozen=True)
class SubscriptionPlan:
    """Feature/layer wiring for one subscription."""

    layers: Tuple[Tuple[Stage, Optional[str]], ...] = ()  # (ancestor stage, timeframe)
    feature_timeframes: Tuple[str, ...] = ()  # timeframes to merge into features
    specs: Tuple[Tuple[str, FeatureSpec], ...] = field(default=())  # (timeframe, spec)
    # (alias "stage:<id>", timeframe, canonical feature id): dispatch features
    # are keyed by canonical ids, so predicates resolving "stage:<id>" read
    # the referenced stage's own-timeframe snapshot through these aliases.
    stage_aliases: Tuple[Tuple[str, str, str], ...] = field(default=())

    @property
    def has_features(self) -> bool:
        return bool(self.specs or self.feature_timeframes)


def stage_chain(document: WorkflowDocument, stage_id: str) -> List[Stage]:
    """Ancestor stages of ``stage_id`` (root first), bounded by validation."""
    by_id = {stage.id: stage for stage in document.stages}
    chain: List[Stage] = []
    cursor = by_id.get(stage_id)
    seen = {stage_id}
    while cursor is not None and cursor.input is not None:
        parent = by_id.get(cursor.input)
        if parent is None or parent.id in seen:
            break  # validation already reports missing/cyclic references
        chain.insert(0, parent)
        seen.add(parent.id)
        cursor = parent
    return chain


def _collect_operand_specs(
    operand, timeframe: str, out: Set[Tuple[str, str, FeatureSpec]], depth: int = 0
) -> None:
    """Collect feature specs from one operand, descending arithmetic trees."""
    if operand is None or operand.kind != "indicator" or depth > 6:
        return
    if operand.name and operand.name.startswith(_STAGE_REF_PREFIX):
        return  # stage references handled by the caller
    if operand.name:
        out.add(
            (
                timeframe,
                "inline",
                FeatureSpec(
                    function=operand.name,
                    params=dict(operand.params or {}),
                    source=operand.source or "close",
                    offset=operand.offset or 0,
                    output=operand.params.get("output"),
                ),
            )
        )
        return
    # expression operand: descend into the argument tree
    from backend.workflows.compiler import _coerce_expression_arg

    for value in (operand.params or {}).values():
        if not isinstance(value, list):
            continue
        for arg in value:
            _collect_operand_specs(_coerce_expression_arg(arg), timeframe, out, depth + 1)


def _operand_specs(stage: Stage, out: Set[Tuple[str, str, FeatureSpec]]) -> None:
    timeframe = stage.timeframe or "day"
    groups = (
        stage.conditions,
        stage.any_conditions,
        stage.not_conditions,
    )
    for group in groups:
        for cond in group:
            for operand in (cond.left, cond.right):
                _collect_operand_specs(operand, timeframe, out)


def build_subscription_plan(document: WorkflowDocument, stage_id: str) -> SubscriptionPlan:
    """Extract the layer chain and declared features for one subscription."""
    stage = next((s for s in document.stages if s.id == stage_id), None)
    if stage is None:
        return SubscriptionPlan()

    ancestors = stage_chain(document, stage_id)
    layers: List[Tuple[Stage, Optional[str]]] = [
        (ancestor, ancestor.timeframe) for ancestor in ancestors
    ]

    needs: Set[Tuple[str, str, FeatureSpec]] = set()
    aliases: Set[Tuple[str, str, str]] = set()
    own_tf = stage.timeframe
    for stage_or_layer in [stage] + ancestors:
        _operand_specs(stage_or_layer, needs)

    # stage references: {indicator: "stage:<id>"} pull the referenced feature
    # stage's timeframe snapshot into the dispatch features.
    referenced_timeframes: Set[str] = set()

    def _scan_refs(target_stage: Stage) -> None:
        for group in (
            target_stage.conditions,
            target_stage.any_conditions,
            target_stage.not_conditions,
        ):
            for cond in group:
                for operand in (cond.left, cond.right):
                    if (
                        operand is not None
                        and operand.kind == "indicator"
                        and operand.name
                        and operand.name.startswith(_STAGE_REF_PREFIX)
                    ):
                        ref_id = operand.name[len(_STAGE_REF_PREFIX):]
                        ref_stage = next(
                            (s for s in document.stages if s.id == ref_id), None
                        )
                        if ref_stage is not None and ref_stage.timeframe:
                            referenced_timeframes.add(ref_stage.timeframe)
                            needs_from_stage(ref_stage)

    def needs_from_stage(feature_stage: Stage) -> None:
        tf = feature_stage.timeframe or "day"
        if feature_stage.function:
            spec = FeatureSpec(
                function=feature_stage.function,
                params=dict(feature_stage.stage_params or {}),
                source=feature_stage.source_field or "close",
                offset=0,
                output=feature_stage.stage_params.get("output"),
            )
            needs.add((tf, f"stage:{feature_stage.id}", spec))
            aliases.add((f"stage:{feature_stage.id}", tf, spec.feature_id))

    for feature_stage in document.stages:
        if feature_stage.type == "feature" and feature_stage.function:
            spec = FeatureSpec(
                function=feature_stage.function,
                params=dict(feature_stage.stage_params or {}),
                source=feature_stage.source_field or "close",
                offset=0,
                output=(feature_stage.stage_params or {}).get("output"),
            )
            needs.add(
                (feature_stage.timeframe or "day", f"stage:{feature_stage.id}", spec)
            )
            aliases.add(
                (
                    f"stage:{feature_stage.id}",
                    feature_stage.timeframe or "day",
                    spec.feature_id,
                )
            )
    _scan_refs(stage)
    for ancestor in ancestors:
        _scan_refs(ancestor)

    # merge: (timeframe, feature_id) -> spec (dedupe identical features)
    merged: Dict[Tuple[str, str], FeatureSpec] = {}
    for timeframe, _tag, spec in needs:
        merged[(timeframe, spec.feature_id)] = spec

    feature_timeframes = {tf for tf, _ in merged}
    if own_tf:
        feature_timeframes.add(own_tf)
    feature_timeframes |= referenced_timeframes
    for _stage, tf in layers:
        if tf:
            feature_timeframes.add(tf)

    return SubscriptionPlan(
        layers=tuple(layers),
        feature_timeframes=tuple(sorted(feature_timeframes)),
        specs=tuple((tf, spec) for (tf, _), spec in merged.items()),
        stage_aliases=tuple(sorted(aliases)),
    )
