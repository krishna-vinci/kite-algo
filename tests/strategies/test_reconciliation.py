"""Evidence classification + collector tests (no HTTP, no database).

The pure `assess` function is exercised across the four explicit cases and every
evidence axis; the connector is then driven with fake worker/paper services to
show how persisted state maps to evidence (including unavailable sources).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.strategies import service as strategy_service
from backend.strategies.reconciliation import (
    BLOCK_AUTHORITY_ACTIVE,
    BLOCK_EVIDENCE_UNAVAILABLE,
    BLOCK_JOB_ACTIVE,
    BLOCK_OPEN_EXPOSURE,
    BLOCK_OUTSTANDING_WORK,
    BLOCK_PROCESS_CLEANUP_UNKNOWN,
    BLOCK_PROCESS_CLEANUP_UNRESOLVED,
    BLOCK_WORK_UNKNOWN,
    CASE_BLOCKED,
    CASE_DATA_ONLY_COMPLETED,
    CASE_TRADING_SETTLED_FLAT,
    CASE_UNLAUNCHED,
    ReconciliationEvidence,
    assess,
)
from backend.strategies.reconciliation_service import ReconciliationEvidenceCollector


def _ev(**overrides) -> ReconciliationEvidence:
    base = dict(
        job_id="hsj_1",
        strategy_id="hs_1",
        attempt=1,
        replacement_blocked=True,
        job_status="recovery_required",
        launched=True,
        trade_capable=True,
        process_cleanup_state="confirmed",
        authority_state="revoked",
        work_state="settled",
        exposure_state="flat",
        protection_state="settled",
    )
    base.update(overrides)
    return ReconciliationEvidence(**base)


# ---------------------------------------------------------------------------
# pure assessment
# ---------------------------------------------------------------------------


def test_case_unlaunched_allowed():
    result = assess(_ev(launched=False, work_state="none", exposure_state="not_applicable", process_cleanup_state=None))
    assert result.allowed and result.case == CASE_UNLAUNCHED


def test_case_data_only_completed_allowed():
    result = assess(_ev(trade_capable=False, exposure_state="not_applicable"))
    assert result.allowed and result.case == CASE_DATA_ONLY_COMPLETED


def test_case_trading_settled_flat_allowed():
    result = assess(_ev())
    assert result.allowed and result.case == CASE_TRADING_SETTLED_FLAT


def test_open_exposure_blocked():
    result = assess(_ev(exposure_state="open"))
    assert not result.allowed and BLOCK_OPEN_EXPOSURE in result.blocking_reasons


def test_outstanding_work_blocked():
    result = assess(_ev(work_state="outstanding"))
    assert not result.allowed and BLOCK_OUTSTANDING_WORK in result.blocking_reasons


def test_unknown_work_blocked():
    result = assess(_ev(work_state="unknown"))
    assert not result.allowed and BLOCK_WORK_UNKNOWN in result.blocking_reasons


def test_cleanup_unknown_and_unresolved_blocked():
    assert assess(_ev(process_cleanup_state=None)).reason_code == BLOCK_PROCESS_CLEANUP_UNKNOWN
    assert assess(_ev(process_cleanup_state="unresolved")).reason_code == BLOCK_PROCESS_CLEANUP_UNRESOLVED


def test_authority_active_blocked():
    result = assess(_ev(authority_state="active"))
    assert not result.allowed and BLOCK_AUTHORITY_ACTIVE in result.blocking_reasons


def test_unavailable_evidence_blocked_and_wins():
    result = assess(_ev(unavailable=["paper_runtime"], evidence_complete=False))
    assert not result.allowed and result.reason_code == BLOCK_EVIDENCE_UNAVAILABLE


def test_active_job_not_a_reconciliation_target():
    result = assess(_ev(job_status="running"))
    assert not result.allowed and result.reason_code == BLOCK_JOB_ACTIVE


def test_not_blocked_when_terminal():
    result = assess(_ev(job_status="stopped", replacement_blocked=False))
    assert not result.allowed and result.reason_code == "HOSTED_JOB_NOT_BLOCKED"


def test_terminal_label_alone_does_not_unblock():
    """A terminal status with unknown cleanup/work must still be blocked."""
    result = assess(_ev(job_status="recovery_required", process_cleanup_state=None, work_state="unknown"))
    assert not result.allowed


# ---------------------------------------------------------------------------
# collector (fake worker + paper services)
# ---------------------------------------------------------------------------


class FakeWorker:
    def __init__(self, *, run=None, token_status=None, live_positions=None):
        self.run = run
        self.token_status = token_status
        self.live_positions = live_positions or []
        self.raise_run = False
        self.raise_token = False

    async def get_run(self, run_id):
        if self.raise_run:
            raise RuntimeError("run store down")
        return self.run

    async def get_token_status(self, token_id):
        if self.raise_token:
            raise RuntimeError("token store down")
        return self.token_status

    async def list_live_strategy_broker_positions(self, *, strategy_run_id, account_id):
        return list(self.live_positions)


class FakePaper:
    """Production-shaped read-only settlement view (no ensure_account)."""

    def __init__(self, payload=None, *, raise_error=False):
        self.payload = payload
        self.raise_error = raise_error
        self.calls = 0

    async def get_strategy_run_settlement_readonly(self, account_scope, strategy_run_id):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("paper down")
        return self.payload


def _settlement(*, run_state, order_count=0, pending_order_count=0, account="kite:paper", run_id="run_1"):
    return {
        "account_scope": account,
        "strategy_run_id": run_id,
        "run_state": run_state,
        "order_count": order_count,
        "pending_order_count": pending_order_count,
    }


class _Missing:
    pass


_MISSING = _Missing()


def _run_state(positions, *, run_id="run_1", is_stale=False, last_event_at="2026-09-15T10:00:00+00:00"):
    open_qty = 0
    if isinstance(positions, list):
        for position in positions:
            if isinstance(position, dict):
                try:
                    open_qty += abs(int(position.get("net_quantity") or 0))
                except (TypeError, ValueError):
                    pass
    state = {
        "strategy_run_id": run_id,
        "strategy_id": run_id,
        "is_stale": is_stale,
        "status": "open" if open_qty else "closed",
        "last_event_at": last_event_at,
    }
    if positions is not _MISSING:
        state["positions"] = positions
    return state


def _job(**overrides):
    base = dict(
        id="hsj_1",
        strategy_id="hs_1",
        attempt=1,
        status="recovery_required",
        desired_state="started",
        execution_mode="paper",
        account_scope="kite:paper",
        run_id="run_1",
        token_id="worker_1",
        handoff_at=datetime.now(timezone.utc),
        reconciled_at=None,
        process_cleanup_state="confirmed",
        process_cleanup_at=datetime.now(timezone.utc),
        process_cleanup_actor="sup-1",
        capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=True),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _collector(worker, paper):
    return ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=paper)


@pytest.mark.asyncio
async def test_collector_trading_settled_flat_allows():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    paper = FakePaper(_settlement(run_state=_run_state([{"net_quantity": 0}]), order_count=2))
    evidence = await _collector(worker, paper).collect(job)
    assert evidence.work_state == "settled"
    assert evidence.exposure_state == "flat"
    assert evidence.evidence_complete
    assert assess(evidence).allowed
    # Read-only path used; no ensure_account-style PnL call.
    assert paper.calls == 1


@pytest.mark.asyncio
async def test_collector_nested_positions_nonzero_stays_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    paper = FakePaper(_settlement(run_state=_run_state([{"net_quantity": 75}]), order_count=2))
    evidence = await _collector(worker, paper).collect(job)
    assert evidence.exposure_state == "open"
    result = assess(evidence)
    assert not result.allowed and BLOCK_OPEN_EXPOSURE in result.blocking_reasons


@pytest.mark.asyncio
async def test_collector_none_and_missing_and_malformed_stay_unknown():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")

    # None payload (blank run id) → unknown, not flat.
    evidence = await _collector(worker, FakePaper(None)).collect(job)
    assert evidence.exposure_state == "unknown" and not assess(evidence).allowed

    # run_state missing but orders exist → unknown.
    evidence = await _collector(worker, FakePaper(_settlement(run_state=None, order_count=3))).collect(job)
    assert evidence.exposure_state == "unknown" and "paper_run_state" in evidence.unavailable

    # positions key missing → unknown.
    evidence = await _collector(worker, FakePaper(_settlement(run_state=_run_state(_MISSING), order_count=1))).collect(job)
    assert evidence.exposure_state == "unknown" and "paper_positions" in evidence.unavailable

    # malformed quantity → unknown.
    bad = _settlement(run_state=_run_state([{"net_quantity": "not-a-number"}]), order_count=1)
    evidence = await _collector(worker, FakePaper(bad)).collect(job)
    assert evidence.exposure_state == "unknown" and not assess(evidence).allowed


@pytest.mark.asyncio
async def test_collector_confirmed_empty_run_state_is_flat_not_unknown():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    paper = FakePaper(_settlement(run_state=None, order_count=0))
    evidence = await _collector(worker, paper).collect(job)
    assert evidence.work_state == "none"
    assert evidence.exposure_state == "flat"
    assert evidence.evidence_complete
    assert assess(evidence).allowed


@pytest.mark.asyncio
async def test_collector_closed_run_with_pending_work_stays_blocked():
    job = _job()
    # Worker status is closed, but authoritative order evidence shows work pending.
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    paper = FakePaper(_settlement(run_state=_run_state([{"net_quantity": 0}]), order_count=2, pending_order_count=1))
    evidence = await _collector(worker, paper).collect(job)
    assert evidence.work_state == "outstanding"
    assert BLOCK_OUTSTANDING_WORK in assess(evidence).blocking_reasons


@pytest.mark.asyncio
async def test_collector_unavailable_paper_is_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    evidence = await _collector(worker, None).collect(job)
    assert "paper_settlement" in evidence.unavailable
    assert evidence.exposure_state == "unknown"
    assert assess(evidence).reason_code == BLOCK_EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_collector_attribution_mismatch_is_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    paper = FakePaper(_settlement(run_state=_run_state([], run_id="run_OTHER"), order_count=1))
    evidence = await _collector(worker, paper).collect(job)
    assert "paper_run_state_attribution" in evidence.unavailable
    assert not assess(evidence).allowed


@pytest.mark.asyncio
async def test_collector_authority_active_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="active")
    paper = FakePaper(_settlement(run_state=_run_state([{"net_quantity": 0}]), order_count=1))
    evidence = await _collector(worker, paper).collect(job)
    assert evidence.authority_state == "active"
    assert BLOCK_AUTHORITY_ACTIVE in assess(evidence).blocking_reasons


@pytest.mark.asyncio
async def test_data_only_completion_does_not_require_closed_run():
    job = _job(
        handoff_at=None,
        run_id="run_1",
        token_id=None,
        capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=False),
    )
    # Trading worker run is still open, but data-only has no trading work.
    worker = FakeWorker(run={"status": "open", "runtime_state": {}}, token_status=None)
    evidence = await _collector(worker, None).collect(job)
    assert evidence.trade_capable is False
    assert evidence.work_state == "none"
    assert evidence.exposure_state == "not_applicable"
    result = assess(evidence)
    # Unlaunched (no handoff) is case 1; a launched data-only attempt is case 2.
    assert result.case in {CASE_UNLAUNCHED, CASE_DATA_ONLY_COMPLETED}
