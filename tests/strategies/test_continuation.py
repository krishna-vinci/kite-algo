"""Evaluation continuation: the distinct verdict for a held book.

These are pure assessments plus the automatic-service decision path driven with
fake collaborators. They pin the invariants the contract names explicitly: an
open book is ``held`` and NEVER flat/settled, only a clean finite exit may clear
a block, and a missing or unknown axis always refuses.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.strategies.continuation import (
    CASE_CONTINUATION_BLOCKED,
    CASE_CONTINUATION_ELIGIBLE,
    COMPLETION_EXITED,
    COMPLETION_STOP_REQUESTED,
    COMPLETION_TIMEOUT,
    COMPLETION_UNKNOWN,
    CONTINUATION_APPROVAL_OUTSTANDING,
    CONTINUATION_AUTHORITY_ACTIVE,
    CONTINUATION_AUTHORITY_UNCERTAIN,
    CONTINUATION_BOOK_NOT_PUBLISHED,
    CONTINUATION_BOOK_UNREADABLE,
    CONTINUATION_ELIGIBLE,
    CONTINUATION_EVIDENCE_UNAVAILABLE,
    CONTINUATION_EXPOSURE_UNKNOWN,
    CONTINUATION_NOT_BLOCKED,
    CONTINUATION_NOT_FINITE,
    CONTINUATION_NOT_NORMAL_COMPLETION,
    CONTINUATION_NOT_TRADE_CAPABLE,
    CONTINUATION_PROCESS_CLEANUP_UNKNOWN,
    CONTINUATION_PROCESS_CLEANUP_UNRESOLVED,
    CONTINUATION_PROTECTION_IN_FLIGHT,
    CONTINUATION_PROTECTION_UNKNOWN,
    CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED,
    CONTINUATION_QUIESCENCE_UNVERIFIED,
    CONTINUATION_RECONCILIATION_DIVERGENCE,
    CONTINUATION_RECOVERY_ACTION_PENDING,
    CONTINUATION_WORK_OUTSTANDING,
    CONTINUATION_WORK_UNKNOWN,
    ContinuationEvidence,
    ContinuationService,
    assess_continuation,
    continuation_digest,
)


def _ev(**overrides) -> ContinuationEvidence:
    base = dict(
        job_id="hsj_1",
        strategy_id="hs_1",
        owner_id="app:o",
        account_id="kite:A",
        execution_environment="paper",
        attempt=1,
        lease_epoch=3,
        run_id="run-1",
        job_kind="finite",
        job_status="recovery_required",
        reconciled=False,
        desired_state="started",
        barrier_version=7,
        projection_version=4,
        completion_state=COMPLETION_EXITED,
        exit_code=0,
        trade_capable=True,
        process_cleanup_state="confirmed",
        authority_state="revoked",
        work_state="settled",
        exposure_state="open",
        protection_state="settled",
        recovery_action_required=False,
        quiescence_state="verified",
        book_state="published",
        divergence_state="none",
        approval_state="none",
    )
    base.update(overrides)
    return ContinuationEvidence(**base)


def test_an_open_book_is_held_and_eligible():
    result = assess_continuation(_ev(exposure_state="open"))
    assert result.allowed is True
    assert result.case == CASE_CONTINUATION_ELIGIBLE
    assert result.reason_code == CONTINUATION_ELIGIBLE
    assert result.held is True


def test_a_flat_book_is_eligible_but_not_held():
    result = assess_continuation(_ev(exposure_state="flat"))
    assert result.allowed is True
    assert result.held is False


def test_the_case_vocabulary_is_not_settlement():
    verdict = assess_continuation(_ev(exposure_state="open"))
    assert verdict.case not in ("trading_settled_flat", "settled", "unsettled")
    assert verdict.proof["held"] is True
    assert verdict.proof["exposure_state"] == "open"
    assert verdict.proof["owner_id"] == "app:o"
    assert verdict.proof["predecessor"]["attempt"] == 1
    assert verdict.proof["predecessor"]["lease_epoch"] == 3
    assert verdict.proof["barrier_version"] == 7
    assert verdict.proof["projection_version"] == 4


def test_a_stop_requested_attempt_is_never_cleared():
    result = assess_continuation(_ev(completion_state=COMPLETION_STOP_REQUESTED))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_NORMAL_COMPLETION


def test_a_timed_out_attempt_is_never_cleared():
    result = assess_continuation(_ev(completion_state=COMPLETION_TIMEOUT))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_NORMAL_COMPLETION


def test_an_unreported_completion_is_never_cleared():
    result = assess_continuation(_ev(completion_state=COMPLETION_UNKNOWN))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_NORMAL_COMPLETION


def test_a_non_zero_exit_is_never_cleared():
    """A crashed or signalled child is not a finished finite evaluation."""
    for code in (1, 2, -15, None):
        with_subtest = assess_continuation(_ev(exit_code=code))
        assert with_subtest.allowed is False, code
        assert with_subtest.reason_code == CONTINUATION_NOT_NORMAL_COMPLETION, code


def test_a_stop_race_is_never_cleared_even_with_a_clean_exit_code():
    """The operator asked this attempt to stop, so its exit 0 is not unattended."""
    result = assess_continuation(_ev(desired_state="stopped"))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_NORMAL_COMPLETION


def test_a_continuous_job_is_not_a_finite_evaluation():
    result = assess_continuation(_ev(job_kind="continuous"))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_FINITE


def test_an_active_job_is_not_a_continuation_target():
    result = assess_continuation(_ev(job_status="running"))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_BLOCKED


def test_an_already_reconciled_attempt_is_not_a_target():
    result = assess_continuation(_ev(reconciled=True))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_BLOCKED


def test_a_data_only_attempt_holds_no_book():
    result = assess_continuation(_ev(trade_capable=False))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_NOT_TRADE_CAPABLE


def test_unavailable_evidence_refuses():
    result = assess_continuation(_ev(unavailable=["paper_settlement"]))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_EVIDENCE_UNAVAILABLE


def test_process_cleanup_unknown_refuses():
    result = assess_continuation(_ev(process_cleanup_state=None))
    assert result.reason_code == CONTINUATION_PROCESS_CLEANUP_UNKNOWN


def test_process_cleanup_unresolved_refuses():
    result = assess_continuation(_ev(process_cleanup_state="unresolved"))
    assert result.reason_code == CONTINUATION_PROCESS_CLEANUP_UNRESOLVED


def test_active_authority_refuses():
    result = assess_continuation(_ev(authority_state="active"))
    assert result.reason_code == CONTINUATION_AUTHORITY_ACTIVE


def test_uncertain_authority_refuses():
    result = assess_continuation(_ev(authority_state="uncertain"))
    assert result.reason_code == CONTINUATION_AUTHORITY_UNCERTAIN


def test_outstanding_work_refuses():
    result = assess_continuation(_ev(work_state="outstanding"))
    assert result.reason_code == CONTINUATION_WORK_OUTSTANDING


def test_unknown_work_refuses():
    result = assess_continuation(_ev(work_state="unknown"))
    assert result.reason_code == CONTINUATION_WORK_UNKNOWN


def test_recovery_action_pending_refuses():
    result = assess_continuation(_ev(recovery_action_required=True))
    assert result.reason_code == CONTINUATION_RECOVERY_ACTION_PENDING


def test_an_in_flight_protective_exit_refuses():
    result = assess_continuation(_ev(protection_state="active"))
    assert result.reason_code == CONTINUATION_PROTECTION_IN_FLIGHT


def test_unknown_protection_ownership_refuses():
    result = assess_continuation(_ev(protection_state="unknown"))
    assert result.reason_code == CONTINUATION_PROTECTION_UNKNOWN


def test_a_standing_protection_policy_refuses_by_name():
    """Protection ownership is run-scoped, so a protected book has no safe
    automatic handover: it is refused by name, never cleared by disabling
    protection."""
    result = assess_continuation(_ev(protection_enabled=True))
    assert result.allowed is False
    assert result.reason_code == CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED


def test_outstanding_approval_refuses():
    result = assess_continuation(_ev(approval_state="outstanding"))
    assert result.reason_code == CONTINUATION_APPROVAL_OUTSTANDING


def test_unknown_approval_state_refuses():
    result = assess_continuation(_ev(approval_state="unknown"))
    assert result.reason_code == CONTINUATION_APPROVAL_OUTSTANDING


def test_reconciliation_divergence_refuses():
    result = assess_continuation(_ev(divergence_state="divergence"))
    assert result.reason_code == CONTINUATION_RECONCILIATION_DIVERGENCE


def test_an_unreadable_barrier_refuses():
    """No barrier version means no proof can be pinned, so nothing is cleared.

    Whether a proof is CURRENT is decided by the service (it records one under
    the book lock and re-validates the version inside the unblock transaction);
    the pure assessment only requires that the barrier is readable at all.
    """
    result = assess_continuation(_ev(quiescence_state="unverified", barrier_version=None))
    assert result.reason_code == CONTINUATION_QUIESCENCE_UNVERIFIED


def test_a_recorded_proof_is_not_required_for_the_assessment_to_pass():
    """The proof is recorded AFTER the assessment, so requiring one here would
    make continuations impossible."""
    result = assess_continuation(_ev(quiescence_state="unverified", barrier_version=7))
    assert result.allowed is True


def test_an_unreadable_book_refuses():
    result = assess_continuation(_ev(book_state="unreadable", projection_version=None))
    assert result.reason_code == CONTINUATION_BOOK_UNREADABLE


def test_an_unpublished_book_is_not_flat():
    result = assess_continuation(
        _ev(book_state="not_published", projection_version=None, exposure_state="unknown")
    )
    assert result.reason_code == CONTINUATION_BOOK_NOT_PUBLISHED


def test_unknown_exposure_refuses():
    result = assess_continuation(_ev(exposure_state="unknown"))
    assert result.reason_code == CONTINUATION_EXPOSURE_UNKNOWN


def test_the_digest_moves_with_the_axes():
    base = continuation_digest(_ev())
    assert continuation_digest(_ev()) == base
    assert continuation_digest(_ev(barrier_version=8)) != base
    assert continuation_digest(_ev(projection_version=5)) != base
    assert continuation_digest(_ev(exposure_state="flat")) != base


class _FakeBarrier:
    def __init__(self, *, proof_valid=True, version=7):
        self.proof_valid = proof_valid
        self.version = version
        self.proof_calls = 0

    def state(self, **_):
        return {"proof_valid": self.proof_valid, "barrier_version": self.version}

    def record_proof(self, **_):
        self.proof_calls += 1
        self.proof_valid = True
        return SimpleNamespace(recorded=True, reason="quiescent", barrier_version=self.version)


class _FakeCollector:
    def __init__(self, evidence):
        self._evidence = evidence
        self.calls = 0

    def collect(self, job, *, completion_state=COMPLETION_UNKNOWN):
        self.calls += 1
        return self._evidence


class _FakeRepo:
    def __init__(self, job, *, continue_result=None):
        self._job = job
        self._continue_result = continue_result or SimpleNamespace(id="audit-1")
        self.audits = []
        self.continue_calls = []

    def get_blocking_job(self, owner_id, strategy_id):
        return self._job

    def record_reconciliation(self, **kwargs):
        self.audits.append(kwargs)
        return SimpleNamespace(id=f"blocked-{len(self.audits)}")

    def reconcile_with_audit(self, job_id, **kwargs):
        self.continue_calls.append(kwargs)
        return self._continue_result


def _job(**overrides):
    base = dict(
        id="hsj_1",
        owner_id="app:o",
        strategy_id="hs_1",
        attempt=1,
        lease_epoch=3,
        run_id="run-1",
        status="recovery_required",
        reconciled_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_service_clears_an_eligible_predecessor_with_a_continuation_audit():
    job = _job()
    repo = _FakeRepo(job)
    barrier = _FakeBarrier()
    service = ContinuationService(
        session_factory=lambda: None,
        repository=repo,
        barrier=barrier,
        collector=_FakeCollector(_ev()),
    )
    result = service.attempt(owner_id="app:o", strategy_id="hs_1", completion_state=COMPLETION_EXITED)
    assert result["continued"] is True
    assert result["held"] is True
    assert result["reason_code"] == CONTINUATION_ELIGIBLE
    assert result["audit_id"] == "audit-1"
    assert len(repo.continue_calls) == 1
    call = repo.continue_calls[0]
    assert call["outcome"] == "continuation"
    assert call["require_barrier_proof"] is True
    assert call["expected_barrier_version"] == 7
    assert call["expected_projection_version"] == 4
    assert barrier.proof_calls == 0


def test_service_refuses_and_audits_a_normal_exit_that_is_not_eligible():
    job = _job()
    repo = _FakeRepo(job)
    service = ContinuationService(
        session_factory=lambda: None,
        repository=repo,
        barrier=_FakeBarrier(),
        collector=_FakeCollector(_ev(exposure_state="unknown")),
    )
    result = service.attempt(owner_id="app:o", strategy_id="hs_1", completion_state=COMPLETION_EXITED)
    assert result["continued"] is False
    assert result["reason_code"] == CONTINUATION_EXPOSURE_UNKNOWN
    assert len(repo.continue_calls) == 0
    assert repo.audits and repo.audits[0]["outcome"] == "blocked"
    assert repo.audits[0]["reason_code"] == CONTINUATION_EXPOSURE_UNKNOWN


def test_service_writes_nothing_for_an_ineligible_shape():
    job = _job(status="running")
    repo = _FakeRepo(job)
    service = ContinuationService(
        session_factory=lambda: None,
        repository=repo,
        barrier=_FakeBarrier(),
        collector=_FakeCollector(_ev()),
    )
    result = service.attempt(owner_id="app:o", strategy_id="hs_1", completion_state=COMPLETION_EXITED)
    assert result["attempted"] is False
    assert result["reason_code"] == CONTINUATION_NOT_BLOCKED
    assert repo.audits == []


def test_service_records_a_proof_when_none_is_current():
    job = _job()
    repo = _FakeRepo(job)
    barrier = _FakeBarrier(proof_valid=False)
    service = ContinuationService(
        session_factory=lambda: None,
        repository=repo,
        barrier=barrier,
        collector=_FakeCollector(_ev()),
    )
    result = service.attempt(owner_id="app:o", strategy_id="hs_1", completion_state=COMPLETION_EXITED)
    assert result["continued"] is True
    assert barrier.proof_calls == 1


def test_service_refuses_a_protected_attempt_and_never_disables_protection():
    job = _job()
    repo = _FakeRepo(job)
    service = ContinuationService(
        session_factory=lambda: None,
        repository=repo,
        barrier=_FakeBarrier(),
        collector=_FakeCollector(_ev(protection_enabled=True)),
    )
    result = service.attempt(owner_id="app:o", strategy_id="hs_1", completion_state=COMPLETION_EXITED)
    assert result["continued"] is False
    assert result["reason_code"] == CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED
    assert repo.continue_calls == []
    assert repo.audits and repo.audits[0]["outcome"] == "blocked"


def test_service_closes_the_run_when_no_protection_is_installed():
    job = _job()
    repo = _FakeRepo(job)
    service = ContinuationService(
        session_factory=lambda: None,
        repository=repo,
        barrier=_FakeBarrier(),
        collector=_FakeCollector(_ev(protection_enabled=False)),
    )
    result = service.attempt(owner_id="app:o", strategy_id="hs_1", completion_state=COMPLETION_EXITED)
    assert result["continued"] is True
    assert repo.continue_calls[0]["close_worker_run"] is True
    assert repo.continue_calls[0]["worker_run_id"] == "run-1"
