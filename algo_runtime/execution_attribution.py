from __future__ import annotations

from typing import Any, Dict, Literal, Mapping, MutableMapping, Optional


ExecutionModeLiteral = Literal["paper", "live", "dry_run"]


def _clean_required(name: str, value: Any) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{name} is required")
    return cleaned


def _clean_optional_mapping(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return dict(value or {})


def build_execution_attribution(
    *,
    execution_mode: ExecutionModeLiteral,
    strategy_run_id: str,
    strategy_family: str,
    strategy_name: str,
    account_ref: str,
    entry_surface: str,
    source: str,
    idempotency_key: str,
    metadata: Optional[Mapping[str, Any]] = None,
    extras: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    canonical = {
        "strategy_run_id": _clean_required("strategy_run_id", strategy_run_id),
        "strategy_family": _clean_required("strategy_family", strategy_family),
        "strategy_name": _clean_required("strategy_name", strategy_name),
        "execution_mode": _clean_required("execution_mode", execution_mode),
        "account_ref": _clean_required("account_ref", account_ref),
        "entry_surface": _clean_required("entry_surface", entry_surface),
        "source": _clean_required("source", source),
        "idempotency_key": _clean_required("idempotency_key", idempotency_key),
    }
    merged_metadata: MutableMapping[str, Any] = _clean_optional_mapping(metadata)
    payload: Dict[str, Any] = dict(canonical)
    for key, value in _clean_optional_mapping(extras).items():
        if value in (None, ""):
            continue
        payload[key] = value
    merged_metadata.update(payload)
    payload["metadata"] = dict(merged_metadata)
    return payload


def build_paper_execution_attribution(
    *,
    strategy_run_id: str,
    strategy_family: str,
    strategy_name: str,
    account_ref: str,
    entry_surface: str,
    idempotency_key: str,
    metadata: Optional[Mapping[str, Any]] = None,
    extras: Optional[Mapping[str, Any]] = None,
    source: str = "kite_algo",
) -> Dict[str, Any]:
    return build_execution_attribution(
        execution_mode="paper",
        strategy_run_id=strategy_run_id,
        strategy_family=strategy_family,
        strategy_name=strategy_name,
        account_ref=account_ref,
        entry_surface=entry_surface,
        source=source,
        idempotency_key=idempotency_key,
        metadata=metadata,
        extras=extras,
    )
