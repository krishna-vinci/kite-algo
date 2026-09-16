"""Operator reconciliation: classify whether a blocked attempt may be unblocked.

This module is deliberately **pure**: it takes a persisted evidence snapshot and
decides whether server-side evidence supports clearing the replacement block. It
never trusts a caller assertion (there is no ``flat=true``/``reconciled=true``
input) and never treats a terminal job label alone as proof of process cleanup or
flatness.

Evidence axes (each explicitly distinguished):

- **process** — ``confirmed`` (supervisor proved the child process group is gone),
  ``unresolved`` (it could not), or ``None`` (unknown/unreported).
- **authority** — child credential ``revoked`` vs still ``active`` vs
  ``uncertain``.
- **work** — ``none`` / ``settled`` / ``outstanding`` / ``unknown``.
- **exposure** — ``not_applicable`` (data-only), ``flat``, ``open``, ``unknown``.
- **protection** — ``settled`` / ``active`` (exit in progress) / ``unknown``.
- **availability** — any required source that could not be read makes the whole
  assessment blocked (``EVIDENCE_UNAVAILABLE``).

The classification yields one of four explicit cases:

1. ``unlaunched`` — no child credential handoff and no accepted work.
2. ``data_only_completed`` — no trading capability/work, cleanup established.
3. ``trading_settled_flat`` — trading-capable, cleanup confirmed, work settled,
   exposure flat, authority revoked.
4. ``blocked`` — open exposure, outstanding/unknown work, uncertain cleanup or
   authority, or unavailable evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "CASE_BLOCKED",
    "CASE_DATA_ONLY_COMPLETED",
    "CASE_NOT_BLOCKED",
    "CASE_TRADING_SETTLED_FLAT",
    "CASE_UNLAUNCHED",
    "ReconciliationAssessment",
    "ReconciliationEvidence",
    "assess",
    "evidence_digest",
]

CASE_UNLAUNCHED = "unlaunched"
CASE_DATA_ONLY_COMPLETED = "data_only_completed"
CASE_TRADING_SETTLED_FLAT = "trading_settled_flat"
CASE_BLOCKED = "blocked"
CASE_NOT_BLOCKED = "not_blocked"

#: Stable blocking reason codes for the frontend.
BLOCK_PROCESS_CLEANUP_UNKNOWN = "PROCESS_CLEANUP_UNKNOWN"
BLOCK_PROCESS_CLEANUP_UNRESOLVED = "PROCESS_CLEANUP_UNRESOLVED"
BLOCK_AUTHORITY_ACTIVE = "AUTHORITY_ACTIVE"
BLOCK_AUTHORITY_UNCERTAIN = "AUTHORITY_UNCERTAIN"
BLOCK_OUTSTANDING_WORK = "OUTSTANDING_WORK"
BLOCK_WORK_UNKNOWN = "WORK_UNKNOWN"
BLOCK_OPEN_EXPOSURE = "OPEN_EXPOSURE"
BLOCK_EXPOSURE_UNKNOWN = "EXPOSURE_UNKNOWN"
BLOCK_PROTECTION_UNKNOWN = "PROTECTION_UNKNOWN"
BLOCK_RECOVERY_ACTION_PENDING = "RECOVERY_ACTION_PENDING"
BLOCK_EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
BLOCK_EXECUTION_QUIESCENCE_UNVERIFIED = "EXECUTION_QUIESCENCE_UNVERIFIED"
BLOCK_NOT_BLOCKED = "HOSTED_JOB_NOT_BLOCKED"
BLOCK_JOB_ACTIVE = "HOSTED_JOB_ACTIVE"

_ACTIVE_STATUSES = {"queued", "starting", "running"}


@dataclass
class ReconciliationEvidence:
    job_id: str
    strategy_id: str
    attempt: int
    run_id: Optional[str] = None
    launched: bool = False
    trade_capable: bool = False
    execution_mode: str = "paper"
    job_status: str = "queued"
    desired_state: str = "started"
    replacement_blocked: bool = False
    process_cleanup_state: Optional[str] = None
    process_cleanup_at: Optional[str] = None
    process_cleanup_actor: Optional[str] = None
    authority_state: str = "uncertain"  # revoked | active | uncertain
    run_status: Optional[str] = None
    work_state: str = "unknown"  # none | settled | outstanding | unknown
    exposure_state: str = "unknown"  # not_applicable | flat | open | unknown
    protection_state: str = "unknown"  # settled | active | unknown
    recovery_action_required: bool = False
    evidence_complete: bool = True
    unavailable: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: Opaque watermark over the external settlement evidence (last event time /
    #: pending counts). Used to detect that execution evidence changed between
    #: assessment and commit.
    settlement_watermark: Optional[str] = None
    #: Whether execution quiescence is established by a durable barrier/version.
    #: There is no such barrier today, so this is ``unverified`` and a
    #: trading-capable attempt stays blocked.
    quiescence_state: str = "unverified"  # verified | unverified

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evidence_digest(evidence: ReconciliationEvidence) -> str:
    """A stable digest of the evidence axes that gate reconciliation.

    Re-computed immediately before commit; a change means outstanding/in-flight
    execution evidence moved under us, so reconciliation fails closed.
    """
    canonical = {
        "job_id": evidence.job_id,
        "attempt": evidence.attempt,
        "job_status": evidence.job_status,
        "launched": evidence.launched,
        "trade_capable": evidence.trade_capable,
        "process_cleanup_state": evidence.process_cleanup_state,
        "authority_state": evidence.authority_state,
        "run_status": evidence.run_status,
        "work_state": evidence.work_state,
        "exposure_state": evidence.exposure_state,
        "protection_state": evidence.protection_state,
        "recovery_action_required": evidence.recovery_action_required,
        "evidence_complete": evidence.evidence_complete,
        "unavailable": sorted(evidence.unavailable),
        "settlement_watermark": evidence.settlement_watermark,
        "quiescence_state": evidence.quiescence_state,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ReconciliationAssessment:
    allowed: bool
    case: str
    reason_code: str
    blocking_reasons: List[str]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _process_blockers(evidence: ReconciliationEvidence) -> List[str]:
    if evidence.process_cleanup_state == "unresolved":
        return [BLOCK_PROCESS_CLEANUP_UNRESOLVED]
    if evidence.process_cleanup_state != "confirmed":
        return [BLOCK_PROCESS_CLEANUP_UNKNOWN]
    return []


def _authority_blockers(evidence: ReconciliationEvidence) -> List[str]:
    if evidence.authority_state == "active":
        return [BLOCK_AUTHORITY_ACTIVE]
    if evidence.authority_state != "revoked":
        return [BLOCK_AUTHORITY_UNCERTAIN]
    return []


def _work_blockers(evidence: ReconciliationEvidence) -> List[str]:
    if evidence.work_state == "outstanding":
        return [BLOCK_OUTSTANDING_WORK]
    if evidence.work_state == "unknown":
        return [BLOCK_WORK_UNKNOWN]
    return []


def _exposure_blockers(evidence: ReconciliationEvidence) -> List[str]:
    if evidence.exposure_state == "open":
        return [BLOCK_OPEN_EXPOSURE]
    if evidence.exposure_state == "unknown":
        return [BLOCK_EXPOSURE_UNKNOWN]
    return []


def assess(evidence: ReconciliationEvidence) -> ReconciliationAssessment:
    """Classify a blocked attempt. Fails closed on any missing evidence."""
    notes = list(evidence.notes)

    # An actively running attempt is not a reconciliation target (stop it first).
    if evidence.job_status in _ACTIVE_STATUSES:
        return ReconciliationAssessment(
            allowed=False,
            case=CASE_BLOCKED,
            reason_code=BLOCK_JOB_ACTIVE,
            blocking_reasons=[BLOCK_JOB_ACTIVE],
            notes=notes,
        )

    if not evidence.replacement_blocked:
        return ReconciliationAssessment(
            allowed=False,
            case=CASE_NOT_BLOCKED,
            reason_code=BLOCK_NOT_BLOCKED,
            blocking_reasons=[],
            notes=notes,
        )

    # A terminal job label alone proves nothing; require the evidence axes.
    if not evidence.evidence_complete or evidence.unavailable:
        return ReconciliationAssessment(
            allowed=False,
            case=CASE_BLOCKED,
            reason_code=BLOCK_EVIDENCE_UNAVAILABLE,
            blocking_reasons=[BLOCK_EVIDENCE_UNAVAILABLE],
            notes=notes + [f"unavailable:{item}" for item in evidence.unavailable],
        )

    blockers: List[str] = []
    blockers += _work_blockers(evidence)
    blockers += _exposure_blockers(evidence)
    if evidence.recovery_action_required:
        blockers.append(BLOCK_RECOVERY_ACTION_PENDING)

    # Case 1: unlaunched — no credential handoff, so no child and no work.
    if not evidence.launched:
        if blockers:
            return ReconciliationAssessment(False, CASE_BLOCKED, blockers[0], blockers, notes)
        notes.append("no child credential was handed off")
        return ReconciliationAssessment(True, CASE_UNLAUNCHED, "UNLAUNCHED", [], notes)

    # Launched: process cleanup and authority must be established.
    process_blockers = _process_blockers(evidence)
    authority_blockers = _authority_blockers(evidence)

    # Case 2: data-only — no trading authority/work, cleanup established.
    if not evidence.trade_capable:
        if evidence.exposure_state not in ("not_applicable", "flat"):
            blockers += _exposure_blockers(evidence)
        blockers += process_blockers + authority_blockers
        if blockers:
            return ReconciliationAssessment(False, CASE_BLOCKED, blockers[0], blockers, notes)
        notes.append("data-only attempt; no trading authority or work")
        return ReconciliationAssessment(True, CASE_DATA_ONLY_COMPLETED, "DATA_ONLY_COMPLETED", [], notes)

    # Case 3: trading-capable with settled work, flat exposure, revoked authority.
    # Evidence-integrity blockers (process cleanup, authority) come first; then
    # proven execution quiescence. There is no durable execution-settlement
    # barrier in the platform today, so quiescence cannot be established and
    # trading-capable reconciliation stays blocked (a terminal job label and two
    # matching reads are NOT proof that already-admitted work cannot complete).
    blockers += process_blockers + authority_blockers
    if evidence.exposure_state != "flat":
        blockers += _exposure_blockers(evidence)
    if evidence.protection_state == "unknown":
        blockers.append(BLOCK_PROTECTION_UNKNOWN)
    elif evidence.protection_state == "active":
        blockers.append(BLOCK_RECOVERY_ACTION_PENDING)
    # Quiescence is the residual blocker once the concrete evidence is clean:
    # a terminal job label and two matching reads are NOT proof that
    # already-admitted work cannot complete afterwards.
    if evidence.quiescence_state != "verified":
        blockers.append(BLOCK_EXECUTION_QUIESCENCE_UNVERIFIED)
    blockers = list(dict.fromkeys(blockers))
    if blockers:
        return ReconciliationAssessment(False, CASE_BLOCKED, blockers[0], blockers, notes)
    notes.append("process cleanup confirmed; work settled; no exposure; authority revoked")
    return ReconciliationAssessment(True, CASE_TRADING_SETTLED_FLAT, "TRADING_SETTLED_FLAT", [], notes)
