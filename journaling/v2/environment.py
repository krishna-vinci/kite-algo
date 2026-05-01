from __future__ import annotations

from typing import Any

from algo_runtime.account_scope import parse_account_scope
from journaling.models import JournalEnvironmentMode, JournalExecutionEnvironment


def resolve_environment_key(
    *,
    mode: str,
    account_scope: str | None = None,
    broker_user_id: str | None = None,
    paper_account_key: str | None = None,
    environment_epoch: int = 1,
    display_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> JournalExecutionEnvironment:
    normalized_mode = _normalize_mode(mode)
    if environment_epoch < 1:
        raise ValueError("environment_epoch must be >= 1")

    payload_metadata = dict(metadata or {})

    if normalized_mode == JournalEnvironmentMode.DRY_RUN_PREVIEW:
        resolved_scope = _resolve_preview_scope(account_scope)
        return JournalExecutionEnvironment(
            mode=JournalEnvironmentMode.DRY_RUN_PREVIEW,
            account_scope=resolved_scope,
            broker_user_id=None,
            paper_account_key=None,
            environment_epoch=environment_epoch,
            display_name=display_name,
            metadata=payload_metadata,
        )

    cleaned_scope = str(account_scope or "").strip()
    if not cleaned_scope:
        raise ValueError("account_scope is required")
    parsed_scope = parse_account_scope(cleaned_scope)

    if normalized_mode == JournalEnvironmentMode.LIVE:
        if parsed_scope.mode != "live":
            raise ValueError("live mode requires a live account_scope")
        if paper_account_key is not None:
            raise ValueError("paper_account_key is not allowed for live mode")
        resolved_broker_user_id = _clean_optional(broker_user_id) or parsed_scope.broker_user_id
        return JournalExecutionEnvironment(
            mode=JournalEnvironmentMode.LIVE,
            account_scope=parsed_scope.normalized,
            broker_user_id=resolved_broker_user_id,
            paper_account_key=None,
            environment_epoch=environment_epoch,
            display_name=display_name,
            metadata=payload_metadata,
        )

    if parsed_scope.mode != "paper":
        raise ValueError("paper mode requires a paper account_scope")
    if broker_user_id is not None:
        raise ValueError("broker_user_id is not allowed for paper mode")
    resolved_paper_key = _clean_optional(paper_account_key) or parsed_scope.paper_key
    return JournalExecutionEnvironment(
        mode=JournalEnvironmentMode.PAPER,
        account_scope=parsed_scope.normalized,
        broker_user_id=None,
        paper_account_key=resolved_paper_key,
        environment_epoch=environment_epoch,
        display_name=display_name,
        metadata=payload_metadata,
    )


def environment_identity_tuple(resolved: JournalExecutionEnvironment) -> tuple[str, str, str, str, int]:
    return (
        str(resolved.mode),
        resolved.account_scope,
        resolved.broker_user_id or "",
        resolved.paper_account_key or "",
        resolved.environment_epoch,
    )


def _normalize_mode(mode: str) -> JournalEnvironmentMode:
    cleaned_mode = str(mode or "").strip().lower()
    if cleaned_mode == "dry_run":
        cleaned_mode = JournalEnvironmentMode.DRY_RUN_PREVIEW.value
    try:
        return JournalEnvironmentMode(cleaned_mode)
    except ValueError as exc:
        raise ValueError(f"Unsupported environment mode '{mode}'") from exc


def _resolve_preview_scope(account_scope: str | None) -> str:
    cleaned_scope = str(account_scope or "").strip()
    if not cleaned_scope:
        return "preview:default"
    parsed_scope = parse_account_scope(cleaned_scope)
    return parsed_scope.normalized


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
