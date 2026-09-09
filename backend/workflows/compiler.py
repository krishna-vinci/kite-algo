"""Normalize -> validate -> CompiledWorkflow (canonical hash).

Validation is total: a document needing unavailable capabilities fails with a
list of ``ValidationIssue``s instead of being partially interpreted. The
canonical hash is the sha256 of the canonical JSON (sorted keys) of the full
document, so YAML -> doc -> JSON -> doc preserves the hash while any semantic
change (level, EMA period, ...) changes it.

Dependency-free: stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from . import registry
from .models import Condition, Operand, Stage, WorkflowDocument

__all__ = [
    "ValidationIssue",
    "WorkflowValidationError",
    "CompiledWorkflow",
    "compile_document",
    "canonical_json",
]

# sanity limits ("expression/depth/size")
MAX_STAGES = 64
MAX_ALERTS = 256
MAX_INSTRUMENTS = 1000
MAX_CONDITIONS_PER_STAGE = 32


@dataclass(frozen=True)
class ValidationIssue:
    where: str  # e.g. "stages.px.conditions[0]" or "alerts.breakout"
    code: str  # unknown_operator | unknown_field | unknown_capability |
    # missing_reference | cycle | duplicate_id | timeframe_missing |
    # timeframe_unsupported | bad_value
    message: str


class WorkflowValidationError(Exception):
    """Raised by compile_document; carries every collected issue."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues: list[ValidationIssue] = list(issues)
        summary = "; ".join(f"{i.where}: {i.code}" for i in self.issues[:5])
        super().__init__(f"{len(self.issues)} validation issue(s): {summary}")


@dataclass(frozen=True)
class CompiledWorkflow:
    document: WorkflowDocument
    canonical_hash: str  # sha256 of canonical JSON (sorted keys)

    def to_json(self) -> str:
        return canonical_json(self.document)


def canonical_json(doc: WorkflowDocument) -> str:
    """Deterministic JSON serialization (sorted keys, compact separators)."""
    return json.dumps(
        doc.to_document_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compile_document(doc: WorkflowDocument) -> CompiledWorkflow:
    """Validate the document and return its compiled form.

    Raises WorkflowValidationError with .issues naming every offending
    location when validation fails.
    """
    issues: list[ValidationIssue] = []
    _validate(doc, issues)
    if issues:
        raise WorkflowValidationError(issues)
    canonical = canonical_json(doc)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CompiledWorkflow(document=doc, canonical_hash=digest)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _validate(doc: WorkflowDocument, issues: list[ValidationIssue]) -> None:
    _add = issues.append

    if doc.version != 1:
        _add(ValidationIssue("document.version", "bad_value", f"unsupported schema version {doc.version!r}"))
    if not doc.name:
        _add(ValidationIssue("document.name", "bad_value", "workflow name must not be empty"))

    if len(doc.instruments) > MAX_INSTRUMENTS:
        _add(
            ValidationIssue(
                "document.instruments",
                "bad_value",
                f"too many instruments ({len(doc.instruments)} > {MAX_INSTRUMENTS})",
            )
        )
    if len(doc.stages) > MAX_STAGES:
        _add(
            ValidationIssue(
                "document.stages", "bad_value", f"too many stages ({len(doc.stages)} > {MAX_STAGES})"
            )
        )
    if len(doc.alerts) > MAX_ALERTS:
        _add(
            ValidationIssue(
                "document.alerts", "bad_value", f"too many alerts ({len(doc.alerts)} > {MAX_ALERTS})"
            )
        )

    stage_ids = _collect_ids(doc.stages, "stages", issues)
    _validate_stages(doc.stages, stage_ids, issues)
    alert_ids = _collect_ids(doc.alerts, "alerts", issues)
    _validate_alerts(doc.alerts, alert_ids, stage_ids, issues)


def _collect_ids(items: tuple, label: str, issues: list[ValidationIssue]) -> set[str]:
    ids: set[str] = set()
    for index, item in enumerate(items):
        where = f"{label}[{index}]"
        if not item.id:
            issues.append(ValidationIssue(where, "bad_value", f"{label[:-1]} id must not be empty"))
            continue
        if item.id in ids:
            issues.append(
                ValidationIssue(where, "duplicate_id", f"duplicate {label[:-1]} id '{item.id}'")
            )
        else:
            ids.add(item.id)
    return ids


def _validate_stages(stages: tuple[Stage, ...], stage_ids: set[str], issues: list[ValidationIssue]) -> None:
    _add = issues.append
    for stage in stages:
        if not stage.id:
            continue  # already reported
        where = f"stages.{stage.id}"

        if stage.type != "signal":
            _add(
                ValidationIssue(
                    where,
                    "unknown_capability",
                    f"stage type '{stage.type}' is not available in Phase 1 (only 'signal')",
                )
            )

        if not registry.is_known_clock(stage.clock):
            _add(
                ValidationIssue(
                    where,
                    "unknown_capability",
                    f"clock '{stage.clock}' is not available in Phase 1 "
                    f"(supported: {sorted(registry.CLOCKS)})",
                )
            )
        elif stage.clock == "candle_close":
            if stage.timeframe is None:
                _add(
                    ValidationIssue(
                        f"{where}.timeframe",
                        "timeframe_missing",
                        "clock 'candle_close' requires a timeframe",
                    )
                )
            elif not registry.is_supported_timeframe(stage.timeframe):
                _add(
                    ValidationIssue(
                        f"{where}.timeframe",
                        "timeframe_unsupported",
                        f"timeframe '{stage.timeframe}' is not supported "
                        f"(supported: {sorted(registry.TIMEFRAMES)})",
                    )
                )

        if stage.input is not None and stage.input not in stage_ids:
            _add(
                ValidationIssue(
                    f"{where}.input",
                    "missing_reference",
                    f"stage input references unknown stage '{stage.input}'",
                )
            )

        if len(stage.conditions) > MAX_CONDITIONS_PER_STAGE:
            _add(
                ValidationIssue(
                    f"{where}.conditions",
                    "bad_value",
                    f"too many conditions ({len(stage.conditions)} > {MAX_CONDITIONS_PER_STAGE})",
                )
            )
        for index, condition in enumerate(stage.conditions):
            _validate_condition(condition, f"{where}.conditions[{index}]", issues)

    _detect_cycles(stages, issues)


def _validate_condition(condition: Condition, where: str, issues: list[ValidationIssue]) -> None:
    _add = issues.append
    if not registry.is_known_operator(condition.op):
        _add(
            ValidationIssue(
                where,
                "unknown_operator",
                f"unknown operator '{condition.op}'",
            )
        )
    _validate_operand(condition.left, f"{where}.left", issues)
    _validate_operand(condition.right, f"{where}.right", issues)


def _validate_operand(operand: Operand, where: str, issues: list[ValidationIssue]) -> None:
    _add = issues.append
    if operand.kind == "field":
        if not operand.name:
            _add(ValidationIssue(where, "bad_value", "field operand has no name"))
        elif registry.is_namespaced_field(operand.name):
            domain = operand.name.split(".", 1)[0]
            _add(
                ValidationIssue(
                    where,
                    "unknown_capability",
                    f"field '{operand.name}' belongs to capability domain '{domain}' "
                    "which is not available in Phase 1",
                )
            )
        elif not registry.is_known_field(operand.name):
            _add(
                ValidationIssue(
                    where,
                    "unknown_field",
                    f"unknown field '{operand.name}' (supported: {sorted(registry.FIELDS)})",
                )
            )
    elif operand.kind == "indicator":
        label = operand.name or "expression"
        _add(
            ValidationIssue(
                where,
                "unknown_capability",
                f"indicator '{label}' is not available in Phase 1",
            )
        )
    else:  # value
        if operand.value is None:
            _add(ValidationIssue(where, "bad_value", "value operand has no value"))
        elif not math.isfinite(operand.value):
            _add(ValidationIssue(where, "bad_value", f"value operand must be finite, got {operand.value}"))

    _check_params_finite(operand.params, where, issues)


def _check_params_finite(params: dict, where: str, issues: list[ValidationIssue]) -> None:
    for key, value in params.items():
        if isinstance(value, float) and not math.isfinite(value):
            issues.append(
                ValidationIssue(f"{where}.params.{key}", "bad_value", f"parameter must be finite, got {value}")
            )


def _detect_cycles(stages: tuple[Stage, ...], issues: list[ValidationIssue]) -> None:
    if len(stages) > 1000:  # size sanity already reported; keep cycle check bounded
        return
    edges: dict[str, Optional[str]] = {
        stage.id: stage.input for stage in stages if stage.id and stage.input
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {stage.id: WHITE for stage in stages if stage.id}
    reported: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        color[node] = GRAY
        trail.append(node)
        nxt = edges.get(node)
        if nxt is not None and nxt in color:
            if color[nxt] == GRAY:
                cycle = trail[trail.index(nxt):] + [nxt]
                for member in cycle[:-1]:
                    if member not in reported:
                        reported.add(member)
                        issues.append(
                            ValidationIssue(
                                f"stages.{member}",
                                "cycle",
                                "stage input cycle detected: " + " -> ".join(cycle),
                            )
                        )
            elif color[nxt] == WHITE:
                visit(nxt, trail)
        trail.pop()
        color[node] = BLACK

    for sid in list(color):
        if color[sid] == WHITE:
            visit(sid, [])


def _validate_alerts(
    alerts: tuple,
    alert_ids: set[str],
    stage_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    _add = issues.append
    for alert in alerts:
        if not alert.id:
            continue  # already reported
        where = f"alerts.{alert.id}"

        if not alert.source:
            _add(ValidationIssue(f"{where}.source", "bad_value", "alert source must not be empty"))
        elif alert.source not in stage_ids:
            _add(
                ValidationIssue(
                    f"{where}.source",
                    "missing_reference",
                    f"alert references unknown stage '{alert.source}'",
                )
            )

        if not registry.is_known_trigger(alert.trigger):
            _add(
                ValidationIssue(
                    f"{where}.trigger",
                    "bad_value",
                    f"unknown trigger '{alert.trigger}' (supported: {sorted(registry.TRIGGERS)})",
                )
            )

        if alert.cooldown_s is not None and alert.cooldown_s < 0:
            _add(ValidationIssue(f"{where}.cooldown_s", "bad_value", "cooldown_s must be >= 0"))
        if alert.reminder_interval_s is not None and alert.reminder_interval_s <= 0:
            _add(
                ValidationIssue(
                    f"{where}.reminder_interval_s", "bad_value", "reminder_interval_s must be > 0"
                )
            )

        if (alert.rearm_level is None) != (alert.rearm_direction is None):
            _add(
                ValidationIssue(
                    f"{where}.rearm",
                    "bad_value",
                    "rearm_level and rearm_direction must be set together",
                )
            )
        if alert.rearm_direction is not None and alert.rearm_direction not in ("above", "below"):
            _add(
                ValidationIssue(
                    f"{where}.rearm_direction",
                    "bad_value",
                    f"rearm_direction must be 'above' or 'below', got '{alert.rearm_direction}'",
                )
            )

        if alert.expires_at is not None:
            try:
                datetime.fromisoformat(alert.expires_at.replace("Z", "+00:00"))
            except ValueError:
                _add(
                    ValidationIssue(
                        f"{where}.expires_at",
                        "bad_value",
                        f"expires_at must be an ISO-8601 timestamp, got '{alert.expires_at}'",
                    )
                )

        # alert_ids unused here beyond duplicate detection in _collect_ids
        _ = alert_ids
