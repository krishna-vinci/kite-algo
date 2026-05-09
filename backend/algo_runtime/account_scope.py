from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ExecutionScopeMode = Literal["paper", "live"]


@dataclass(frozen=True)
class ParsedAccountScope:
    raw: str
    normalized: str
    mode: ExecutionScopeMode
    paper_key: str | None
    live_account_ref: str | None
    broker_user_id: str | None


def parse_account_scope(value: str) -> ParsedAccountScope:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("account_scope is required")
    if ":" not in raw:
        raise ValueError(f"Unsupported account_scope '{raw}'")
    prefix, identifier = raw.split(":", 1)
    normalized_prefix = str(prefix or "").strip().lower()
    normalized_identifier = str(identifier or "").strip()
    if normalized_prefix != "kite" or not normalized_identifier:
        raise ValueError(f"Unsupported account_scope '{raw}'")
    normalized = f"kite:{normalized_identifier}"
    lowered_identifier = normalized_identifier.lower()
    is_paper_scope = (
        lowered_identifier == "paper"
        or lowered_identifier.startswith("paper-")
        or lowered_identifier.startswith("paper_")
        or lowered_identifier.startswith("test-paper")
        or lowered_identifier.endswith("-paper")
        or lowered_identifier.endswith("_paper")
    )
    if is_paper_scope:
        return ParsedAccountScope(
            raw=raw,
            normalized=normalized,
            mode="paper",
            paper_key=normalized,
            live_account_ref=None,
            broker_user_id=None,
        )
    return ParsedAccountScope(
        raw=raw,
        normalized=normalized,
        mode="live",
        paper_key=None,
        live_account_ref=normalized,
        broker_user_id=normalized_identifier,
    )


def is_paper_account_scope(value: str) -> bool:
    return parse_account_scope(value).mode == "paper"


def is_live_account_scope(value: str) -> bool:
    return parse_account_scope(value).mode == "live"
