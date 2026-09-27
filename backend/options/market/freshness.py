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
OPTION_CHAIN_UNAVAILABLE = "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE"
OPTION_CHAIN_STALE = "OPTION_CHAIN_SNAPSHOT_STALE"
OPTION_GREEKS_UNAVAILABLE = "OPTION_GREEKS_UNAVAILABLE"
OPTION_GREEKS_STALE = "OPTION_GREEKS_STALE"
OPTION_PRICE_DRIFT_EXCEEDED = "OPTION_PRICE_DRIFT_EXCEEDED"


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


def validate_option_chain_at_send(
    plan: Mapping[str, Any],
    *,
    current_chain: Optional[Mapping[str, Any]],
    now: datetime,
    max_age_seconds: float = LIVE_OPTION_CHAIN_MAX_AGE_SECONDS,
    max_drift_pct: float = 0.005,
) -> dict[str, Any]:
    """Re-read price evidence at send, using frozen prices only as reference.

    The freeze owns *what* to trade. Freshness at send belongs to the current
    chain, not to the age of the approval: an owner may approve much later, but
    the send is still bounded by a fresh read of every frozen contract.
    """
    resolved = dict(plan.get("resolved_plan") or {})
    evidence = resolved.get("option_chain_evidence")
    legs = resolved.get("legs")
    if not isinstance(evidence, Mapping):
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {"reason": "freeze_evidence_missing"},
        )
    if not isinstance(legs, list) or not legs:
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {"reason": "resolved_legs_missing"},
        )

    frozen_underlying = str(evidence.get("underlying") or "")
    frozen_expiry = str(evidence.get("expiry") or "")
    underlying = str(resolved.get("underlying") or "")
    expiry = str(resolved.get("expiry") or "")
    if not frozen_underlying or frozen_underlying != underlying:
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {"freeze_underlying": frozen_underlying, "plan_underlying": underlying},
        )
    if not frozen_expiry or frozen_expiry != expiry:
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {"freeze_expiry": frozen_expiry, "plan_expiry": expiry},
        )
    frozen_legs = evidence.get("legs")
    if not isinstance(frozen_legs, Mapping):
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {"reason": "frozen_legs_missing"},
        )
    if current_chain is None or not isinstance(current_chain, Mapping):
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {"underlying": underlying, "expiry": expiry, "reason": "current_chain_missing"},
        )

    current_updated_at = _as_datetime(current_chain.get("updated_at"))
    if current_updated_at is None:
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {"reason": "current_snapshot_timestamp_missing"},
        )
    current_age = (now - current_updated_at).total_seconds()
    if current_age > max_age_seconds:
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_STALE,
            {
                "age_seconds": current_age,
                "max_age_seconds": max_age_seconds,
                "stage": "send",
            },
        )
    if str(current_chain.get("underlying") or "") != underlying or str(
        current_chain.get("expiry") or ""
    ) != expiry:
        raise OptionChainEvidenceRefusal(
            OPTION_CHAIN_UNAVAILABLE,
            {
                "requested_underlying": underlying,
                "requested_expiry": expiry,
                "snapshot_underlying": str(current_chain.get("underlying") or ""),
                "snapshot_expiry": str(current_chain.get("expiry") or ""),
            },
        )

    current_packets: dict[str, Mapping[str, Any]] = {}
    for chain_row in current_chain.get("chain") or []:
        if not isinstance(chain_row, Mapping):
            continue
        for option_type in ("ce", "pe"):
            packet = chain_row.get(option_type)
            if isinstance(packet, Mapping) and packet.get("token") is not None:
                current_packets[str(packet.get("token"))] = packet

    uses_greeks = _plan_uses_greeks(plan)
    checked_legs: dict[str, dict[str, Any]] = {}
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        instrument_id = str(leg.get("instrument_id") or "")
        token = str(leg.get("broker_token") if leg.get("broker_token") is not None else instrument_id)
        frozen_packet = frozen_legs.get(instrument_id)
        if not isinstance(frozen_packet, Mapping):
            raise OptionChainEvidenceRefusal(
                OPTION_CHAIN_UNAVAILABLE,
                {"instrument_id": instrument_id, "reason": "leg_missing_from_freeze"},
            )
        frozen_ltp = _finite(frozen_packet.get("ltp"))
        if frozen_ltp is None or frozen_ltp <= 0.0:
            raise OptionChainEvidenceRefusal(
                OPTION_CHAIN_UNAVAILABLE,
                {"instrument_id": instrument_id, "reason": "frozen_ltp_not_finite"},
            )
        current_packet = current_packets.get(token)
        if current_packet is None:
            raise OptionChainEvidenceRefusal(
                OPTION_CHAIN_UNAVAILABLE,
                {
                    "instrument_id": instrument_id,
                    "reason": "leg_missing_from_current_snapshot",
                    "stage": "send",
                },
            )
        current_ltp = _finite(current_packet.get("ltp"))
        if current_ltp is None or current_ltp <= 0.0:
            raise OptionChainEvidenceRefusal(
                OPTION_CHAIN_UNAVAILABLE,
                {
                    "instrument_id": instrument_id,
                    "reason": "current_ltp_not_finite",
                    "stage": "send",
                },
            )
        if uses_greeks:
            current_packet_updated_at = _as_datetime(current_packet.get("updated_at"))
            if current_packet_updated_at is None:
                raise OptionChainEvidenceRefusal(
                    OPTION_GREEKS_UNAVAILABLE,
                    {"instrument_id": instrument_id, "reason": "greek_timestamp_missing"},
                )
            greek_age = (now - current_packet_updated_at).total_seconds()
            if greek_age > max_age_seconds:
                raise OptionChainEvidenceRefusal(
                    OPTION_GREEKS_STALE,
                    {
                        "instrument_id": instrument_id,
                        "age_seconds": greek_age,
                        "max_age_seconds": max_age_seconds,
                    },
                )
            if _finite(current_packet.get("iv")) is None or _finite(current_packet.get("delta")) is None:
                raise OptionChainEvidenceRefusal(
                    OPTION_GREEKS_UNAVAILABLE,
                    {"instrument_id": instrument_id, "fields": ["iv", "delta"]},
                )

        side = str(leg.get("side") or "").upper()
        if side == "BUY":
            bound = frozen_ltp * (1.0 + max_drift_pct)
            exceeded = current_ltp > bound
        elif side == "SELL":
            bound = frozen_ltp * (1.0 - max_drift_pct)
            exceeded = current_ltp < bound
        else:
            raise OptionChainEvidenceRefusal(
                OPTION_CHAIN_UNAVAILABLE,
                {"instrument_id": instrument_id, "reason": "side_missing"},
            )
        if exceeded:
            raise OptionChainEvidenceRefusal(
                OPTION_PRICE_DRIFT_EXCEEDED,
                {
                    "instrument_id": instrument_id,
                    "side": side,
                    "frozen_ltp": frozen_ltp,
                    "current_ltp": current_ltp,
                    "max_drift_pct": max_drift_pct,
                    "bound": bound,
                },
            )
        checked_legs[instrument_id] = {
            "frozen_ltp": frozen_ltp,
            "current_ltp": current_ltp,
            "side": side,
            "bound": bound,
        }

    return {
        "underlying": underlying,
        "expiry": expiry,
        "current_snapshot_updated_at": current_updated_at.isoformat(),
        "current_age_seconds": current_age,
        "legs": checked_legs,
    }
