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
from backend.strategies.account_truth import INGEST_IDLE
from backend.strategies.reconciliation import ReconciliationEvidence, barrier_quiescence_state

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
        settlement_barrier: Optional[Any] = None,
        session_factory: Any = None,
    ) -> None:
        self._worker = worker_repo
        self._paper = paper_runtime
        self._option_status = option_status_reader
        #: The LIVE settlement sources (the strategy's attributed live book and
        #: the account's ingest truth) are read from the platform database -- the
        #: paper runtime is never consulted for a live book.
        self._session_factory = session_factory
        # The settlement barrier (D-4) backs ``quiescence_state``: ``verified``
        # only on a valid proof for the job's strategy book. ``None`` means the
        # default store is constructed lazily on first use.
        self._settlement_barrier = settlement_barrier

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
        if mode == "live":
            return self._live_settlement(job)
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
        return await self._collect(job)

    # ------------------------------------------------------------------ live

    def _live_session(self):
        if self._session_factory is None:
            from backend.app.database import SessionLocal

            self._session_factory = SessionLocal
        return self._session_factory()

    def _live_settlement(self, job) -> Dict[str, Any]:
        """LIVE settlement evidence: the attributed strategy book + account truth.

        Deliberately NOT the paper collector and NOT an account-net-flat check:

        * the run binding must name this job's strategy/account and the ``live``
          environment, or the evidence is attributed to something else and stays
          ``unknown``;
        * the exposure axis is the strategy's OWN published live attribution
          projection, so a flat *account* cannot hide an open *strategy* book;
        * work is ``outstanding`` while any durable live plan step for this book
          is unresolved (pending/partial/finalizing/rejecting/uncertain/
          repair_required) -- a staged step whose effects are not yet confirmed
          is in flight, not settled;
        * account truth must be COMPLETE (a finished ingest cycle) before the
          book is treated as read; missing/unrefreshable truth is ``unknown``,
          never flat.
        """
        from sqlalchemy import text

        account_id = str(job.account_scope or "")
        strategy_id = str(job.strategy_id or "")
        run_id = str(job.run_id or "")
        unavailable: List[str] = []
        session = self._live_session()
        try:
            binding = session.execute(
                text(
                    """
                    SELECT strategy_id, owner_id, account_id, execution_environment
                    FROM public.strategy_run_bindings
                    WHERE strategy_run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            ).mappings().first()
            if binding is None:
                return {
                    "work_state": "unknown",
                    "exposure_state": "unknown",
                    "unavailable": ["live_run_binding"],
                    "watermark": None,
                }
            if (
                str(binding["strategy_id"]) != strategy_id
                or str(binding["account_id"]) != account_id
                or str(binding["execution_environment"]) != "live"
            ):
                return {
                    "work_state": "unknown",
                    "exposure_state": "unknown",
                    "unavailable": ["live_run_binding_attribution"],
                    "watermark": None,
                }

            publication = session.execute(
                text(
                    """
                    SELECT projection_version FROM public.strategy_projection_state
                    WHERE account_id = :account_id AND strategy_id = :strategy_id
                      AND execution_environment = 'live'
                    """
                ),
                {"account_id": account_id, "strategy_id": strategy_id},
            ).first()
            positions = session.execute(
                text(
                    """
                    SELECT net_quantity FROM public.strategy_position_projection
                    WHERE account_id = :account_id AND strategy_id = :strategy_id
                      AND execution_environment = 'live'
                    """
                ),
                {"account_id": account_id, "strategy_id": strategy_id},
            ).fetchall()
            unresolved_steps = session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM public.live_plan_submissions
                    WHERE account_id = :account_id AND strategy_id = :strategy_id
                      AND execution_environment = 'live'
                      AND state NOT IN ('filled', 'rejected', 'no_op')
                    """
                ),
                {"account_id": account_id, "strategy_id": strategy_id},
            ).scalar()
            ingest = session.execute(
                text(
                    """
                    SELECT status, last_complete_ingest_at
                    FROM public.account_ingest_state
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            ).mappings().first()
        except Exception:
            return {
                "work_state": "unknown",
                "exposure_state": "unknown",
                "unavailable": ["live_settlement"],
                "watermark": None,
            }
        finally:
            session.close()

        # Complete CURRENT account truth is required: a live book cannot be read
        # as flat (or as settled) unless the account's own ingest cycle finished
        # cleanly. The policy is the platform's existing one (``account_truth``):
        # only ``idle`` means a completed cycle, so ``refreshing``, ``stale``,
        # an unknown/unrecognised status, or a missing completion timestamp are
        # all named unavailability rather than a flat reading.
        if ingest is None:
            unavailable.append("live_account_ingest_state")
        else:
            status = str(ingest["status"] or "")
            if ingest["last_complete_ingest_at"] is None:
                unavailable.append("live_account_ingest_incomplete")
            elif status != INGEST_IDLE:
                unavailable.append(f"live_account_ingest_{status or 'unknown'}")

        if publication is None:
            # Never published is UNKNOWN, not flat: there is no attributed book
            # to be flat ON.
            unavailable.append("live_attribution_unpublished")
        truth_complete = not any(
            reason.startswith("live_account_ingest") for reason in unavailable
        )
        exposure_state = "unknown"
        if publication is not None and truth_complete:
            exposure_state = "flat"
            for row in positions:
                quantity = _to_int_checked(row[0])
                if quantity is None:
                    exposure_state = "unknown"
                    unavailable.append("live_attribution_quantities")
                    break
                if quantity != 0:
                    exposure_state = "open"
                    break

        steps = _to_int_checked(unresolved_steps)
        if steps is None:
            work_state = "unknown"
            unavailable.append("live_plan_submissions")
        elif steps > 0:
            work_state = "outstanding"
        elif publication is None or not truth_complete:
            # No attributed book and no unresolved step: nothing to settle, but
            # only claim "settled" when the book itself is readable AND the
            # account's truth is complete and current.
            work_state = "unknown"
        else:
            work_state = "settled"

        watermark = ":".join(
            [
                "live",
                str(publication[0] if publication is not None else "unpublished"),
                str(steps if steps is not None else "unknown"),
                str(len(positions)),
            ]
        )
        return {
            "work_state": work_state,
            "exposure_state": exposure_state,
            "unavailable": sorted(set(unavailable)),
            "watermark": watermark,
        }

    async def _collect(self, job) -> ReconciliationEvidence:
        unavailable: List[str] = []
        notes: List[str] = []
        launched = job.handoff_at is not None
        trade_capable, cap_notes = self._capabilities(job)
        notes.extend(cap_notes)
        quiescence_state = "unverified"
        if trade_capable:
            # D-4: quiescence is barrier-backed now — ``verified`` only when a
            # valid proof covers this job's strategy book at collection time.
            quiescence_state = barrier_quiescence_state(
                account_id=str(job.account_scope or ""),
                strategy_id=str(job.strategy_id or "") or None,
                execution_environment=str(job.execution_mode or ""),
                barrier=self._settlement_barrier,
            )
            if quiescence_state == "verified":
                notes.append("execution_settlement_barrier_quiescence_verified")
            else:
                # Name the barrier's actual state rather than a legacy label: the
                # blocker is "no valid proof covering this book's current
                # version", and the operator reconciliation path is what records
                # one (see the reconcile route).
                barrier_state = None
                try:
                    from backend.strategies.settlement import ExecutionBarrier

                    barrier = self._settlement_barrier or ExecutionBarrier()
                    barrier_state = barrier.state(
                        account_id=str(job.account_scope or ""),
                        strategy_id=str(job.strategy_id or ""),
                        execution_environment=str(job.execution_mode or ""),
                    )
                except Exception:  # noqa: BLE001 - unreadable stays unverified
                    barrier_state = None
                notes.append("execution_settlement_barrier_proof_missing_or_invalid")
                notes.append(f"settlement_barrier_state={barrier_state}")

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
            quiescence_state=quiescence_state,
        )


def _option_status_blocks_trading(status: str) -> bool:
    try:
        from backend.api.services.safety import option_run_status_blocks_trading

        return bool(option_run_status_blocks_trading(status))
    except Exception:  # pragma: no cover - defensive
        return True
