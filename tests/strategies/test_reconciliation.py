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
    def __init__(self, pnl=None, *, raise_error=False):
        self.pnl = pnl
        self.raise_error = raise_error

    async def get_strategy_run_pnl(self, account_scope, run_id):
        if self.raise_error:
            raise RuntimeError("paper down")
        return self.pnl


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


@pytest.mark.asyncio
async def test_collector_trading_settled_flat():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    collector = ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=FakePaper(pnl={"positions": []}))
    evidence = await collector.collect(job)
    assert evidence.trade_capable and evidence.launched
    assert evidence.work_state == "settled"
    assert evidence.exposure_state == "flat"
    assert evidence.authority_state == "revoked"
    assert evidence.evidence_complete
    assert assess(evidence).allowed


@pytest.mark.asyncio
async def test_collector_open_exposure_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    paper = FakePaper(pnl={"positions": [{"net_quantity": 75}]})
    evidence = await ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=paper).collect(job)
    assert evidence.exposure_state == "open"
    result = assess(evidence)
    assert not result.allowed and BLOCK_OPEN_EXPOSURE in result.blocking_reasons


@pytest.mark.asyncio
async def test_collector_outstanding_work_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "open", "runtime_state": {}}, token_status="revoked")
    evidence = await ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=FakePaper(pnl={"positions": []})).collect(job)
    assert evidence.work_state == "outstanding"
    assert BLOCK_OUTSTANDING_WORK in assess(evidence).blocking_reasons


@pytest.mark.asyncio
async def test_collector_unavailable_paper_is_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="revoked")
    evidence = await ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=None).collect(job)
    assert "paper_runtime" in evidence.unavailable
    assert evidence.exposure_state == "unknown"
    assert assess(evidence).reason_code == BLOCK_EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_collector_authority_active_blocked():
    job = _job()
    worker = FakeWorker(run={"status": "closed", "runtime_state": {}}, token_status="active")
    evidence = await ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=FakePaper(pnl={"positions": []})).collect(job)
    assert evidence.authority_state == "active"
    assert BLOCK_AUTHORITY_ACTIVE in assess(evidence).blocking_reasons


@pytest.mark.asyncio
async def test_collector_unlaunched_data_only():
    job = _job(
        handoff_at=None,
        run_id=None,
        token_id=None,
        capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=False),
    )
    worker = FakeWorker()
    evidence = await ReconciliationEvidenceCollector(worker_repo=worker, paper_runtime=None).collect(job)
    assert not evidence.launched
    assert evidence.work_state == "none"
    assert assess(evidence).case == CASE_UNLAUNCHED
