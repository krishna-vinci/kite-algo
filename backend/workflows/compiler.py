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
from .models import (
    BreadthSpec,
    Condition,
    ConditionGroup,
    Operand,
    SequenceSpec,
    Stage,
    WorkflowDocument,
)

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
MAX_FEATURE_STAGES = registry.MAX_FEATURE_STAGES
MAX_CONDITIONS_PER_GROUP = registry.MAX_CONDITIONS_PER_GROUP
MAX_ARITHMETIC_DEPTH = registry.MAX_ARITHMETIC_DEPTH
MAX_INPUT_CHAIN_DEPTH = registry.MAX_INPUT_CHAIN_DEPTH


# Allowed data_policy value sets (anything else is a bad_value issue).
DATA_POLICY_MISSING = frozenset({"exclude_and_report"})
DATA_POLICY_INSUFFICIENT_HISTORY = frozenset({"wait"})


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

    if not registry.is_supported_session(doc.session):
        _add(
            ValidationIssue(
                "document.session",
                "unknown_capability",
                f"session '{doc.session}' is not supported in Phase 1 "
                f"(supported: {sorted(registry.SESSIONS)})",
            )
        )
    else:
        for index, instrument in enumerate(doc.instruments):
            if not registry.session_accepts_exchange(doc.session, instrument.exchange):
                _add(
                    ValidationIssue(
                        f"document.instruments[{index}].exchange",
                        "session_mismatch",
                        f"session '{doc.session}' does not support exchange "
                        f"'{instrument.exchange}'",
                    )
                )

    _validate_reserved(doc, issues)

    if doc.data_policy.missing not in DATA_POLICY_MISSING:
        _add(
            ValidationIssue(
                "document.data_policy.missing",
                "bad_value",
                f"data_policy.missing must be {sorted(DATA_POLICY_MISSING)[0]!r} in Phase 1, "
                f"got {doc.data_policy.missing!r}",
            )
        )
    if doc.data_policy.insufficient_history not in DATA_POLICY_INSUFFICIENT_HISTORY:
        _add(
            ValidationIssue(
                "document.data_policy.insufficient_history",
                "bad_value",
                f"data_policy.insufficient_history must be "
                f"{sorted(DATA_POLICY_INSUFFICIENT_HISTORY)[0]!r} in Phase 1, "
                f"got {doc.data_policy.insufficient_history!r}",
            )
        )

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
    feature_stage_count = sum(1 for stage in doc.stages if stage.type == "feature")
    if feature_stage_count > MAX_FEATURE_STAGES:
        _add(
            ValidationIssue(
                "document.stages",
                "bad_value",
                f"too many feature stages ({feature_stage_count} > {MAX_FEATURE_STAGES})",
            )
        )
    if len(doc.alerts) > MAX_ALERTS:
        _add(
            ValidationIssue(
                "document.alerts", "bad_value", f"too many alerts ({len(doc.alerts)} > {MAX_ALERTS})"
            )
        )

    _validate_universe(doc.universe, issues)

    stage_ids = _collect_ids(doc.stages, "stages", issues)
    _validate_stages(doc.stages, stage_ids, issues, doc_ref=doc)
    alert_ids = _collect_ids(doc.alerts, "alerts", issues)
    _validate_alerts(doc.alerts, alert_ids, stage_ids, issues)
    _validate_screener(doc, stage_ids, issues)
    _validate_screener_only_fields(doc, issues)
    _validate_breadth_referenced(doc, issues)


def _validate_breadth_referenced(
    doc: WorkflowDocument, issues: list[ValidationIssue]
) -> None:
    """A breadth stage must be referenced by an alert to have any effect.

    The aggregate is evaluated by the member subscriptions an ALERT
    materializes; without one the stage is never dispatched, so the workflow
    would be silently dead. Reject it with an actionable message instead of
    accepting configuration that can never notify.
    """
    referenced = {alert.source for alert in doc.alerts}
    for stage in doc.stages:
        if stage.breadth is None or stage.id in referenced:
            continue
        issues.append(
            ValidationIssue(
                f"stages.{stage.id}",
                "missing_reference",
                f"breadth stage '{stage.id}' is not referenced by any alert, so "
                "nothing would ever be dispatched or notified; add an alert with "
                f"'source: {stage.id}'",
            )
        )


def _validate_screener_only_fields(doc: WorkflowDocument, issues: list[ValidationIssue]) -> None:
    """``change_pct``/``turnover`` exist only on the screener stored-data
    path; in an alert document they would silently never resolve."""
    if doc.screener is not None:
        return
    screener_only = {
        name
        for name, spec in registry.FIELDS.items()
        if isinstance(spec, dict) and spec.get("screener_only")
    }
    if not screener_only:
        return
    for stage in doc.stages:
        for group in (stage.conditions, stage.any_conditions, stage.not_conditions):
            for cond in group:
                for operand in (cond.left, cond.right):
                    if (
                        getattr(operand, "kind", None) == "field"
                        and getattr(operand, "name", None) in screener_only
                    ):
                        issues.append(
                            ValidationIssue(
                                f"stages.{stage.id}.conditions",
                                "unknown_capability",
                                f"field '{operand.name}' is only available in "
                                "screener documents (stored-data scans)",
                            )
                        )



def _validate_screener(doc: WorkflowDocument, stage_ids: set[str], issues: list[ValidationIssue]) -> None:
    """Validate the screener block (Phase 3 F9).

    A screener document runs SCHEDULED STORED-DATA scans over a universe:
    live-tick stages and per-rule alerts do not apply (attachments are the
    notification surface). MCX/currency have no session calendar, so only
    nse_equity schedules validate (the parser already rejects others).
    """
    _add = issues.append
    screener = doc.screener
    if screener is None:
        return
    if doc.universe is None and not doc.instruments:
        _add(
            ValidationIssue(
                "document.screener",
                "missing_reference",
                "a screener document must declare a universe expression or "
                "an explicit instruments list to scan",
            )
        )
    if not doc.stages:
        _add(
            ValidationIssue(
                "document.stages",
                "bad_value",
                "a screener document requires at least one filter/signal stage",
            )
        )
    for stage in doc.stages:
        if stage.clock == "ltp":
            _add(
                ValidationIssue(
                    f"stages.{stage.id}.clock",
                    "unknown_capability",
                    "screener scans run over stored completed candles; a "
                    "live-tick stage is not valid in a screener document",
                )
            )
    referenced_inputs = {stage.input for stage in doc.stages if stage.input}
    terminals = [s.id for s in doc.stages if s.id not in referenced_inputs]
    if len(terminals) > 1:
        _add(
            ValidationIssue(
                "document.stages",
                "bad_value",
                "a screener document must have exactly one terminal stage "
                f"(the ranked pipeline output); found {sorted(terminals)}",
            )
        )
    if doc.alerts:
        _add(
            ValidationIssue(
                "document.alerts",
                "bad_value",
                "a screener document notifies through screener attachments, "
                "not per-rule alerts",
            )
        )
    rank = screener.rank
    if rank is not None:
        where = "document.screener.rank.by"
        operand = rank.by
        if operand.kind == "field":
            _validate_operand(operand, where, issues)
        elif operand.kind == "indicator":
            _validate_indicator_operand(operand, where, issues)
        else:
            _add(ValidationIssue(where, "bad_value", "rank.by must be a field or indicator expression"))
    for index, att in enumerate(screener.attachments):
        if not att.channels:
            _add(
                ValidationIssue(
                    f"document.screener.attachments[{index}].channels",
                    "bad_value",
                    "attachment requires at least one channel",
                )
            )


def _validate_reserved(doc: WorkflowDocument, issues: list[ValidationIssue]) -> None:
    """Reserved top-level keys never vanish silently: unsupported values fail
    validation with named issues (silent-capability rule)."""
    reserved = getattr(doc, "_reserved", None) or {}
    timezone_value = reserved.get("timezone")
    if "timezone" in reserved and timezone_value is not None and timezone_value != "Asia/Kolkata":
        issues.append(
            ValidationIssue(
                "document.timezone",
                "bad_value",
                f"timezone must be 'Asia/Kolkata' in Phase 1, got {timezone_value!r}",
            )
        )


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


def _validate_stages(stages: tuple[Stage, ...], stage_ids: set[str], issues: list[ValidationIssue], doc_ref=None) -> None:
    _add = issues.append
    feature_ids = {
        stage.id
        for stage in stages
        if stage.type == "feature" and stage.function
    }
    for stage in stages:
        if not stage.id:
            continue  # already reported
        where = f"stages.{stage.id}"

        if not registry.is_known_stage_type(stage.type):
            _add(
                ValidationIssue(
                    where,
                    "unknown_capability",
                    f"stage type '{stage.type}' is not available "
                    f"(supported: {sorted(registry.STAGE_TYPES)})",
                )
            )

        if stage.type == "feature":
            _validate_feature_stage(stage, where, issues)
        else:
            if stage.function is not None:
                _add(
                    ValidationIssue(
                        f"{where}.function",
                        "bad_value",
                        "'function' is only valid on feature stages",
                    )
                )

        _validate_advanced_stage(stage, where, issues)

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
                if _stage_is_fundamentals_only(stage):
                    # Fundamentals ride the declared candle clock: the latest
                    # stored snapshot is read at each candle evaluation. With
                    # no timeframe the stage would never be dispatched, so
                    # reject instead of accepting a silently dead stage.
                    _add(
                        ValidationIssue(
                            f"{where}.timeframe",
                            "timeframe_missing",
                            "fundamentals conditions evaluate on the declared "
                            "candle clock (no dedicated fundamentals stream "
                            "exists): specify a timeframe (e.g. timeframe: 1d) "
                            "so the stage is dispatched",
                        )
                    )
                else:
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

        if stage.input is not None:
            if stage.input not in stage_ids:
                if stage.input == "universe":
                    # The implicit membership source: valid when the document
                    # declares a universe expression.
                    if getattr(doc_ref, "universe", None) is None:
                        _add(
                            ValidationIssue(
                                f"{where}.input",
                                "missing_reference",
                                "stage input references 'universe' but the document "
                                "declares no universe expression",
                            )
                        )
                else:
                    _add(
                        ValidationIssue(
                            f"{where}.input",
                            "missing_reference",
                            f"stage input references unknown stage '{stage.input}'",
                        )
                    )
            elif stage.type == "feature":
                _add(
                    ValidationIssue(
                        f"{where}.input",
                        "bad_value",
                        "feature stages do not take an upstream input; they compute "
                        "over completed candles of their timeframe",
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
        if len(stage.any_conditions) > MAX_CONDITIONS_PER_GROUP:
            _add(
                ValidationIssue(
                    f"{where}.any",
                    "bad_value",
                    f"too many conditions in 'any' group ({len(stage.any_conditions)} > {MAX_CONDITIONS_PER_GROUP})",
                )
            )
        if len(stage.not_conditions) > MAX_CONDITIONS_PER_GROUP:
            _add(
                ValidationIssue(
                    f"{where}.not",
                    "bad_value",
                    f"too many conditions in 'not' group ({len(stage.not_conditions)} > {MAX_CONDITIONS_PER_GROUP})",
                )
            )
        for index, condition in enumerate(stage.conditions):
            _validate_condition(condition, f"{where}.conditions[{index}]", issues, feature_ids)
        for index, condition in enumerate(stage.any_conditions):
            _validate_condition(condition, f"{where}.any[{index}]", issues, feature_ids)
        for index, condition in enumerate(stage.not_conditions):
            _validate_condition(condition, f"{where}.not[{index}]", issues, feature_ids)

    _detect_cycles(stages, issues)
    _validate_chain_depth(stages, issues)


def _stage_is_fundamentals_only(stage: Stage) -> bool:
    """True when every condition references only fundamentals fields.

    Used to tailor the ``timeframe_missing`` issue: fundamentals evaluate on
    the declared candle clock (latest stored snapshot + acquisition
    metadata); there is no dedicated fundamentals evaluation stream.
    """
    conditions = list(stage.conditions) + list(stage.any_conditions) + list(stage.not_conditions)
    if not conditions:
        return False

    def _fields_only_fundamentals(operand) -> bool:
        if operand is None:
            return True
        if operand.kind == "field":
            return isinstance(operand.name, str) and operand.name.startswith("fundamentals.")
        if operand.kind == "value":
            return True
        return False

    for cond in conditions:
        if not _fields_only_fundamentals(cond.left) or not _fields_only_fundamentals(cond.right):
            return False
    return True


def _validate_feature_stage(stage: Stage, where: str, issues: list[ValidationIssue]) -> None:
    _add = issues.append
    if not registry.is_known_feature_function(stage.function):
        _add(
            ValidationIssue(
                f"{where}.function",
                "unknown_capability",
                f"unknown feature function '{stage.function}' "
                f"(supported: {sorted(registry.FEATURE_FUNCTIONS)})",
            )
        )
        return
    spec = registry.FEATURE_FUNCTIONS[stage.function]
    params = dict(spec.get("defaults", {}))
    for name, value in (stage.stage_params or {}).items():
        if name not in spec["params"]:
            _add(
                ValidationIssue(
                    f"{where}.params.{name}",
                    "bad_value",
                    f"unknown param '{name}' for feature '{stage.function}' "
                    f"(supported: {sorted(spec['params'])})",
                )
            )
            continue
        params[name] = value
    for name, value in params.items():
        low, high = spec["params"][name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
            _add(
                ValidationIssue(
                    f"{where}.params.{name}",
                    "bad_value",
                    f"param '{name}' for feature '{stage.function}' must be a number in "
                    f"[{low}, {high}], got {value!r}",
                )
            )
    if stage.source_field is not None and not registry.is_known_field(stage.source_field):
        _add(
            ValidationIssue(
                f"{where}.source",
                "unknown_field",
                f"unknown feature source field '{stage.source_field}'",
            )
        )
    else:
        expected_inputs = spec["inputs"]
        if stage.source_field is not None and len(expected_inputs) > 1:
            _add(
                ValidationIssue(
                    f"{where}.source",
                    "bad_value",
                    f"feature '{stage.function}' needs fields {list(expected_inputs)}; "
                    "a single source override is not valid",
                )
            )


def _validate_chain_depth(stages: tuple[Stage, ...], issues: list[ValidationIssue]) -> None:
    by_id = {stage.id: stage for stage in stages if stage.id}
    for stage in by_id.values():
        depth = 0
        cursor = stage
        seen = set()
        while cursor.input is not None:
            if cursor.id in seen or depth > MAX_INPUT_CHAIN_DEPTH:
                issues.append(
                    ValidationIssue(
                        f"stages.{stage.id}.input",
                        "bad_value",
                        f"input chain exceeds the maximum depth of {MAX_INPUT_CHAIN_DEPTH}",
                    )
                )
                break
            seen.add(cursor.id)
            depth += 1
            cursor = by_id.get(cursor.input)
            if cursor is None:
                break  # missing_reference already reported


def _validate_advanced_stage(stage: Stage, where: str, issues: list[ValidationIssue]) -> None:
    """Validate Phase 4 F10 stage-level advanced conditions.

    Covers ``consecutive_bars``, ``sequence``, ``breadth`` and their clock and
    exclusivity rules. Bounds come from ``registry`` so validation and the
    advertised capabilities cannot drift apart.
    """
    has_sequence = stage.sequence is not None
    has_breadth = stage.breadth is not None
    has_conditions = bool(stage.conditions or stage.any_conditions or stage.not_conditions)

    if stage.consecutive_bars is not None:
        if stage.type != "signal":
            issues.append(
                ValidationIssue(
                    f"{where}.consecutive_bars",
                    "bad_value",
                    "'consecutive_bars' is only valid on signal stages",
                )
            )
        if stage.clock != "candle_close":
            # Ticks carry no bar identity: an ltp stage would never fire.
            issues.append(
                ValidationIssue(
                    f"{where}.consecutive_bars",
                    "bad_value",
                    "'consecutive_bars' requires clock 'candle_close' "
                    "(consecutive bars are counted over completed candles)",
                )
            )
        if not has_conditions or has_sequence or has_breadth:
            issues.append(
                ValidationIssue(
                    f"{where}.consecutive_bars",
                    "bad_value",
                    "'consecutive_bars' applies to the stage's own conditions and "
                    "cannot be combined with 'sequence' or 'breadth'",
                )
            )
        value = stage.consecutive_bars
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not registry.MIN_CONSECUTIVE_BARS <= value <= registry.MAX_CONSECUTIVE_BARS
        ):
            issues.append(
                ValidationIssue(
                    f"{where}.consecutive_bars",
                    "bad_value",
                    f"must be an integer between {registry.MIN_CONSECUTIVE_BARS} and "
                    f"{registry.MAX_CONSECUTIVE_BARS}, got {value!r}",
                )
            )

    if has_sequence:
        if stage.type != "signal":
            issues.append(
                ValidationIssue(
                    f"{where}.sequence", "bad_value", "'sequence' is only valid on signal stages"
                )
            )
        if stage.clock != "candle_close":
            issues.append(
                ValidationIssue(
                    f"{where}.sequence",
                    "bad_value",
                    "'sequence' requires clock 'candle_close' "
                    "(A-then-B progress is counted over completed bars)",
                )
            )
        if has_conditions or has_breadth or stage.consecutive_bars is not None:
            issues.append(
                ValidationIssue(
                    f"{where}.sequence",
                    "bad_value",
                    "'sequence' replaces the stage's own conditions and cannot be "
                    "combined with 'conditions', 'consecutive_bars' or 'breadth'",
                )
            )
        _validate_sequence(stage.sequence, where, issues)

    if has_breadth:
        if stage.type != "breadth":
            issues.append(
                ValidationIssue(
                    f"{where}.breadth",
                    "bad_value",
                    "a 'breadth' block requires stage type 'breadth'",
                )
            )
        if stage.clock != "candle_close":
            issues.append(
                ValidationIssue(
                    f"{where}.breadth", "bad_value", "'breadth' requires clock 'candle_close'"
                )
            )
        if has_conditions or has_sequence or stage.consecutive_bars is not None:
            issues.append(
                ValidationIssue(
                    f"{where}.breadth",
                    "bad_value",
                    "'breadth' cannot be combined with 'conditions', 'sequence' or "
                    "'consecutive_bars'",
                )
            )
        _validate_breadth(stage.breadth, where, issues)
    elif stage.type == "breadth":
        issues.append(
            ValidationIssue(
                where, "bad_value", "stage type 'breadth' requires a 'breadth' block"
            )
        )


def _validate_sequence(sequence: SequenceSpec, where: str, issues: list[ValidationIssue]) -> None:
    path = f"{where}.sequence"
    if not sequence.first:
        issues.append(ValidationIssue(f"{path}.first", "bad_value", "'first' must not be empty"))
    if not sequence.then:
        issues.append(ValidationIssue(f"{path}.then", "bad_value", "'then' must not be empty"))
    if sequence.within_bars is None and sequence.within_s is None:
        issues.append(
            ValidationIssue(
                path,
                "bad_value",
                "a sequence requires at least one bound: 'within_bars' or 'within'",
            )
        )
    if sequence.within_bars is not None and not (
        registry.MIN_SEQUENCE_WITHIN_BARS
        <= sequence.within_bars
        <= registry.MAX_SEQUENCE_WITHIN_BARS
    ):
        issues.append(
            ValidationIssue(
                f"{path}.within_bars",
                "bad_value",
                f"must be between {registry.MIN_SEQUENCE_WITHIN_BARS} and "
                f"{registry.MAX_SEQUENCE_WITHIN_BARS}, got {sequence.within_bars!r}",
            )
        )
    if sequence.within_s is not None and not (
        60 <= sequence.within_s <= registry.MAX_SEQUENCE_WITHIN_S
    ):
        issues.append(
            ValidationIssue(
                f"{path}.within",
                "bad_value",
                f"must be between 60s and {registry.MAX_SEQUENCE_WITHIN_S}s, "
                f"got {sequence.within_s}s",
            )
        )
    for group_name, groups in (("first", sequence.first), ("then", sequence.then)):
        for index, group in enumerate(groups):
            _validate_condition_group(
                group, f"{path}.{group_name}[{index}].{group.kind}", issues
            )


def _validate_breadth(breadth: BreadthSpec, where: str, issues: list[ValidationIssue]) -> None:
    path = f"{where}.breadth"
    if not registry.is_known_breadth_mode(breadth.mode):
        issues.append(
            ValidationIssue(
                f"{path}.mode",
                "unknown_capability",
                f"breadth mode '{breadth.mode}' is not available "
                f"(supported: {sorted(registry.BREADTH_MODES)})",
            )
        )
    elif not registry.is_implemented_breadth_mode(breadth.mode):
        # Reserved name, deliberately unimplemented: "K symbols on the same
        # bar" needs its own alignment and partial-bar semantics.
        issues.append(
            ValidationIssue(
                f"{path}.mode",
                "unknown_capability",
                f"breadth mode '{breadth.mode}' is not implemented (windowed "
                "distinct-symbol participation is the only supported form; "
                "simultaneous breadth is deferred until separately specified)",
            )
        )
    if not (
        registry.MIN_BREADTH_INSTRUMENTS
        <= breadth.distinct_instruments
        <= registry.MAX_BREADTH_INSTRUMENTS
    ):
        issues.append(
            ValidationIssue(
                f"{path}.distinct_instruments",
                "bad_value",
                f"must be between {registry.MIN_BREADTH_INSTRUMENTS} and "
                f"{registry.MAX_BREADTH_INSTRUMENTS}, got {breadth.distinct_instruments!r}",
            )
        )
    if not registry.MIN_BREADTH_WINDOW_S <= breadth.window_s <= registry.MAX_BREADTH_WINDOW_S:
        issues.append(
            ValidationIssue(
                f"{path}.window",
                "bad_value",
                f"must be between {registry.MIN_BREADTH_WINDOW_S}s and "
                f"{registry.MAX_BREADTH_WINDOW_S}s, got {breadth.window_s}s",
            )
        )
    if not breadth.condition:
        issues.append(ValidationIssue(f"{path}.condition", "bad_value", "must contain conditions"))
    for index, group in enumerate(breadth.condition):
        _validate_condition_group(group, f"{path}.condition[{index}].{group.kind}", issues)


def _validate_condition_group(
    group: ConditionGroup, where: str, issues: list[ValidationIssue]
) -> None:
    if group.kind not in ("all", "any", "not"):
        issues.append(ValidationIssue(where, "bad_value", f"unknown group '{group.kind}'"))
        return
    if group.kind == "not" and len(group.conditions) != 1:
        issues.append(
            ValidationIssue(where, "bad_value", "a 'not' group takes exactly one condition")
        )
    if len(group.conditions) > registry.MAX_CONDITIONS_PER_GROUP:
        issues.append(
            ValidationIssue(
                where,
                "bad_value",
                f"at most {registry.MAX_CONDITIONS_PER_GROUP} conditions per group",
            )
        )
    if not group.conditions:
        issues.append(ValidationIssue(where, "bad_value", "group must contain conditions"))
    for index, cond in enumerate(group.conditions):
        _validate_condition(cond, f"{where}[{index}]", issues)


def _validate_condition(
    condition: Condition,
    where: str,
    issues: list[ValidationIssue],
    feature_stage_ids: Optional[frozenset] = None,
) -> None:
    _add = issues.append
    if not registry.is_known_operator(condition.op):
        _add(
            ValidationIssue(
                where,
                "unknown_operator",
                f"unknown operator '{condition.op}'",
            )
        )
    if condition.op == "within":
        _validate_within_bounds(condition, where, issues)
    _validate_operand(condition.left, f"{where}.left", issues, feature_stage_ids)
    _validate_operand(condition.right, f"{where}.right", issues, feature_stage_ids)
    if condition.op not in ("breaks_prev_high", "breaks_prev_low"):
        for side, operand in (("left", condition.left), ("right", condition.right)):
            if operand.kind == "field" and registry.is_context_field(operand.name or ""):
                _add(
                    ValidationIssue(
                        f"{where}.{side}",
                        "bad_value",
                        f"field '{operand.name}' is context-resolved and only valid "
                        "as the right operand of breaks_prev_high/breaks_prev_low",
                    )
                )
    if condition.hysteresis is not None:
        _validate_hysteresis(condition, where, issues)


def _validate_hysteresis(cond: Condition, where: str, issues: list[ValidationIssue]) -> None:
    """Validate explicit condition hysteresis (Phase 4 F10, D14).

    Only a level operator on a CONSTANT threshold is supported: the release
    bound must be comparable to the level at compile time, and an
    indicator/expression/pair/context threshold has no such constant.
    """
    hysteresis = cond.hysteresis
    path = f"{where}.hysteresis"
    if cond.op not in registry.HYSTERESIS_OPS:
        issues.append(
            ValidationIssue(
                path,
                "bad_value",
                f"hysteresis is valid on {sorted(registry.HYSTERESIS_OPS)}, got '{cond.op}'",
            )
        )
        return
    if cond.right.kind != "value" or cond.right.value is None:
        issues.append(
            ValidationIssue(
                path,
                "bad_value",
                "hysteresis requires a constant threshold (right: {value: <number>}); "
                "dynamic release operands are not implemented",
            )
        )
        return
    level = cond.right.value
    release = hysteresis.release
    if cond.op in ("gt", "gte") and not release < level:
        issues.append(
            ValidationIssue(
                f"{path}.release",
                "bad_value",
                f"release ({release}) must be below the threshold ({level}) for '{cond.op}'",
            )
        )
    if cond.op in ("lt", "lte") and not release > level:
        issues.append(
            ValidationIssue(
                f"{path}.release",
                "bad_value",
                f"release ({release}) must be above the threshold ({level}) for '{cond.op}'",
            )
        )


def _validate_within_bounds(condition: Condition, where: str, issues: list[ValidationIssue]) -> None:
    """`within` needs a finite numeric upper bound in right params 'hi'; an
    unbounded range would evaluate to permanent unknown and never fire."""
    _add = issues.append
    hi = condition.right.params.get("hi")
    if (
        isinstance(hi, bool)
        or not isinstance(hi, (int, float))
        or not math.isfinite(float(hi))
    ):
        _add(
            ValidationIssue(
                f"{where}.right.params.hi",
                "bad_value",
                "operator 'within' requires a finite numeric upper bound "
                f"in the right operand's 'hi' param, got {hi!r}",
            )
        )
    lo = condition.right.value if condition.right.value is not None else condition.right.params.get("lo")
    if (
        isinstance(lo, bool)
        or not isinstance(lo, (int, float))
        or not math.isfinite(float(lo))
    ):
        _add(
            ValidationIssue(
                f"{where}.right.params.lo"
                if condition.right.value is None
                else f"{where}.right.value",
                "bad_value",
                "operator 'within' requires a finite numeric lower bound "
                f"in the right operand's value, got {lo!r}",
            )
        )


def _validate_operand(
    operand: Operand,
    where: str,
    issues: list[ValidationIssue],
    feature_stage_ids: Optional[frozenset] = None,
) -> None:
    _add = issues.append
    if operand.kind == "field":
        if not operand.name:
            _add(ValidationIssue(where, "bad_value", "field operand has no name"))
        elif registry.is_namespaced_field(operand.name):
            domain = operand.name.split(".", 1)[0]
            if domain == "fundamentals" and operand.name in registry.FUNDAMENTALS_FIELDS:
                return  # Phase 2: latest-snapshot fundamentals observation
            if registry.is_external_field(operand.name):
                # Phase 4 F10: registered external producer value. The producer
                # row is created at runtime, so only the shape is checked here;
                # an unresolvable reference is unknown at evaluation.
                return
            if domain == "external":
                _add(
                    ValidationIssue(
                        where,
                        "bad_value",
                        f"'{operand.name}' is not a valid external reference; use "
                        "external.<producer>.<field> with a registered producer name",
                    )
                )
                return
            _add(
                ValidationIssue(
                    where,
                    "unknown_capability",
                    f"field '{operand.name}' belongs to capability domain '{domain}' "
                    "which is not available",
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
    elif operand.kind == "value":
        if operand.value is None:
            _add(ValidationIssue(where, "bad_value", "value operand has no value"))
        elif not math.isfinite(operand.value):
            _add(ValidationIssue(where, "bad_value", f"value operand must be finite, got {operand.value}"))
    elif operand.kind == "indicator":
        _validate_indicator_operand(operand, where, issues, feature_stage_ids)
    elif operand.kind == "pair":
        _validate_pair_operand(operand, where, issues)


def _validate_pair_operand(
    operand: Operand, where: str, issues: list[ValidationIssue]
) -> None:
    """Validate a cross-instrument pair operand (Phase 4 F10).

    Both legs use the STAGE's timeframe and the same completed-bar store, so
    basis compatibility is structural (``historical_candles`` carries no
    adjustment column). What is validated here is identity, required
    parameters and bounds; alignment/freshness are runtime concerns and
    resolve to ``unknown`` with a named reason.
    """
    _add = issues.append
    kind = operand.name or ""
    spec = registry.PAIR_COMPUTATIONS.get(kind)
    if spec is None:
        _add(
            ValidationIssue(
                where,
                "unknown_capability",
                f"unknown pair computation '{kind}' "
                f"(supported: {sorted(registry.PAIR_COMPUTATIONS)})",
            )
        )
        return
    params = dict(operand.params or {})
    for key in spec["requires"]:
        if params.get(key) is None:
            _add(
                ValidationIssue(
                    f"{where}.{key}",
                    "bad_value",
                    f"pair computation '{kind}' requires '{key}'",
                )
            )
    allowed = set(spec["requires"]) | set(spec.get("optional", ()))
    for key in params:
        if key not in allowed:
            _add(
                ValidationIssue(
                    f"{where}.{key}",
                    "bad_value",
                    f"unknown key '{key}' for pair computation '{kind}' "
                    f"(supported: {sorted(allowed)})",
                )
            )
    for side in ("instrument", "reference"):
        value = params.get(side)
        if value is None:
            continue
        if not isinstance(value, str) or ":" not in value:
            _add(
                ValidationIssue(
                    f"{where}.{side}",
                    "bad_value",
                    f"pair '{side}' must be an exchange-qualified instrument key "
                    f"like 'NSE:INFY', got {value!r}",
                )
            )
    instrument = params.get("instrument")
    reference = params.get("reference")
    if isinstance(instrument, str) and isinstance(reference, str) and instrument == reference:
        _add(
            ValidationIssue(
                where,
                "bad_value",
                "a pair operand needs two DIFFERENT instruments "
                f"(both were '{instrument}')",
            )
        )
    field = params.get("field")
    if field is not None:
        if field not in spec["fields"]:
            _add(
                ValidationIssue(
                    f"{where}.field",
                    "bad_value",
                    f"pair computation '{kind}' reads {sorted(spec['fields'])}, got {field!r}",
                )
            )
    if kind == "relative_strength":
        lookback = params.get("lookback")
        low, high = registry.PAIR_LOOKBACK_BOUNDS
        if (
            isinstance(lookback, bool)
            or not isinstance(lookback, int)
            or not low <= lookback <= high
        ):
            _add(
                ValidationIssue(
                    f"{where}.lookback",
                    "bad_value",
                    f"lookback must be an integer between {low} and {high}, got {lookback!r}",
                )
            )
    if "max_skew_bars" in params:
        skew = params["max_skew_bars"]
        if (
            isinstance(skew, bool)
            or not isinstance(skew, int)
            or not 0 <= skew <= registry.PAIR_MAX_SKEW_BARS
        ):
            _add(
                ValidationIssue(
                    f"{where}.max_skew_bars",
                    "bad_value",
                    f"max_skew_bars must be between 0 and {registry.PAIR_MAX_SKEW_BARS}, "
                    f"got {skew!r}",
                )
            )


def _validate_indicator_operand(
    operand: Operand,
    where: str,
    issues: list[ValidationIssue],
    feature_stage_ids: Optional[frozenset] = None,
    _depth: int = 0,
) -> None:
    """Validate inline indicator references and bounded arithmetic trees."""
    _add = issues.append
    if operand.kind == "value":
        if operand.value is None:
            _add(ValidationIssue(where, "bad_value", "value operand has no value"))
        elif not math.isfinite(operand.value):
            _add(ValidationIssue(where, "bad_value", f"value operand must be finite, got {operand.value}"))
        return
    if operand.kind == "pair":
        _validate_pair_operand(operand, where, issues)
        return
    if operand.kind == "field":
        _validate_operand(operand, where, issues)
        return
    if operand.name is not None and operand.name.startswith("stage:"):
        # F8 stage reference: resolves to a declared feature stage's snapshot
        # (canonical feature id aliased at dispatch). Validated, not ignored.
        ref_id = operand.name[len("stage:"):]
        if not ref_id:
            _add(ValidationIssue(where, "bad_value", "stage reference must name a feature stage (stage:<id>)"))
        elif feature_stage_ids is None or ref_id not in feature_stage_ids:
            _add(
                ValidationIssue(
                    where,
                    "missing_reference",
                    f"stage reference '{operand.name}' does not resolve to a "
                    "declared feature stage (type: feature with a function)",
                )
            )
        if operand.source is not None:
            _add(ValidationIssue(f"{where}.source", "bad_value", "a stage reference takes no source override"))
        if operand.offset:
            _add(ValidationIssue(f"{where}.offset", "bad_value", "a stage reference takes no offset"))
        return
    if operand.name is not None:
        if not registry.is_known_feature_function(operand.name):
            _add(
                ValidationIssue(
                    where,
                    "unknown_capability",
                    f"unknown indicator function '{operand.name}' "
                    f"(supported: {sorted(registry.FEATURE_FUNCTIONS)})",
                )
            )
        else:
            spec = registry.FEATURE_FUNCTIONS[operand.name]
            defaults = dict(spec.get("defaults", {}))
            for name, value in (operand.params or {}).items():
                if name in registry.ARITHMETIC_OPS:
                    continue  # expression keys handled below
                if name not in spec["params"]:
                    _add(
                        ValidationIssue(
                            f"{where}.params.{name}",
                            "bad_value",
                            f"unknown param '{name}' for indicator '{operand.name}' "
                            f"(supported: {sorted(spec['params'])})",
                        )
                    )
                    continue
                defaults[name] = value
            for name, value in defaults.items():
                low, high = spec["params"][name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
                    _add(
                        ValidationIssue(
                            f"{where}.params.{name}",
                            "bad_value",
                            f"param '{name}' for indicator '{operand.name}' must be a number in "
                            f"[{low}, {high}], got {value!r}",
                        )
                    )
        if operand.source is not None:
            if not registry.is_known_field(operand.source):
                _add(ValidationIssue(f"{where}.source", "unknown_field", f"unknown indicator source field '{operand.source}'"))
            elif len(registry.FEATURE_FUNCTIONS.get(operand.name, {}).get("inputs", ())) > 1:
                _add(
                    ValidationIssue(
                        f"{where}.source",
                        "bad_value",
                        f"indicator '{operand.name}' needs multiple inputs; a single source override is not valid",
                    )
                )
        offset = operand.offset
        if offset is not None and (isinstance(offset, bool) or offset < 0 or offset > 100):
            _add(ValidationIssue(f"{where}.offset", "bad_value", f"offset must be an integer in [0, 100], got {offset!r}"))
        # any leftover param keys that are neither spec params nor expression ops
        if operand.name in registry.FEATURE_FUNCTIONS:
            known = set(registry.FEATURE_FUNCTIONS[operand.name]["params"]) | set(registry.ARITHMETIC_OPS)
            for key in (operand.params or {}):
                if key not in known:
                    _add(
                        ValidationIssue(
                            f"{where}.params.{key}",
                            "bad_value",
                            f"unknown param '{key}' for indicator '{operand.name}'",
                        )
                    )
    else:
        # expression operand: params must contain exactly one arithmetic op
        expression_ops = [k for k in (operand.params or {}) if k in registry.ARITHMETIC_OPS]
        unknown_keys = [k for k in (operand.params or {}) if k not in registry.ARITHMETIC_OPS]
        for key in unknown_keys:
            _add(ValidationIssue(f"{where}.params.{key}", "bad_value", f"unknown expression key '{key}'"))
        if len(expression_ops) != 1:
            _add(
                ValidationIssue(
                    where,
                    "bad_value",
                    "an arithmetic operand requires exactly one of "
                    f"{sorted(registry.ARITHMETIC_OPS)}",
                )
            )
            return
        op_name = expression_ops[0]
        arity = registry.ARITHMETIC_OPS[op_name]["arity"]
        args = operand.params[op_name]
        # MAX_ARITHMETIC_DEPTH is advertised in /capabilities but was never
        # enforced; a deep tree is bounded here as the spec requires.
        if _depth + 1 > registry.MAX_ARITHMETIC_DEPTH:
            _add(
                ValidationIssue(
                    where,
                    "bad_value",
                    f"arithmetic nesting exceeds the maximum depth of "
                    f"{registry.MAX_ARITHMETIC_DEPTH}",
                )
            )
            return
        if not isinstance(args, list) or len(args) != arity:
            _add(
                ValidationIssue(
                    f"{where}.params.{op_name}",
                    "bad_value",
                    f"arithmetic '{op_name}' requires exactly {arity} operand arguments",
                )
            )
            return
        for index, arg in enumerate(args):
            child = _coerce_expression_arg(arg)
            if child is None:
                _add(
                    ValidationIssue(
                        f"{where}.params.{op_name}[{index}]",
                        "bad_value",
                        f"arithmetic arguments must be numbers or operand mappings, got {arg!r}",
                    )
                )
                continue
            _validate_indicator_operand(
                child, f"{where}.params.{op_name}[{index}]", issues, feature_stage_ids,
                _depth + 1,
            )


def _coerce_expression_arg(arg: Any) -> Optional[Operand]:
    """Normalize one raw arithmetic argument into an operand for validation.

    Expression arguments are the raw parser-captured mappings (numbers,
    {field: ...}, {indicator: ...}, or nested {multiply: [...]}, etc.).
    """
    if isinstance(arg, bool):
        return None
    if isinstance(arg, (int, float)):
        return Operand(kind="value", value=float(arg))
    if not isinstance(arg, dict):
        return None
    if "kind" in arg or "field" in arg or "indicator" in arg or "value" in arg:
        from backend.workflows.parser import _operand  # lenient operand parser

        try:
            return _operand(arg, "expr")
        except Exception:
            return None
    if set(arg.keys()) & set(registry.ARITHMETIC_OPS):
        return Operand(kind="indicator", name=None, params=dict(arg))
    return None


def _validate_universe(universe, issues: list[ValidationIssue]) -> None:
    """Validate the document-level membership expression (Phase 2 F7).

    Membership resolution itself happens at activation/refresh against the
    saved universes and index sources; a document referencing an unknown
    universe fails at materialization with an explicit error (and preview
    reports it) rather than being rejected here without database context.
    """
    if universe is None:
        return
    for index, ref in enumerate(universe.refs):
        if not ref.name:
            issues.append(
                ValidationIssue(
                    f"document.universe.union[{index}].name",
                    "bad_value",
                    "universe reference name must not be empty",
                )
            )
    for index, ref in enumerate(universe.exclude):
        if not ref.name:
            issues.append(
                ValidationIssue(
                    f"document.universe.exclude[{index}].name",
                    "bad_value",
                    "universe exclusion name must not be empty",
                )
            )
    for index, ref in enumerate(universe.intersect):
        if not ref.name:
            issues.append(
                ValidationIssue(
                    f"document.universe.intersect[{index}].name",
                    "bad_value",
                    "universe intersection name must not be empty",
                )
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


def _validate_session_cap(alert, where: str, issues: list[ValidationIssue]) -> None:
    """Validate the per-session notification cap (Phase 4 F10).

    The cap is scoped to (owner, workflow, revision, alert) and counts
    LOGICAL notifications across every instrument. ``session_cap_reset``
    accepts only ``session``: any value implying exchange market hours is
    rejected rather than silently applying NSE hours to feed-driven segments,
    which have no session calendar.
    """
    if alert.max_per_session is None:
        if alert.session_cap_reset is not None:
            issues.append(
                ValidationIssue(
                    f"{where}.session_cap_reset",
                    "bad_value",
                    "'session_cap_reset' requires 'max_per_session'",
                )
            )
        return
    value = alert.max_per_session
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not registry.MIN_PER_SESSION <= value <= registry.MAX_PER_SESSION
    ):
        issues.append(
            ValidationIssue(
                f"{where}.max_per_session",
                "bad_value",
                f"must be an integer between {registry.MIN_PER_SESSION} and "
                f"{registry.MAX_PER_SESSION}, got {value!r}",
            )
        )
    reset = alert.session_cap_reset
    if reset is not None and reset not in registry.SESSION_CAP_RESETS:
        issues.append(
            ValidationIssue(
                f"{where}.session_cap_reset",
                "bad_value",
                f"unsupported reset '{reset}' (supported: "
                f"{sorted(registry.SESSION_CAP_RESETS)}). Exchange-hours resets are "
                "not available: MCX/currency sessions are feed-driven and have no "
                "session calendar, so the boundary is the resolved session id "
                "(the IST date for those segments)",
            )
        )


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
        _validate_session_cap(alert, where, issues)
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
