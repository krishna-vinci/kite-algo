"""Collect the persisted evidence a reconciliation decision depends on.

Read-only and reuses existing services — the worker run store, the paper
runtime's **read-only settlement view** (no ``ensure_account`` side effect) and
the run's protection/recovery state. It never creates a second position or
execution ledger, and never creates account state.

Any missing, malformed, unavailable or ambiguous source keeps the relevant axis
``unknown`` and the whole assessment blocked — it is never treated as flat.
Confirmed-empty positions (a run state with an empty ``positions`` list) are
distinguished from missing position data (no run state / malformed quantities).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.strategies import service as strategy_service
from backend.strategies.reconciliation import ReconciliationEvidence

__all__ = ["ReconciliationEvidenceCollector"]


def _to_int_checked(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ReconciliationEvidenceCollector:
    def __init__(
        self,
        *,
        worker_repo: Any,
        paper_runtime: Any = None,
        option_status_reader: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> None:
        self._worker = worker_repo
        self._paper = paper_runtime
        self._option_status = option_status_reader

    @staticmethod
    def _capabilities(job) -> Tuple[bool, List[str]]:
        try:
            caps = strategy_service.parse_capability_snapshot(job.capabilities_snapshot)
            return bool(caps.get("trade")), []
        except strategy_service.StrategyValidationError:
            # Ambiguous/legacy snapshot: assume trading is possible (conservative).
            return True, ["capabilities_ambiguous_treated_as_trading"]

    async def _settlement(self, job, trade_capable: bool) -> Dict[str, Any]:
        """Return work/exposure/watermark/unavailable from read-only evidence."""
        unavailable: List[str] = []
        if not trade_capable:
            # No trading authority exists, so there is no trading work or exposure
            # to settle — do not require a trading worker run to be closed.
            return {"work_state": "none", "exposure_state": "not_applicable", "unavailable": [], "watermark": None}
        mode = str(job.execution_mode or "")
        if mode == "dry_run":
            # Preview-only: no execution path exists, so nothing could be accepted.
            return {"work_state": "none", "exposure_state": "flat", "unavailable": [], "watermark": "dry_run"}
        if mode != "paper":
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["execution_mode"], "watermark": None}
        if self._paper is None or not hasattr(self._paper, "get_strategy_run_settlement_readonly"):
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_settlement"], "watermark": None}
        try:
            payload = await self._paper.get_strategy_run_settlement_readonly(
                str(job.account_scope), str(job.run_id)
            )
        except Exception:
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_settlement"], "watermark": None}
        if not isinstance(payload, dict):
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_settlement"], "watermark": None}
        # Verify run/account attribution.
        if str(payload.get("account_scope") or "") != str(job.account_scope) or str(
            payload.get("strategy_run_id") or ""
        ) != str(job.run_id):
            return {
                "work_state": "unknown",
                "exposure_state": "unknown",
                "unavailable": ["paper_settlement_attribution"],
                "watermark": None,
            }

        order_count = _to_int_checked(payload.get("order_count"))
        pending_count = _to_int_checked(payload.get("pending_order_count"))
        if order_count is None or pending_count is None or order_count < 0 or pending_count < 0:
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_settlement_counts"], "watermark": None}
        # Truncated attribution is incomplete coverage, never "settled".
        if payload.get("coverage_complete") is False:
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_settlement_incomplete"], "watermark": None}

        run_state = payload.get("run_state")
        if run_state is None:
            # No attributed orders/trades at all → confirmed no work and no exposure.
            if order_count == 0:
                return {"work_state": "none", "exposure_state": "flat", "unavailable": [], "watermark": "empty"}
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_run_state"], "watermark": None}
        if not isinstance(run_state, dict):
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_run_state"], "watermark": None}

        # Attribution inside the run state must agree with the job's run id.
        run_identity = str(run_state.get("strategy_run_id") or run_state.get("strategy_id") or "")
        if run_identity != str(job.run_id):
            return {
                "work_state": "unknown",
                "exposure_state": "unknown",
                "unavailable": ["paper_run_state_attribution"],
                "watermark": None,
            }
        if run_state.get("is_stale"):
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_run_stale"], "watermark": None}

        positions = run_state.get("positions")
        if not isinstance(positions, list):
            # Missing position data must not be read as flat.
            return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_positions"], "watermark": None}
        exposure_state = "flat"
        for position in positions:
            if not isinstance(position, dict):
                return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_positions"], "watermark": None}
            quantity = _to_int_checked(position.get("net_quantity"))
            if quantity is None:
                return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["paper_positions"], "watermark": None}
            if quantity != 0:
                exposure_state = "open"
                break

        work_state = "outstanding" if pending_count > 0 else ("settled" if order_count > 0 else "none")
        watermark = ":".join(
            [
                str(run_state.get("last_event_at") or run_state.get("last_updated_at") or ""),
                str(order_count),
                str(pending_count),
            ]
        )

        # Optional options settlement state (paper options route through the paper
        # runtime, but verify the option run state when a reader is available).
        if self._option_status is not None:
            try:
                option_status = self._option_status(str(job.account_scope), str(job.run_id))
            except Exception:
                return {"work_state": "unknown", "exposure_state": "unknown", "unavailable": ["option_run_state"], "watermark": None}
            if option_status:
                if _option_status_blocks_trading(str(option_status)):
                    work_state = "outstanding"
                watermark = f"{watermark}:opt={option_status}"

        return {"work_state": work_state, "exposure_state": exposure_state, "unavailable": unavailable, "watermark": watermark}

    async def collect(self, job) -> ReconciliationEvidence:
        unavailable: List[str] = []
        notes: List[str] = []
        launched = job.handoff_at is not None
        trade_capable, cap_notes = self._capabilities(job)
        notes.extend(cap_notes)
        if trade_capable:
            notes.append("no_execution_settlement_barrier_quiescence_unverified")

        run: Optional[Dict[str, Any]] = None
        if job.run_id:
            try:
                run = await self._worker.get_run(job.run_id)
            except Exception:
                unavailable.append("worker_run")

        token_status: Optional[str] = None
        if job.token_id:
            try:
                token_status = await self._worker.get_token_status(job.token_id)
            except Exception:
                unavailable.append("worker_token")

        job_status = str(job.status or "")
        terminal = job_status in {"recovery_required", "stopped", "failed"}
        if "worker_token" in unavailable:
            authority_state = "uncertain"
        elif token_status == "active":
            authority_state = "active"
        elif token_status == "revoked":
            authority_state = "revoked" if terminal else "uncertain"
        elif token_status is None and job.token_id is None:
            authority_state = "revoked"  # never minted: no credential exists
        else:
            authority_state = "uncertain"

        settlement = await self._settlement(job, trade_capable)
        unavailable.extend(settlement["unavailable"])
        work_state = settlement["work_state"]
        exposure_state = settlement["exposure_state"]
        watermark = settlement["watermark"]

        protection_state = "unknown"
        recovery_action_required = False
        if run is not None:
            runtime_state = dict(run.get("runtime_state") or {})
            protection = dict(runtime_state.get("backend_protection_state") or {})
            protection_state = "active" if protection.get("exit_submitted") else "settled"
            recovery = dict(runtime_state.get("runtime_recovery") or {})
            recovery_action_required = bool(recovery.get("action_required"))
        elif not launched:
            protection_state = "settled"

        replacement_blocked = job_status in {"queued", "starting", "running"} or (
            job_status == "recovery_required" and job.reconciled_at is None
        )

        return ReconciliationEvidence(
            job_id=str(job.id),
            strategy_id=str(job.strategy_id),
            attempt=int(job.attempt),
            run_id=job.run_id,
            launched=launched,
            trade_capable=trade_capable,
            execution_mode=str(job.execution_mode or ""),
            job_status=job_status,
            desired_state=str(job.desired_state or ""),
            replacement_blocked=replacement_blocked,
            process_cleanup_state=job.process_cleanup_state,
            process_cleanup_at=(job.process_cleanup_at.isoformat() if job.process_cleanup_at else None),
            process_cleanup_actor=job.process_cleanup_actor,
            authority_state=authority_state,
            run_status=(str(run.get("status")) if run else None),
            work_state=work_state,
            exposure_state=exposure_state,
            protection_state=protection_state,
            recovery_action_required=recovery_action_required,
            evidence_complete=not unavailable,
            unavailable=sorted(set(unavailable)),
            notes=notes,
            settlement_watermark=watermark,
            quiescence_state="unverified",
        )


def _option_status_blocks_trading(status: str) -> bool:
    try:
        from backend.api.services.safety import option_run_status_blocks_trading

        return bool(option_run_status_blocks_trading(status))
    except Exception:  # pragma: no cover - defensive
        return True
