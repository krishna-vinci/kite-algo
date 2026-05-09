from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


_KNOWN_INTERNAL_SOURCE_TEMPLATE_IDS: dict[str, str] = {
    "option-strategy": "internal:option_strategy",
    "options-strategy": "internal:option_strategy",
    "investing-strategy": "internal:investing_strategy",
    "investment-strategy": "internal:investment_strategy",
    "indicator-strategy": "internal:indicator_strategy",
    "algo-worker": "internal:algo_worker",
}


def normalize_strategy_label(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split()).strip()
    return cleaned or None


def normalize_identity_key(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "unknown"


@dataclass(slots=True)
class ResolvedStrategyIdentity:
    template_id: str
    strategy_family: str
    display_name: str
    variant_key: str | None
    deployment_key: str | None
    raw_identity: dict[str, Any]
    resolved_identity: dict[str, Any]
    resolution_method: str
    resolution_confidence: Decimal
    identity_rule_version: str = "journal_v2_identity_v1"
    grouping_rule_version: str = "journal_v2_grouping_v1"
    ambiguous: bool = False


def is_low_confidence_resolution(identity: ResolvedStrategyIdentity) -> bool:
    if identity.ambiguous:
        return True
    return identity.resolution_confidence < Decimal("0.80")


def unresolved_reason_for_identity(identity: ResolvedStrategyIdentity) -> str:
    if identity.resolution_method == "legacy_strategy_name":
        return "missing_template_id_strategy_name_only"
    if identity.resolution_method == "deployment_key_fallback":
        return "deployment_only_identity"
    if identity.resolution_method == "unresolved":
        return "unresolved_identity"
    if identity.resolution_confidence < Decimal("0.80"):
        return "low_confidence_identity"
    return "identity_review_required"


def resolve_strategy_identity(
    payload: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ResolvedStrategyIdentity:
    raw: dict[str, Any] = dict(payload or {})
    raw.update(kwargs)

    strategy_family = str(raw.get("strategy_family") or "unknown_strategy").strip() or "unknown_strategy"
    strategy_name = normalize_strategy_label(raw.get("strategy_name"))
    scenario_name = normalize_strategy_label(raw.get("scenario_name"))
    display_name = strategy_name or scenario_name or strategy_family

    variant_source = raw.get("scenario_key") or raw.get("config_hash")
    variant_key = normalize_identity_key(str(variant_source)) if variant_source else None

    deployment_raw = raw.get("deployment_key")
    deployment_key = normalize_identity_key(str(deployment_raw)) if deployment_raw else None

    explicit_template_id = str(raw.get("template_id") or "").strip()
    worker_template_id = str(raw.get("worker_template_id") or "").strip()
    code_fingerprint = str(raw.get("code_fingerprint") or "").strip()

    source_candidates = [
        raw.get("source_system"),
        raw.get("entry_surface"),
    ]
    mapped_template_id: str | None = None
    for candidate in source_candidates:
        if not candidate:
            continue
        mapped_template_id = _KNOWN_INTERNAL_SOURCE_TEMPLATE_IDS.get(normalize_identity_key(str(candidate)))
        if mapped_template_id:
            break

    template_id: str
    resolution_method: str
    confidence: Decimal
    ambiguous = False

    if explicit_template_id:
        template_id = explicit_template_id
        resolution_method = "explicit_template_id"
        confidence = Decimal("1.0")
    elif mapped_template_id:
        template_id = mapped_template_id
        resolution_method = "known_internal_source"
        confidence = Decimal("0.90")
    elif worker_template_id:
        template_id = worker_template_id
        resolution_method = "worker_template_id"
        confidence = Decimal("0.95")
    elif deployment_key:
        template_id = f"deployment:{deployment_key}"
        resolution_method = "deployment_key_fallback"
        confidence = Decimal("0.70")
        ambiguous = True
    elif code_fingerprint:
        template_id = f"code:{normalize_identity_key(code_fingerprint)}"
        resolution_method = "code_fingerprint"
        confidence = Decimal("0.80")
    else:
        source_signature_parts = [
            raw.get("source_system"),
            raw.get("module"),
            raw.get("class_name"),
            raw.get("function_name"),
        ]
        source_signature = ":".join([str(part).strip() for part in source_signature_parts if str(part or "").strip()])
        if source_signature:
            template_id = f"source:{normalize_identity_key(source_signature)}"
            resolution_method = "source_code_signature"
            confidence = Decimal("0.75")
        elif strategy_name:
            template_id = f"legacy-name:{normalize_identity_key(strategy_name)}"
            resolution_method = "legacy_strategy_name"
            confidence = Decimal("0.50")
            ambiguous = True
        else:
            template_id = "unknown:unresolved"
            resolution_method = "unresolved"
            confidence = Decimal("0.10")
            ambiguous = True

    resolved_identity = {
        "template_id": template_id,
        "variant_key": variant_key,
        "deployment_key": deployment_key,
        "strategy_family": strategy_family,
        "display_name": display_name,
    }

    return ResolvedStrategyIdentity(
        template_id=template_id,
        strategy_family=strategy_family,
        display_name=display_name,
        variant_key=variant_key,
        deployment_key=deployment_key,
        raw_identity=raw,
        resolved_identity=resolved_identity,
        resolution_method=resolution_method,
        resolution_confidence=confidence,
        ambiguous=ambiguous,
    )
