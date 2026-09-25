"""B2.1b: the governed repair path for partial / cleanup option runs.

Pure, no database: the assessment is derived from the run's OWN confirmed fills
through the existing ``StagedStructureExit`` adapter, and the transition is the
same lifecycle the execution path uses. The route suite covers the HTTP surface.
"""

from __future__ import annotations

import pytest

from backend.options.execution.models import OptionRunState
from backend.options.execution.repair import (
    ACTION_CLOSE_FLAT,
    ACTION_CLOSE_RESIDUAL,
    REASON_ADJUST_IN_FLIGHT,
    REASON_AMBIGUOUS,
    REASON_EVIDENCE_CHANGED,
    REASON_NOT_REPAIRABLE,
    STATE_AMBIGUOUS,
    STATE_FLAT,
    STATE_NOT_REPAIRABLE,
    STATE_RESIDUAL,
    OptionRunRepairRefusal,
    OptionRunRepairService,
    assess_option_run_repair,
)
from backend.options.protection.staged_exit import StagedStructureExit


SHORT = "NIFTY26OCT25000CE"
HEDGE = "NIFTY26OCT30000CE"


def _no_session():
    raise AssertionError("the assessment must not open a session")


def _staged_exit() -> StagedStructureExit:
    return StagedStructureExit(session_factory=_no_session)


def _legs() -> list:
    return [
        {
            "leg_id": "leg_short",
            "tradingsymbol": SHORT,
            "transaction_type": "SELL",
            "quantity": 75,
            "exchange": "NFO",
            "product": "NRML",
        },
        {
            "leg_id": "leg_hedge",
            "tradingsymbol": HEDGE,
            "transaction_type": "BUY",
            "quantity": 75,
            "exchange": "NFO",
            "product": "NRML",
        },
    ]


def _trade(leg_id: str, side: str, quantity: int) -> dict:
    return {
        "leg_id": leg_id,
        "transaction_type": side,
        "quantity": quantity,
        "tradingsymbol": SHORT if leg_id == "leg_short" else HEDGE,
    }


def _run(status: str = "partial_entry", *, trades: list, orders: list | None = None) -> OptionRunState:
    return OptionRunState(
        strategy_run_id="opt_run_1",
        strategy_name="bull_put_spread",
        product="NRML",
        status=status,
        legs=_legs(),
        orders=list(orders or []),
        trades=list(trades),
        metadata={"worker_run_id": "worker_1", "account_id": "acc_1"},
    )


class _FakeRunStore:
    """The run store surface the repair service uses, in memory."""

    def __init__(self, run: OptionRunState) -> None:
        self.run = run

    def get_run(self, strategy_run_id: str) -> OptionRunState:
        if str(strategy_run_id) != self.run.strategy_run_id:
            raise KeyError(strategy_run_id)
        return self.run

    def save_run_if_status(self, run: OptionRunState, *, allowed_from, db=None) -> bool:
        if str(self.run.status) not in {str(value) for value in allowed_from}:
            return False
        self.run = run
        return True


def _service(
    run: OptionRunState, *, adjust_owner: dict | None = None
) -> tuple[OptionRunRepairService, _FakeRunStore]:
    store = _FakeRunStore(run)
    reader = None if adjust_owner is None else (lambda _run_id: dict(adjust_owner))
    return (
        OptionRunRepairService(
            run_store=store, staged_exit=_staged_exit(), adjust_owner_reader=reader
        ),
        store,
    )


def test_a_flat_partial_run_is_flat_and_closes_through_the_service():
    run = _run(
        "partial_entry",
        trades=[
            _trade("leg_short", "SELL", 75),
            _trade("leg_short", "BUY", 75),
            _trade("leg_hedge", "BUY", 75),
            _trade("leg_hedge", "SELL", 75),
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit())
    assert assessment["state"] == STATE_FLAT
    assert assessment["close_plan"] == []
    assert assessment["evidence_digest"]

    service, store = _service(run)
    next_run, planned = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_CLOSE_FLAT,
        evidence_digest=assessment["evidence_digest"],
    )
    assert planned["state"] == STATE_FLAT
    service.commit(next_run, allowed_from=str(run.status))
    assert store.run.status == "exited"
    # A second repair of the same run is refused: the run is no longer repairable.
    assert assess_option_run_repair(store.run, _staged_exit())["state"] == STATE_NOT_REPAIRABLE


def test_a_closed_flat_run_stops_blocking_a_new_equivalent_entry(monkeypatch):
    """The flat -> admissible twin: B2.1a refuses, the repair clears it."""
    from backend.options.execution import plan_binding
    from backend.strategies import execution_snapshot

    run = _run(
        "partial_entry",
        trades=[
            _trade("leg_short", "SELL", 75),
            _trade("leg_short", "BUY", 75),
            _trade("leg_hedge", "BUY", 75),
            _trade("leg_hedge", "SELL", 75),
        ],
    )
    plan = {
        "plan_id": "plan-new",
        "resolved_plan": {
            "target_kind": "option_structure",
            "structure_digest": "digest-new-structure",
            "option_run": {"phase": "entry", "option_run_id": None},
            "legs": [{"instrument_id": "i2", "side": "SELL", "broker_symbol": SHORT}],
        },
    }
    discovered = {
        "rows": [
            {
                "option_run_id": run.strategy_run_id,
                "status": "partial_entry",
                "structure_digest": "digest-old-structure",
                "legs": _legs(),
                "plan_ids": [],
                "originating_plan_id": None,
                "protective_exit_unresolved": False,
            }
        ]
    }

    def fake_option_runs_for_scope(self, **kwargs):
        return list(discovered["rows"]), {"coverage": "known", "count": len(discovered["rows"])}

    monkeypatch.setattr(
        execution_snapshot.OwnedWorkSnapshotService,
        "option_runs_for_scope",
        fake_option_runs_for_scope,
    )

    def entry_admissible() -> None:
        plan_binding.assess_option_entry_admissibility(
            plan,
            strategy_id="strategy-1",
            account_id="acc_1",
            execution_environment="paper",
            session=object(),
        )

    with pytest.raises(plan_binding.PlanBindingRefusal) as refusal:
        entry_admissible()
    assert refusal.value.reason_code == "OPTION_STRUCTURE_UNRESOLVED"

    service, store = _service(run)
    assessment = service.assessment(run.strategy_run_id)
    next_run, _planned = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_CLOSE_FLAT,
        evidence_digest=assessment["evidence_digest"],
    )
    service.commit(next_run, allowed_from=str(run.status))
    assert store.run.status == "exited"

    discovered["rows"][0]["status"] = store.run.status
    entry_admissible()  # no refusal: the repaired run no longer blocks a new entry


def test_a_residual_partial_run_plans_a_short_first_close_only():
    run = _run(
        "partial_entry",
        trades=[_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)],
    )
    assessment = assess_option_run_repair(run, _staged_exit())
    assert assessment["state"] == STATE_RESIDUAL
    assert [order["tradingsymbol"] for order in assessment["close_plan"]] == [SHORT]
    (close,) = assessment["close_plan"]
    # The short is closed by a BUY; the hedge is NOT released while the short is open.
    assert close["transaction_type"] == "BUY" and close["quantity"] == 75
    assert HEDGE not in {order["tradingsymbol"] for order in assessment["close_plan"]}

    service, store = _service(run)
    next_run, _planned = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_CLOSE_RESIDUAL,
        evidence_digest=assessment["evidence_digest"],
    )
    service.commit(next_run, allowed_from=str(run.status))
    assert store.run.status == "exiting"
    assert store.run.pending_legs == ["leg_short"]


def test_an_unresolved_protective_stage_is_ambiguous_and_refuses_by_name():
    run = _run(
        "partial_exit",
        trades=[_trade("leg_short", "SELL", 75)],
        orders=[
            {"stage_digest": "abcdef1234567890", "attempt": 1, "state": "sending", "legs": []}
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit())
    assert assessment["state"] == STATE_AMBIGUOUS
    assert assessment["reason_code"] == REASON_AMBIGUOUS
    assert "protective_stage_unresolved" in assessment["reasons"]

    service, _store = _service(run)
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.plan(
            option_run_id=run.strategy_run_id,
            action=ACTION_CLOSE_FLAT,
            evidence_digest=assessment["evidence_digest"],
        )
    assert refusal.value.reason_code == REASON_AMBIGUOUS
    assert refusal.value.status_code == 409


def test_a_fill_the_run_cannot_attribute_is_ambiguous():
    run = _run(
        "partial_exit",
        trades=[_trade("leg_short", "SELL", 75), _trade("leg_not_mine", "BUY", 75)],
    )
    assessment = assess_option_run_repair(run, _staged_exit())
    assert assessment["state"] == STATE_AMBIGUOUS
    assert "unattributable_trades" in assessment["reasons"]


def test_an_adjusting_run_is_repairable_once_its_owning_plan_finished():
    """A leg generation that stopped mid-flight is stranded work too."""
    run = _run(
        "adjusting",
        trades=[_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)],
    )
    owner = {"state": "finished", "plan_ids": ["plan-roll"], "plans": {}}
    assessment = assess_option_run_repair(run, _staged_exit(), adjust_owner=owner)
    assert assessment["state"] == STATE_RESIDUAL
    assert assessment["reasons"] == []
    assert assessment["reason_code"] is None
    assert [order["tradingsymbol"] for order in assessment["close_plan"]] == [SHORT]

    service, store = _service(run, adjust_owner=owner)
    planned, detail = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_CLOSE_RESIDUAL,
        evidence_digest=assessment["evidence_digest"],
    )
    assert detail["state"] == STATE_RESIDUAL
    service.commit(planned, allowed_from=str(run.status))
    assert store.run.status == "exiting"
    assert store.run.pending_legs == ["leg_short"]


def test_a_flat_adjusting_run_closes_flat_along_the_existing_edges():
    """``adjusting`` reaches ``exited`` through ``cleanup_required``, as repair does."""
    run = _run(
        "adjusting",
        trades=[
            _trade("leg_short", "SELL", 75),
            _trade("leg_short", "BUY", 75),
            _trade("leg_hedge", "BUY", 75),
            _trade("leg_hedge", "SELL", 75),
        ],
    )
    owner = {"state": "finished", "plan_ids": ["plan-roll"], "plans": {}}
    assessment = assess_option_run_repair(run, _staged_exit(), adjust_owner=owner)
    assert assessment["state"] == STATE_FLAT

    service, store = _service(run, adjust_owner=owner)
    planned, _detail = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_CLOSE_FLAT,
        evidence_digest=assessment["evidence_digest"],
    )
    service.commit(planned, allowed_from=str(run.status))
    assert store.run.status == "exited"


def test_an_adjusting_run_whose_plan_is_still_submitting_is_ambiguous():
    """Nothing is closed out from under a plan that may still be submitting."""
    run = _run("adjusting", trades=[_trade("leg_short", "SELL", 75)])
    submitting = {"state": "in_flight", "plan_ids": ["plan-roll"], "plans": {}}
    assessment = assess_option_run_repair(run, _staged_exit(), adjust_owner=submitting)
    assert assessment["state"] == STATE_AMBIGUOUS
    assert assessment["reason_code"] == REASON_AMBIGUOUS
    assert REASON_ADJUST_IN_FLIGHT in assessment["reasons"]

    service, store = _service(run, adjust_owner=submitting)
    inspection = service.assessment(run.strategy_run_id)
    assert REASON_ADJUST_IN_FLIGHT in inspection["reasons"]
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.plan(
            option_run_id=run.strategy_run_id,
            action=ACTION_CLOSE_RESIDUAL,
            evidence_digest=inspection["evidence_digest"],
        )
    assert refusal.value.reason_code == REASON_AMBIGUOUS
    assert refusal.value.status_code == 409
    assert store.run.status == "adjusting"

    # No reader at all is the same answer, never "stranded": the caller that
    # cannot read the owner is not told the run may be closed.
    unread = assess_option_run_repair(run, _staged_exit())
    assert unread["state"] == STATE_AMBIGUOUS
    assert REASON_ADJUST_IN_FLIGHT in unread["reasons"]


def test_a_non_repairable_status_refuses_and_does_not_change_the_run():
    run = _run("entered", trades=[_trade("leg_short", "SELL", 75)])
    assessment = assess_option_run_repair(run, _staged_exit())
    assert assessment["state"] == STATE_NOT_REPAIRABLE
    assert assessment["reason_code"] == REASON_NOT_REPAIRABLE

    service, store = _service(run)
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.plan(
            option_run_id=run.strategy_run_id,
            action=ACTION_CLOSE_FLAT,
            evidence_digest=assessment["evidence_digest"],
        )
    assert refusal.value.reason_code == REASON_NOT_REPAIRABLE
    assert store.run.status == "entered"


def test_a_changed_evidence_digest_refuses_before_anything_moves():
    trades = [_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)]
    run = _run("partial_entry", trades=trades)
    stale = assess_option_run_repair(run, _staged_exit())["evidence_digest"]

    # A fill lands: the residual moved, so the digest an operator read is stale.
    run.trades.append(_trade("leg_short", "BUY", 75))
    service, store = _service(run)
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.plan(
            option_run_id=run.strategy_run_id,
            action=ACTION_CLOSE_RESIDUAL,
            evidence_digest=stale,
        )
    assert refusal.value.reason_code == REASON_EVIDENCE_CHANGED
    assert store.run.status == "partial_entry"
