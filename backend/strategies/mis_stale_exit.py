"""MIS stale-worker exit: the policy's own attachment, with evidence (D-3).

A MIS worker that stops heartbeating leaves an *intraday* position behind, which is
exactly the case the square-off schedule exists for — except the schedule fires at
15:20 and a stale worker may have died at 09:45. ``exit_on_worker_stale`` is the
policy that says "do not wait for the close".

This module attaches that policy at the strategy-policy level rather than inside
the enforced protection runtime, for two reasons. The protection runtime's
semantics are explicitly out of scope in this phase — it already owns the live
stale path, and duplicating its decision would give two places the authority to
liquidate. And the thing this phase adds is the *evidence*: which attributed legs
were exited, how large the exit was, and that it went through the durable claim
path rather than being issued directly.

The sizing rule is the Phase 8 one and it is not a safety margin: an exit is
clamped to the strategy's attributed quantity, so a stale MIS strategy cannot
reach another strategy's shares, whatever the policy says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from backend.strategies.mis_squareoff import (
    MisSquareoffEvidenceStore,
    SquareoffRecord,
    attributed_exit_size,
)

#: The policy key that enables this attachment (mirrors the protection vocabulary).
STALE_EXIT_POLICY = "exit_on_worker_stale"

#: Heartbeat age bounds, mirroring ``OperationalProtection``'s validation so a
#: policy accepted there is accepted here.
MIN_STALE_SECONDS = 30
MAX_STALE_SECONDS = 86400


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class StaleExitOutcome:
    acted: bool
    reason: str
    exited: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)


class MisStaleExitPolicy:
    """Exits a stale MIS strategy's attributed legs, through the claim path."""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        positions_loader: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        claim_submitter: Optional[Callable[..., Any]] = None,
        evidence_store: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session_factory = session_factory
        self._positions_loader = positions_loader
        self._claim_submitter = claim_submitter
        self._clock = clock or _utcnow
        if evidence_store is None:
            evidence_store = MisSquareoffEvidenceStore(session_factory=session_factory)
        self.evidence = evidence_store

    # -- policy reads -------------------------------------------------------

    @staticmethod
    def policy_for(protection: Mapping[str, Any]) -> Dict[str, Any]:
        """Whether this run's protection asks for a stale-worker exit."""
        operations = dict((protection or {}).get("operations") or {})
        enabled = bool((protection or {}).get("enabled")) and bool(
            operations.get(STALE_EXIT_POLICY)
        )
        stale_seconds = operations.get("worker_stale_sec")
        try:
            stale_seconds = int(stale_seconds) if stale_seconds is not None else None
        except (TypeError, ValueError):
            stale_seconds = None
        return {"enabled": enabled, "worker_stale_sec": stale_seconds}

    @staticmethod
    def is_stale(*, last_heartbeat_at: Any, stale_seconds: Optional[int], now: datetime) -> bool:
        """Whether the worker has been silent long enough to act on."""
        if not stale_seconds:
            return False
        heartbeat = _as_datetime(last_heartbeat_at)
        if heartbeat is None:
            # Never heartbeated: unknown, and unknown is not a reason to liquidate
            # somebody's position. The recovery path classifies it instead.
            return False
        return (now - heartbeat).total_seconds() > int(stale_seconds)

    # -- the exit -----------------------------------------------------------

    async def apply(
        self,
        run: Mapping[str, Any],
        *,
        positions: Optional[List[Dict[str, Any]]] = None,
        session_date: Any = None,
        now: Optional[datetime] = None,
    ) -> StaleExitOutcome:
        """Exit a stale MIS run's attributed legs, or explain why not."""
        moment = now or self._clock()
        runtime_state = dict(run.get("runtime_state") or {})
        policy = self.policy_for(runtime_state.get("backend_protection") or {})
        if not policy["enabled"]:
            return StaleExitOutcome(False, "policy_disabled")

        if not self.is_stale(
            last_heartbeat_at=run.get("last_heartbeat_at"),
            stale_seconds=policy["worker_stale_sec"],
            now=moment,
        ):
            return StaleExitOutcome(False, "worker_not_stale")

        legs = (
            list(positions)
            if positions is not None
            else self._load_positions(run)
        )
        # Only MIS: this policy is the intraday lane's, and exiting a CNC book on a
        # stale worker would liquidate a position that was never at risk.
        mis_legs = [
            dict(leg)
            for leg in legs
            if str(leg.get("product") or "").upper() == "MIS"
        ]
        if not mis_legs:
            return StaleExitOutcome(False, "no_mis_exposure", detail={"legs_seen": len(legs)})

        exited: List[Dict[str, Any]] = []
        evidence: List[Dict[str, Any]] = []
        for leg in mis_legs:
            attributed = int(leg.get("attributed_quantity", leg.get("net_quantity", 0)) or 0)
            # The exit closes the book, so its direction is opposite, and its
            # magnitude is clamped to what the strategy actually owns.
            wanted = -attributed
            sized = attributed_exit_size(
                attributed_quantity=attributed, requested_quantity=wanted
            )
            if sized == 0:
                continue
            claim_id = await self._submit_claim(run, leg, sized)
            record = self.evidence.record(
                SquareoffRecord(
                    account_id=str(run.get("account_scope") or ""),
                    strategy_id=str(leg.get("strategy_id") or ""),
                    strategy_run_id=str(run.get("strategy_run_id") or ""),
                    product="MIS",
                    session_date=session_date or moment.date(),
                    exchange=str(leg.get("exchange") or ""),
                    scheduled_at=moment,
                    outcome="stale_worker_exit",
                    exit_claim_id=claim_id,
                    detail={
                        "attributed_quantity": attributed,
                        "exit_quantity": sized,
                        "worker_stale_sec": policy["worker_stale_sec"],
                        "tradingsymbol": leg.get("tradingsymbol"),
                    },
                )
            )
            exited.append({"tradingsymbol": leg.get("tradingsymbol"), "quantity": sized})
            evidence.append(record)

        if not exited:
            return StaleExitOutcome(False, "nothing_to_exit", detail={"legs_seen": len(legs)})
        return StaleExitOutcome(True, "stale_worker_exit", exited=exited, evidence=evidence)

    # -- internals ----------------------------------------------------------

    def _load_positions(self, run: Mapping[str, Any]) -> List[Dict[str, Any]]:
        if self._positions_loader is not None:
            return list(self._positions_loader(run) or [])
        return []

    async def _submit_claim(
        self, run: Mapping[str, Any], leg: Mapping[str, Any], quantity: int
    ) -> Optional[str]:
        """Submit through the durable claim path, never directly.

        The claim path is what makes the exit idempotent and observable; issuing
        the order directly would bypass the mechanism that exists precisely so a
        stale job cannot exit twice.
        """
        if self._claim_submitter is None:
            return None
        claim_id = await self._claim_submitter(run, leg, quantity)
        return str(claim_id) if claim_id else None
