"""Freeze and freshness checks for the canonical option-chain snapshot.

The option market service is the single source for chain rows and Greek packets.
These helpers never re-derive a price or Greek: they bind the exact frozen legs
to the evidence that compiler had when the plan was made immutable.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


OPTION_CHAIN_MAX_AGE_SECONDS = 10.0
LIVE_OPTION_CHAIN_MAX_AGE_SECONDS = 5.0


class OptionChainEvidenceRefusal(Exception):
    """A named refusal from freeze or pre-send freshness validation."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _snapshot_digest(chain: Mapping[str, Any]) -> str:
    canonical = json.dumps(chain, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan_uses_greeks(plan: Mapping[str, Any]) -> bool:
    """Whether IV/delta are semantics of the plan, not optional diagnostics."""

    def contains(values: Any) -> bool:
        if isinstance(values, Mapping):
            return any(
                str(key).lower() in {"iv", "delta", "delta_target", "target_delta"}
                or contains(value)
                for key, value in values.items()
                if str(key) != "option_chain_evidence"
            )
        if isinstance(values, (list, tuple)):
            return any(contains(value) for value in values)
        return False

    return contains(plan)


def option_chain_freeze_evidence(
    market_service: Any,
    plan: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Bind every resolved option leg to one session snapshot, or refuse."""
    moment = now or _utcnow()
    resolved = dict(plan.get("resolved_plan") or {})
    underlying = str(resolved.get("underlying") or "")
    expiry = str(resolved.get("expiry") or "")
    legs = resolved.get("legs")
    if not underlying or not expiry or not isinstance(legs, list) or not legs:
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
            {"underlying": underlying, "expiry": expiry},
        )
    try:
        chain = dict(market_service.get_chain(underlying, expiry) or {})
    except OptionChainEvidenceRefusal:
        raise
    except Exception as exc:
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
            {"underlying": underlying, "expiry": expiry, "reason": str(exc)},
        ) from exc
    updated_at = _as_datetime(chain.get("updated_at"))
    if updated_at is None:
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
            {"underlying": underlying, "expiry": expiry, "message": "snapshot has no timestamp"},
        )
    age = (moment - updated_at).total_seconds()
    if age > OPTION_CHAIN_MAX_AGE_SECONDS:
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_STALE",
            {"age_seconds": age, "max_age_seconds": OPTION_CHAIN_MAX_AGE_SECONDS},
        )
    if str(chain.get("expiry") or "") != expiry:
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
            {"requested_expiry": expiry, "snapshot_expiry": str(chain.get("expiry") or "")},
        )

    try:
        greeks = dict(market_service.get_greeks(underlying, expiry) or {})
    except OptionChainEvidenceRefusal:
        raise
    except Exception as exc:
        raise OptionChainEvidenceRefusal(
            "OPTION_GREEKS_UNAVAILABLE",
            {"underlying": underlying, "expiry": expiry, "reason": str(exc)},
        ) from exc

    packets: dict[str, dict[str, Any]] = {}
    for chain_row in chain.get("chain") or []:
        if not isinstance(chain_row, Mapping):
            continue
        for option_type in ("ce", "pe"):
            packet = chain_row.get(option_type)
            if not isinstance(packet, Mapping) or packet.get("token") is None:
                continue
            packets[str(packet.get("token"))] = dict(packet)

    leg_evidence: dict[str, Any] = {}
    uses_greeks = _plan_uses_greeks(plan)
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        instrument_id = str(leg.get("instrument_id") or "")
        token = str(leg.get("broker_token") if leg.get("broker_token") is not None else instrument_id)
        packet = packets.get(token)
        if packet is None:
            raise OptionChainEvidenceRefusal(
                "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
                {"instrument_id": instrument_id, "reason": "leg_missing_from_snapshot"},
            )
        ltp = _finite(packet.get("ltp"))
        if ltp is None:
            raise OptionChainEvidenceRefusal(
                "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
                {"instrument_id": instrument_id, "reason": "ltp_not_finite"},
            )
        greek_updated_at = _as_datetime(packet.get("updated_at"))
        if greek_updated_at is None:
            raise OptionChainEvidenceRefusal(
                "OPTION_GREEKS_UNAVAILABLE",
                {"instrument_id": instrument_id, "reason": "greek_timestamp_missing"},
            )
        greek_age = (moment - greek_updated_at).total_seconds()
        if greek_age > OPTION_CHAIN_MAX_AGE_SECONDS:
            raise OptionChainEvidenceRefusal(
                "OPTION_GREEKS_STALE",
                {"instrument_id": instrument_id, "age_seconds": greek_age},
            )
        evidence_leg: dict[str, Any] = {
            "tradingsymbol": str(packet.get("tsym") or leg.get("tradingsymbol") or ""),
            "ltp": ltp,
            "greek_updated_at": greek_updated_at.isoformat() if greek_updated_at else None,
        }
        if uses_greeks:
            iv = _finite(packet.get("iv"))
            delta = _finite(packet.get("delta"))
            if iv is None:
                raise OptionChainEvidenceRefusal(
                    "OPTION_GREEKS_UNAVAILABLE",
                    {"instrument_id": instrument_id, "field": "iv"},
                )
            if delta is None:
                raise OptionChainEvidenceRefusal(
                    "OPTION_GREEKS_UNAVAILABLE",
                    {"instrument_id": instrument_id, "field": "delta"},
                )
            evidence_leg["iv"] = iv
            evidence_leg["delta"] = delta
        leg_evidence[instrument_id] = evidence_leg

    return {
        "underlying": str(chain.get("underlying") or underlying),
        "expiry": expiry,
        "snapshot_updated_at": updated_at.isoformat(),
        "snapshot_digest": _snapshot_digest(chain),
        "legs": leg_evidence,
    }


def validate_option_chain_evidence(
    plan: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float = LIVE_OPTION_CHAIN_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate already-frozen evidence immediately before a live send."""
    evidence = (plan.get("resolved_plan") or {}).get("option_chain_evidence")
    if not isinstance(evidence, Mapping):
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE", {"reason": "freeze_evidence_missing"}
        )
    updated_at = _as_datetime(evidence.get("snapshot_updated_at"))
    if updated_at is None:
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE", {"reason": "snapshot_timestamp_missing"}
        )
    age = (now - updated_at).total_seconds()
    if age > max_age_seconds:
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_STALE",
            {"age_seconds": age, "max_age_seconds": max_age_seconds},
        )

    resolved_legs = (plan.get("resolved_plan") or {}).get("legs")
    frozen_legs = evidence.get("legs")
    if not isinstance(frozen_legs, Mapping) or not isinstance(resolved_legs, list):
        raise OptionChainEvidenceRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE", {"reason": "frozen_legs_missing"}
        )
    uses_greeks = _plan_uses_greeks(plan)
    for leg in resolved_legs:
        if not isinstance(leg, Mapping):
            continue
        instrument_id = str(leg.get("instrument_id") or "")
        packet = frozen_legs.get(instrument_id)
        if not isinstance(packet, Mapping):
            raise OptionChainEvidenceRefusal(
                "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
                {"instrument_id": instrument_id, "reason": "leg_missing_from_freeze"},
            )
        ltp = _finite(packet.get("ltp"))
        if ltp is None:
            raise OptionChainEvidenceRefusal(
                "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
                {"instrument_id": instrument_id, "reason": "frozen_ltp_not_finite"},
            )
        greek_updated_at = _as_datetime(packet.get("greek_updated_at"))
        if greek_updated_at is None:
            raise OptionChainEvidenceRefusal(
                "OPTION_GREEKS_UNAVAILABLE", {"instrument_id": instrument_id}
            )
        greek_age = (now - greek_updated_at).total_seconds()
        if greek_age > max_age_seconds:
            raise OptionChainEvidenceRefusal(
                "OPTION_GREEKS_STALE",
                {"instrument_id": instrument_id, "age_seconds": greek_age},
            )
        if uses_greeks and (_finite(packet.get("iv")) is None or _finite(packet.get("delta")) is None):
            raise OptionChainEvidenceRefusal(
                "OPTION_GREEKS_UNAVAILABLE",
                {"instrument_id": instrument_id, "fields": ["iv", "delta"]},
            )
    return dict(evidence)
