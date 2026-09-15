"""Collect the persisted evidence a reconciliation decision depends on.

Everything here is **read-only** and reuses existing services — the worker run
store, the paper runtime and the run's protection/recovery state. It never
creates a second position or execution ledger. If any required source cannot be
read, the evidence is marked unavailable and the assessment fails closed.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from backend.strategies import service as strategy_service
from backend.strategies.reconciliation import ReconciliationEvidence

__all__ = ["ReconciliationEvidenceCollector"]


class ReconciliationEvidenceCollector:
    def __init__(
        self,
        *,
        worker_repo: Any,
        paper_runtime: Any = None,
    ) -> None:
        self._worker = worker_repo
        self._paper = paper_runtime

    @staticmethod
    def _capabilities(job) -> tuple:
        try:
            caps = strategy_service.parse_capability_snapshot(job.capabilities_snapshot)
            return bool(caps.get("trade")), []
        except strategy_service.StrategyValidationError:
            # Ambiguous/legacy snapshot: assume trading is possible (conservative)
            # and require the full trading evidence before unblocking.
            return True, ["capabilities_ambiguous_treated_as_trading"]

    @staticmethod
    def _work_state(job, run: Optional[Dict[str, Any]], launched: bool) -> str:
        if not launched:
            return "none"
        if run is None:
            return "unknown" if job.run_id else "none"
        status = str(run.get("status") or "")
        if status in {"closed", "failed"}:
            return "settled"
        if status in {"open", "paused", "exiting"}:
            return "outstanding"
        return "unknown"

    async def _exposure(
        self, job, run: Optional[Dict[str, Any]], trade_capable: bool
    ) -> tuple:
        if not trade_capable:
            return "not_applicable", []
        mode = str(job.execution_mode or "")
        if mode == "dry_run":
            return "flat", []
        if mode == "paper":
            if self._paper is None:
                return "unknown", ["paper_runtime"]
            if not job.run_id:
                return "flat", []
            try:
                pnl = await self._paper.get_strategy_run_pnl(str(job.account_scope), str(job.run_id))
            except Exception:
                return "unknown", ["paper_pnl"]
            if not isinstance(pnl, dict):
                return "flat", []
            positions = list(pnl.get("positions") or pnl.get("legs") or [])
            for position in positions:
                try:
                    if int(position.get("net_quantity") or 0) != 0:
                        return "open", []
                except (TypeError, ValueError):
                    return "unknown", ["paper_pnl"]
            return "flat", []
        if mode == "live":
            if not job.run_id:
                return "flat", []
            try:
                positions = await self._worker.list_live_strategy_broker_positions(
                    strategy_run_id=str(job.run_id), account_id=str(job.account_scope)
                )
            except Exception:
                return "unknown", ["live_positions"]
            for position in positions or []:
                try:
                    if int(position.get("net_quantity") or 0) != 0:
                        return "open", []
                except (TypeError, ValueError):
                    return "unknown", ["live_positions"]
            return "flat", []
        return "unknown", ["execution_mode"]

    async def collect(self, job) -> ReconciliationEvidence:
        unavailable: List[str] = []
        notes: List[str] = []
        launched = job.handoff_at is not None
        trade_capable, cap_notes = self._capabilities(job)
        notes.extend(cap_notes)

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

        work_state = self._work_state(job, run, launched)
        exposure_state, exposure_unavailable = await self._exposure(job, run, trade_capable)
        unavailable.extend(exposure_unavailable)

        protection_state = "unknown"
        recovery_action_required = False
        if run is not None:
            runtime_state = dict(run.get("runtime_state") or {})
            protection = dict(runtime_state.get("backend_protection_state") or {})
            protection_state = "active" if protection.get("exit_submitted") else "settled"
            recovery = dict(runtime_state.get("runtime_recovery") or {})
            recovery_action_required = bool(recovery.get("action_required"))

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
        )
