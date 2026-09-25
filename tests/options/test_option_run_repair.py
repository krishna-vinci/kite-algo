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
    ACTION_OWNER_EXIT,
    REASON_ADJUST_IN_FLIGHT,
    REASON_AMBIGUOUS,
    REASON_EVIDENCE_CHANGED,
    REASON_LEDGER_INCOMPLETE,
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


# --------------------------------------------------------------- rolled runs

ROLL_SHORT = "NIFTY26NOV25000CE"
ROLL_HEDGE = "NIFTY26NOV30000CE"


def _roll_leg(leg_id: str, symbol: str, side: str, quantity: int = 75) -> dict:
    return {
        "leg_id": leg_id,
        "tradingsymbol": symbol,
        "transaction_type": side,
        "quantity": quantity,
        "exchange": "NFO",
        "product": "NRML",
    }


def _roll_fill(leg_id: str, symbol: str, side: str, quantity: int = 75) -> dict:
    return {
        "leg_id": leg_id,
        "transaction_type": side,
        "quantity": quantity,
        "tradingsymbol": symbol,
    }


def _rolled_run(
    status: str = "entered",
    *,
    trades: list,
    released_legs: list | None = None,
    history: list | None = None,
) -> OptionRunState:
    """A run right AFTER a completed roll.

    The HELD legs are the new generation, and the generation the roll RELEASED
    is recorded in the run's own metadata (``structure_generation_history``) -
    exactly what the paper/live roll writers leave behind. The released
    generation's confirmed fills stay on the run's ledger under their OLD ids.
    """
    held = [
        _roll_leg("roll:1", ROLL_SHORT, "SELL"),
        _roll_leg("roll:2", ROLL_HEDGE, "BUY"),
    ]
    released = (
        released_legs
        if released_legs is not None
        else [_roll_leg("old:1", SHORT, "SELL"), _roll_leg("old:2", HEDGE, "BUY")]
    )
    recorded = (
        history
        if history is not None
        else [{"generation": 1, "structure_digest": "digest-old", "legs": released}]
    )
    return OptionRunState(
        strategy_run_id="opt_run_1",
        strategy_name="bull_put_spread",
        product="NRML",
        status=status,
        legs=held,
        orders=[],
        trades=list(trades),
        metadata={
            "worker_run_id": "worker_1",
            "account_id": "acc_1",
            "structure_generation": 2,
            "structure_digest": "digest-new",
            "structure_generation_history": recorded,
        },
    )


def _released_generation_fills() -> list:
    """The released generation's own ledger: opened and released, ZERO per leg."""
    return [
        _roll_fill("old:1", SHORT, "SELL"),
        _roll_fill("old:1", SHORT, "BUY"),
        _roll_fill("old:2", HEDGE, "BUY"),
        _roll_fill("old:2", HEDGE, "SELL"),
    ]


def test_a_rolled_run_whose_released_generation_nets_flat_is_not_ambiguous():
    """After a roll the released legs' fills are this run's OWN evidence."""
    run = _rolled_run(
        "entered",
        trades=[
            *_released_generation_fills(),
            _roll_fill("roll:1", ROLL_SHORT, "SELL"),
            _roll_fill("roll:2", ROLL_HEDGE, "BUY"),
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_RESIDUAL
    assert assessment["reason_code"] is None
    assert assessment["reasons"] == []
    assert assessment["unattributable_trades"] == []
    # The released generation nets to zero, so only the HELD short is planned -
    # short first, its hedge withheld until that short is proven gone.
    assert [
        (order["tradingsymbol"], order["transaction_type"], order["quantity"])
        for order in assessment["close_plan"]
    ] == [(ROLL_SHORT, "BUY", 75)]
    assert [row["reason"] for row in assessment["withheld_hedges"]] == [
        "short_not_proven_closed"
    ]
    # The released legs are still part of the run's own book, at zero.
    assert assessment["evidence"]["open_by_leg"]["old:1"] == 0
    assert assessment["evidence"]["open_by_leg"]["old:2"] == 0


def test_a_released_leg_that_does_not_net_flat_is_residual_exposure_in_the_plan():
    """A released leg still netting non-zero is real exposure, not a silent zero."""
    run = _rolled_run(
        "entered",
        # The old short was never released; everything else nets flat, so the
        # ONLY residual exposure is the OLD short.
        trades=[
            _roll_fill("old:1", SHORT, "SELL"),
            _roll_fill("old:2", HEDGE, "BUY"),
            _roll_fill("old:2", HEDGE, "SELL"),
            _roll_fill("roll:1", ROLL_SHORT, "SELL"),
            _roll_fill("roll:1", ROLL_SHORT, "BUY"),
            _roll_fill("roll:2", ROLL_HEDGE, "BUY"),
            _roll_fill("roll:2", ROLL_HEDGE, "SELL"),
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_RESIDUAL
    assert assessment["reasons"] == []
    assert assessment["evidence"]["open_by_leg"]["old:1"] == -75
    assert [
        (order["tradingsymbol"], order["transaction_type"], order["quantity"])
        for order in assessment["close_plan"]
    ] == [(SHORT, "BUY", 75)]

    # The plan names the OLD leg as the one being closed, so the staged exit can
    # attribute the covering fill back onto it.
    service, _store = _service(run)
    planned, _detail = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_OWNER_EXIT,
        evidence_digest=assessment["evidence_digest"],
        owner_exit=True,
    )
    assert planned.status == "exiting"
    assert planned.pending_legs == ["old:1"]


def test_a_leg_an_adjust_kept_is_planned_once_even_though_it_is_in_the_history():
    """A kept leg is in the held set AND the recorded generation: still ONE leg."""
    kept_short = _roll_leg("roll:1", ROLL_SHORT, "SELL")
    run = _rolled_run(
        "entered",
        released_legs=[kept_short, _roll_leg("old:2", HEDGE, "BUY")],
        trades=[
            _roll_fill("roll:1", ROLL_SHORT, "SELL"),
            _roll_fill("roll:2", ROLL_HEDGE, "BUY"),
            _roll_fill("old:2", HEDGE, "BUY"),
            _roll_fill("old:2", HEDGE, "SELL"),
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_RESIDUAL
    # The kept short is planned ONCE: its history entry must not double it into
    # a second covering order.
    assert [
        (order["tradingsymbol"], order["transaction_type"], order["quantity"])
        for order in assessment["close_plan"]
    ] == [(ROLL_SHORT, "BUY", 75)]

    service, _store = _service(run)
    planned, _detail = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_OWNER_EXIT,
        evidence_digest=assessment["evidence_digest"],
        owner_exit=True,
    )
    assert planned.pending_legs == ["roll:1"]


def test_a_trade_on_an_unknown_leg_stays_ambiguous_after_a_roll():
    run = _rolled_run(
        "entered",
        trades=[
            *_released_generation_fills(),
            _roll_fill("roll:1", ROLL_SHORT, "SELL"),
            _roll_fill("roll:2", ROLL_HEDGE, "BUY"),
            _roll_fill("leg_not_mine", SHORT, "BUY"),
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_AMBIGUOUS
    assert assessment["reason_code"] == REASON_AMBIGUOUS
    assert "unattributable_trades" in assessment["reasons"]
    assert [row["leg_id"] for row in assessment["unattributable_trades"]] == [
        "leg_not_mine"
    ]


def test_an_unreadable_leg_history_is_ambiguous():
    """A history the run cannot read is a refusal, never a smaller book."""
    run = _rolled_run("entered", trades=[_roll_fill("roll:1", ROLL_SHORT, "SELL")])
    run.metadata["structure_generation_history"] = "not-a-list"
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_AMBIGUOUS
    assert assessment["reason_code"] == REASON_AMBIGUOUS
    assert "unreadable_fills" in assessment["reasons"]
    assert "leg_history" in [row["stage"] for row in assessment["unreadable_fills"]]


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


def test_an_adjusting_run_names_the_unanswered_step_an_owner_must_dispose_of():
    """B2.6b: the ``adjust_in_flight`` refusal carries its own coordinates.

    The options UI cannot otherwise learn WHICH plan/step is unanswered, so an
    ``ambiguous`` assessment lists them: the plan ids and step numbers come from
    the plan-execution fold (`option_adjust_owner_state` -> 
    `option_plan_execution_state`), and each step's own trail row supplies the
    word and the order it links.
    """
    run = _run("adjusting", trades=[_trade("leg_short", "SELL", 75)])
    submitting = {
        "state": "in_flight",
        "plan_ids": ["plan-adjust"],
        "plans": {
            "plan-adjust": {
                "state": "in_flight",
                "evidence": {
                    "plan_id": "plan-adjust",
                    "submitted_events": 1,
                    "unresolved_steps": [1],
                },
            }
        },
    }
    seen: list = []

    def _reader(plan_id, step_no):
        seen.append((plan_id, step_no))
        return {"state": "submitted", "order_id": None}

    assessment = assess_option_run_repair(
        run, _staged_exit(), adjust_owner=submitting, unresolved_step_reader=_reader
    )
    assert assessment["state"] == STATE_AMBIGUOUS
    assert REASON_ADJUST_IN_FLIGHT in assessment["reasons"]
    assert assessment["unresolved_steps"] == [
        {"plan_id": "plan-adjust", "step_no": 1, "state": "submitted", "order_id": None}
    ]
    assert seen == [("plan-adjust", 1)]

    # The service path the route uses carries the same coordinates.
    service = OptionRunRepairService(
        run_store=_FakeRunStore(run),
        staged_exit=_staged_exit(),
        adjust_owner_reader=lambda _run_id: dict(submitting),
        unresolved_step_reader=_reader,
    )
    inspection = service.assessment(run.strategy_run_id)
    assert inspection["unresolved_steps"] == assessment["unresolved_steps"]

    # A plan the fold does not report as unfinished names no steps, and a caller
    # with no reader still gets the refusal - with no invented word.
    finished = {
        "state": "finished",
        "plan_ids": ["plan-adjust"],
        "plans": {"plan-adjust": {"state": "finished", "evidence": {"plan_id": "plan-adjust"}}},
    }
    assert (
        assess_option_run_repair(
            run, _staged_exit(), adjust_owner=finished, unresolved_step_reader=_reader
        )["unresolved_steps"]
        == []
    )
    assert assess_option_run_repair(run, _staged_exit(), adjust_owner=submitting)[
        "unresolved_steps"
    ] == [{"plan_id": "plan-adjust", "step_no": 1, "state": "", "order_id": None}]


def test_a_run_whose_plan_fill_remains_working_is_ambiguous():
    """A live remainder is in-flight work, never repairable residual evidence."""
    run = _run("adjusting", trades=[_trade("leg_short", "SELL", 75)])
    working = {
        "state": "in_flight",
        "plan_ids": ["plan-roll"],
        "plans": {"plan-roll": {"state": "in_flight", "evidence": {"unresolved_steps": [1]}}},
    }
    assessment = assess_option_run_repair(
        run, _staged_exit(), adjust_owner=working, ledger_consistent=True
    )
    assert assessment["state"] == STATE_AMBIGUOUS
    assert assessment["reason_code"] == REASON_AMBIGUOUS
    assert REASON_ADJUST_IN_FLIGHT in assessment["reasons"]


def test_a_ledger_trail_disagreement_is_ambiguous_as_ledger_incomplete():
    run = _run("adjusting", trades=[_trade("leg_short", "SELL", 75)])
    owner = {"state": "finished", "plan_ids": ["plan-roll"], "plans": {}}
    assessment = assess_option_run_repair(
        run, _staged_exit(), adjust_owner=owner, ledger_consistent=False
    )
    assert assessment["state"] == STATE_AMBIGUOUS
    assert assessment["reason_code"] == REASON_AMBIGUOUS
    assert REASON_LEDGER_INCOMPLETE in assessment["reasons"]

    service = OptionRunRepairService(
        run_store=_FakeRunStore(run),
        staged_exit=_staged_exit(),
        adjust_owner_reader=lambda _run_id: owner,
        ledger_consistency_reader=lambda _run: False,
    )
    inspection = service.assessment(run.strategy_run_id)
    assert REASON_LEDGER_INCOMPLETE in inspection["reasons"]


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


# ---------------------------------------------------------------------------
# B2.6b S2: the owner-authorized discretionary exit of ONE option run
# ---------------------------------------------------------------------------


def _exit_service(run: OptionRunState, *, adjust_owner: dict | None = None):
    return _service(run, adjust_owner=adjust_owner)


def _unresolved_stage() -> list:
    return [
        {
            "stage_digest": "abcdef1234567890",
            "attempt": 1,
            "state": "sending",
            "account_id": "acc_1",
            "legs": [],
        }
    ]


def test_an_entered_run_is_admitted_for_the_owner_exit_and_hedges_stay_withheld():
    """A clean ``entered`` run admits an exit; its hedge is NOT released yet."""
    run = _run(
        "entered",
        trades=[_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)],
    )
    # The repair path still refuses it: an exit is the owner exit's job.
    assert assess_option_run_repair(run, _staged_exit())["state"] == STATE_NOT_REPAIRABLE

    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_RESIDUAL
    assert assessment["evidence"]["shorts_proven_closed"] is False
    assert [
        (order["tradingsymbol"], order["transaction_type"], order["quantity"])
        for order in assessment["close_plan"]
    ] == [(SHORT, "BUY", 75)]

    service, store = _exit_service(run)
    next_run, planned = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_OWNER_EXIT,
        evidence_digest=assessment["evidence_digest"],
        owner_exit=True,
    )
    assert planned["state"] == STATE_RESIDUAL
    # The run takes the exiting state; it is NOT exited on a stage being accepted.
    assert next_run.status == "exiting"
    service.commit(next_run, allowed_from="entered")
    assert store.run.status == "exiting"
    assert store.run.pending_legs == ["leg_short"]


def test_a_proven_short_closure_admits_the_hedge_release():
    run = _run(
        "entered",
        trades=[
            _trade("leg_short", "SELL", 75),
            _trade("leg_short", "BUY", 75),
            _trade("leg_hedge", "BUY", 75),
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["evidence"]["shorts_proven_closed"] is True
    assert [
        (order["tradingsymbol"], order["transaction_type"], order["quantity"])
        for order in assessment["close_plan"]
    ] == [(HEDGE, "SELL", 75)]


def test_a_short_only_entered_run_admits_exactly_its_short_close():
    """No hedge to hold back: the plan is the short's close and nothing else."""
    run = _run(
        "entered",
        trades=[_trade("leg_short", "SELL", 75)],
    )
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_RESIDUAL
    assert assessment["withheld_hedges"] == []
    assert [
        (order["tradingsymbol"], order["transaction_type"], order["quantity"])
        for order in assessment["close_plan"]
    ] == [(SHORT, "BUY", 75)]


def test_a_flat_entered_run_completes_the_exit_as_exited():
    run = _run(
        "entered",
        trades=[
            _trade("leg_short", "SELL", 75),
            _trade("leg_short", "BUY", 75),
            _trade("leg_hedge", "BUY", 75),
            _trade("leg_hedge", "SELL", 75),
        ],
    )
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_FLAT
    service, store = _exit_service(run)
    next_run, _planned = service.plan(
        option_run_id=run.strategy_run_id,
        action=ACTION_OWNER_EXIT,
        evidence_digest=assessment["evidence_digest"],
        owner_exit=True,
    )
    assert next_run.status == "exited"
    service.commit(next_run, allowed_from="entered")
    assert store.run.status == "exited"
    # A terminal run reads COMPLETE, never "not repairable".
    terminal = assess_option_run_repair(store.run, _staged_exit(), owner_exit=True)
    assert terminal["state"] == STATE_FLAT


@pytest.mark.parametrize(
    "status,trades,adjust_owner,expected_reason",
    [
        (
            "entered",
            [_trade("leg_short", "SELL", 75)],
            {"state": "finished"},
            "OPTION_PROTECTIVE_EXIT_UNRESOLVED",
        ),
        (
            "adjusting",
            [_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)],
            {"state": "in_flight"},
            "OPTION_RUN_ADJUST_IN_FLIGHT",
        ),
        (
            "entered",
            [
                {"leg_id": "leg_ghost", "transaction_type": "BUY", "quantity": 75},
            ],
            {"state": "finished"},
            "OPTION_RUN_EVIDENCE_AMBIGUOUS",
        ),
    ],
)
def test_the_owner_exit_refuses_each_gate_by_its_own_name(
    status, trades, adjust_owner, expected_reason
):
    from backend.api.services.option_run_repair import owner_exit_refusal

    orders = _unresolved_stage() if expected_reason == "OPTION_PROTECTIVE_EXIT_UNRESOLVED" else None
    run = _run(status, trades=trades, orders=orders)
    service, store = _exit_service(run, adjust_owner=adjust_owner)
    assessment = assess_option_run_repair(
        run,
        _staged_exit(),
        adjust_owner=adjust_owner,
        owner_exit=True,
    )
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.plan(
            option_run_id=run.strategy_run_id,
            action=ACTION_OWNER_EXIT,
            evidence_digest=assessment["evidence_digest"],
            owner_exit=True,
        )
    assert owner_exit_refusal(refusal.value).reason_code == expected_reason
    assert store.run.status == status


def test_a_created_run_is_not_exitable_and_a_lost_cas_names_the_state_change():
    from backend.api.services.option_run_repair import owner_exit_refusal

    run = _run("created", trades=[])
    service, store = _exit_service(run)
    assessment = assess_option_run_repair(run, _staged_exit(), owner_exit=True)
    assert assessment["state"] == STATE_NOT_REPAIRABLE
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.plan(
            option_run_id=run.strategy_run_id,
            action=ACTION_OWNER_EXIT,
            evidence_digest=assessment["evidence_digest"],
            owner_exit=True,
        )
    assert owner_exit_refusal(refusal.value).reason_code == "OPTION_EXIT_BEFORE_ENTRY"

    # A run that MOVED between the plan and the commit loses the CAS by name.
    entered = _run(
        "entered",
        trades=[_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)],
    )
    service, store = _exit_service(entered)
    planned, _assessment = service.plan(
        option_run_id=entered.strategy_run_id,
        action=ACTION_OWNER_EXIT,
        evidence_digest=assess_option_run_repair(
            entered, _staged_exit(), owner_exit=True
        )["evidence_digest"],
        owner_exit=True,
    )
    store.run.status = "exiting"  # another caller won the transition first
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.commit(planned, allowed_from="entered")
    assert owner_exit_refusal(refusal.value).reason_code == "OPTION_RUN_STATE_CHANGED"


def test_the_evidence_digest_is_binding_for_the_owner_exit_too():
    from backend.api.services.option_run_repair import owner_exit_refusal

    run = _run(
        "entered",
        trades=[_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)],
    )
    stale = assess_option_run_repair(run, _staged_exit(), owner_exit=True)["evidence_digest"]
    run.trades.append(_trade("leg_short", "BUY", 75))
    service, store = _exit_service(run)
    with pytest.raises(OptionRunRepairRefusal) as refusal:
        service.plan(
            option_run_id=run.strategy_run_id,
            action=ACTION_OWNER_EXIT,
            evidence_digest=stale,
            owner_exit=True,
        )
    assert owner_exit_refusal(refusal.value).reason_code == "OPTION_RUN_EXIT_EVIDENCE_CHANGED"
    assert store.run.status == "entered"


def test_the_owner_exit_view_names_the_run_evidence_a_post_must_match():
    from backend.api.services.option_run_repair import owner_exit_view

    run = _run(
        "entered",
        trades=[_trade("leg_short", "SELL", 75), _trade("leg_hedge", "BUY", 75)],
    )
    service, _store = _exit_service(run)
    view = owner_exit_view(service, run.strategy_run_id)
    assert view["state"] == STATE_RESIDUAL
    assert view["status"] == "entered"
    assert view["adjust_owner_state"] == "finished"
    assert view["protective_stage_state"] == "resolved"
    assert view["shorts_proven_closed"] is False
    assert view["naked_short_quantity"] == 75
    assert [row["tradingsymbol"] for row in view["close_plan"]] == [SHORT]
    assert view["evidence_digest"] == assess_option_run_repair(
        run, _staged_exit(), owner_exit=True
    )["evidence_digest"]

    # An unresolved stage is reported as its own state, not as "resolved".
    staged = _run(
        "entered",
        trades=[_trade("leg_short", "SELL", 75)],
        orders=_unresolved_stage(),
    )
    service, _store = _exit_service(staged)
    assert owner_exit_view(service, staged.strategy_run_id)["protective_stage_state"] == "sending"
