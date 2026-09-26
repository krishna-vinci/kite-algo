"""The options example's own decision rules, without a broker or a database.

The Phase 5 harness proves the whole path end to end on disposable PostgreSQL.
These are the targeted regressions for the specific ways this example could
silently do the wrong thing:

* a queued AUTONOMOUS close is not a finished close (only ``executed`` is);
* a refused or rejected close is never reported as submitted;
* ANY outstanding work blocks a repeat adjustment, including a state the example
  does not recognise;
* stale chain/Greeks data, a missing live premium and a missing lot size are
  named refusals rather than a guessed price or size;
* a terminal REQUEST is not a closed STRUCTURE: an unknown run status or a
  leftover open leg keeps the child honest.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "hosted_platform" / (
    "options_index_setup_adjustment.py"
)
DYNAMIC_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "hosted_platform" / (
    "options_dynamic_straddle.py"
)


def _load_example():
    spec = importlib.util.spec_from_file_location("phase5_options_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def example(monkeypatch):
    module = _load_example()
    # The wait loop's pacing is not what these tests exercise.
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module


def _stamp(seconds_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class _OptionsApi:
    def __init__(self, chain=None, greeks=None):
        self.chain = chain if chain is not None else {
            "updated_at": _stamp(),
            "expiry": "2026-10-29",
            "spot_ltp": 22520.0,
            "chain": [
                {
                    "strike": 22500.0,
                    "ce": {"token": 50004, "tsym": "NIFTY26OCT22500CE", "ltp": 120.0, "delta": 0.5},
                }
            ],
        }
        self.greeks = greeks if greeks is not None else {
            "updated_at": _stamp(),
            "expiry": "2026-10-29",
            "contracts": [{"strike": 22500.0, "ce": {"delta": 0.5}}],
        }

    def get_chain(self, underlying, expiry=None):  # noqa: ANN001, ARG002
        return dict(self.chain)

    def get_greeks(self, underlying, expiry=None):  # noqa: ANN001, ARG002
        return dict(self.greeks)


class _Client:
    def __init__(self, api):
        self.options = api


class _Run:
    def __init__(self, *, statuses, owned_work, submitted=None, attribution=None):
        self._statuses = list(statuses)
        self._owned_work = owned_work
        self.submitted = dict(submitted or {})
        self._attribution = attribution or {
            "attributed": True,
            "strategy_id": "stg-1",
            "account_id": "kite:paper-options",
        }
        self.config = type("Cfg", (), {"account_scope": "kite:paper-options"})()

    def execution_request(self, request_id):  # noqa: ANN001
        status = self._statuses.pop(0) if self._statuses else "dispatching"
        return {"request_id": request_id, "status": status, "authorization_mode": "autonomous"}

    def owned_work(self):
        return self._owned_work

    def attribution(self):
        return dict(self._attribution)

    def submit_and_request_execution(self, payload, idempotency_key=None):  # noqa: ANN001
        self.last_payload = payload
        return dict(self.submitted)


class _Ctx:
    def __init__(self, client, run, params=None):
        self.client = client
        self.run = run
        self.params = dict(params or {})
        self.run_id = "run-1"
        self.said = []

    def progress(self, text):  # noqa: ANN001
        self.said.append(text)


def _run_row(**overrides):
    row = {
        "option_run_id": "opt-1",
        "status": "entered",
        "expiry": "2026-10-29",
        "expiry_policy": "exit_before_cutoff",
        "structure_id": "structure-1",
        "legs": [
            {
                "tradingsymbol": "NIFTY26OCT22500CE",
                "transaction_type": "BUY",
                "quantity": 50,
                "lot_size": 50,
                "instrument_token": 50004,
            }
        ],
        "pending_legs": [],
        "failed_legs": [],
    }
    row.update(overrides)
    return row


def _close_submitted(request_id: str = "req-close-1", status: str = "queued"):
    return {"execution_request": {"request_id": request_id, "status": status}}


# -- pending work blocks, whatever it is called -----------------------------


@pytest.mark.parametrize(
    "state",
    ["withheld", "releasing", "pending", "partial", "finalizing", "uncertain", "queued", "unfamiliar"],
)
def test_any_pending_state_blocks_a_repeat_adjustment(example, state):
    snapshot = {"pending": [{"plan_id": "p1", "state": state, "remaining_quantity": 25}]}
    assert example._pending_adjustment(snapshot) is snapshot["pending"][0]
    assert example._pending_adjustment({"pending": []}) is None


# -- the close waits in EVERY mode ------------------------------------------


def test_autonomous_queued_close_waits_for_the_executed_outcome(example):
    run = _Run(
        statuses=["queued", "dispatching", "executed"],
        owned_work={"coverage": "known", "pending": [], "option_runs": []},
        submitted=_close_submitted(),
    )
    ctx = _Ctx(_Client(_OptionsApi()), run)

    assert example._submit_close(ctx, "NIFTY", _run_row(), "NRML", 30.0) is True


def test_rejected_close_is_not_reported_as_submitted(example):
    run = _Run(
        statuses=["rejected"],
        owned_work={"coverage": "known", "pending": [], "option_runs": []},
        submitted=_close_submitted(),
    )
    ctx = _Ctx(_Client(_OptionsApi()), run)

    assert example._submit_close(ctx, "NIFTY", _run_row(), "NRML", 30.0) is False
    assert any("did not reach an executed outcome" in line for line in ctx.said)


def test_awaiting_approval_close_times_out_as_not_submitted(example):
    run = _Run(
        statuses=["awaiting_approval"],
        owned_work={"coverage": "known", "pending": [], "option_runs": []},
        submitted=_close_submitted(status="awaiting_approval"),
    )
    ctx = _Ctx(_Client(_OptionsApi()), run)

    assert example._submit_close(ctx, "NIFTY", _run_row(), "NRML", 0.0) is False


# -- freshness and sizing are evidence, never guesses ------------------------


def test_stale_chain_is_a_named_refusal(example):
    api = _OptionsApi(chain={"updated_at": _stamp(3600), "expiry": "2026-10-29", "chain": []})
    run = _Run(statuses=["executed"], owned_work={}, submitted=_close_submitted())
    ctx = _Ctx(_Client(api), run, params={"quote_max_age_seconds": 60})

    assert example._submit_close(ctx, "NIFTY", _run_row(), "NRML", 30.0) is False
    assert any("not fresh" in line for line in ctx.said)


def test_missing_updated_at_is_not_treated_as_fresh(example):
    api = _OptionsApi(chain={"expiry": "2026-10-29", "chain": []}, greeks={"contracts": []})
    run = _Run(statuses=["executed"], owned_work={}, submitted=_close_submitted())
    ctx = _Ctx(_Client(api), run)

    assert example._submit_close(ctx, "NIFTY", _run_row(), "NRML", 30.0) is False
    assert any("no updated_at stamp" in line for line in ctx.said)


def test_resource_error_is_not_treated_as_fresh(example):
    api = _OptionsApi(chain={"updated_at": _stamp(), "resource_error": "rate_limited", "chain": []})
    run = _Run(statuses=["executed"], owned_work={}, submitted=_close_submitted())
    ctx = _Ctx(_Client(api), run)

    assert example._submit_close(ctx, "NIFTY", _run_row(), "NRML", 30.0) is False
    assert any("resource_error" in line for line in ctx.said)


def test_missing_lot_size_refuses_instead_of_guessing_the_unit(example):
    run = _Run(statuses=["executed"], owned_work={}, submitted=_close_submitted())
    ctx = _Ctx(_Client(_OptionsApi()), run)
    leg = _run_row()["legs"][0]
    leg.pop("lot_size")
    leg.pop("quantity")

    assert example._submit_close(ctx, "NIFTY", _run_row(legs=[leg]), "NRML", 30.0) is False
    assert any("no lot size" in line for line in ctx.said)


def test_missing_live_premium_does_not_fall_back_to_the_stored_price(example):
    api = _OptionsApi(
        chain={
            "updated_at": _stamp(),
            "expiry": "2026-10-29",
            "chain": [
                {
                    "strike": 22500.0,
                    "ce": {"token": 50004, "tsym": "NIFTY26OCT22500CE"},
                }
            ],
        }
    )
    run = _Run(statuses=["executed"], owned_work={}, submitted=_close_submitted())
    ctx = _Ctx(_Client(api), run)
    row = _run_row()
    row["legs"][0]["ltp"] = 120.0  # history: must not be used as the price

    assert example._submit_close(ctx, "NIFTY", row, "NRML", 30.0) is False
    assert any("no usable live premium" in line for line in ctx.said)


# -- a terminal request is not a closed structure ---------------------------


def test_unknown_run_status_is_never_reported_as_closed(example):
    snapshot = {
        "coverage": "known",
        "pending": [],
        "positions": [],
        "option_runs": [_run_row(status="teleported")],
    }
    verdict, reason = example._close_evidence(snapshot, snapshot["option_runs"])
    assert verdict == "unknown"
    assert "known vocabulary" in reason
    assert example._open_run(snapshot["option_runs"]) is not None


def test_open_leg_work_keeps_the_close_unproven(example):
    snapshot = {
        "coverage": "known",
        "pending": [],
        "positions": [],
        "option_runs": [_run_row(status="exited", pending_legs=[{"leg_id": "leg-1"}])],
    }
    verdict, reason = example._close_evidence(snapshot, snapshot["option_runs"])
    assert verdict == "open"
    assert "leg work" in reason


def test_unpublished_book_keeps_the_close_unproven(example):
    snapshot = {
        "coverage": "unknown",
        "pending": [],
        "positions": [],
        "option_runs": [_run_row(status="exited")],
    }
    verdict, reason = example._close_evidence(snapshot, snapshot["option_runs"])
    assert verdict == "unknown"
    assert "not published" in reason


def test_remaining_own_quantity_keeps_the_close_unproven(example):
    snapshot = {
        "coverage": "known",
        "pending": [],
        "positions": [{"tradingsymbol": "NIFTY26OCT22500CE", "net_quantity": 50}],
        "option_runs": [_run_row(status="settled")],
    }
    verdict, reason = example._close_evidence(snapshot, snapshot["option_runs"])
    assert verdict == "open"
    assert "open leg" in reason


def test_closed_run_with_no_outstanding_work_is_closed(example):
    snapshot = {
        "coverage": "known",
        "pending": [],
        "positions": [],
        "option_runs": [_run_row(status="settled", completed_legs=[{"leg_id": "leg-1"}])],
    }
    verdict, _reason = example._close_evidence(snapshot, snapshot["option_runs"])
    assert verdict == "closed"


# -- the observation loop does not re-submit while work is outstanding -------


def _spread_chain(strikes=(22500.0, 22600.0)):
    return {
        "updated_at": _stamp(),
        "expiry": "2026-10-29",
        "spot_ltp": 22520.0,
        "chain": [
            {
                "strike": strike,
                "ce": {
                    "token": 50000 + int(strike / 100),
                    "tsym": f"NIFTY26OCT{int(strike)}CE",
                    "ltp": 120.0,
                    "delta": 0.5,
                    "iv": 0.14,
                },
            }
            for strike in strikes
        ],
    }


def _ready_index_reads(ctx):  # noqa: ANN001
    ctx.client.get_candles = lambda *a, **k: {
        "candles": [
            {"timestamp": "2026-09-23T03:45:00Z", "open": 1, "high": 1, "low": 1, "close": 22520}
        ]
        * 30
    }
    ctx.client.calculate_indicator = lambda payload: {
        "ready": True,
        "values": {"rsi": [60.0, 60.0]},
    }


def test_the_duplicate_probe_is_skipped_when_it_is_not_the_held_structure(example):
    """A probe that could open something NEW is not a probe: it is refused."""
    run = _Run(
        statuses=["refused"],
        owned_work={"coverage": "known", "pending": [], "option_runs": []},
        submitted=_close_submitted(),
    )
    ctx = _Ctx(_Client(_OptionsApi(chain=_spread_chain())), run)
    # The run holds a structure the frozen selection no longer describes.
    held = _run_row(
        legs=[
            {
                "tradingsymbol": "NIFTY26OCT22400CE",
                "transaction_type": "BUY",
                "quantity": 50,
                "lot_size": 50,
            }
        ]
    )

    report = example._duplicate_entry_probe(ctx, "NIFTY", held, "NRML", "exit_before_cutoff", 1.0)

    assert "skipped" in report
    assert not hasattr(run, "last_payload"), "the probe submitted an order it should not have"


def test_the_duplicate_probe_reports_the_platforms_own_refusal(example):
    run = _Run(
        statuses=["refused"],
        owned_work={"coverage": "known", "pending": [], "option_runs": []},
        submitted=_close_submitted(),
    )
    run.execution_request = lambda request_id: {  # type: ignore[assignment]
        "request_id": request_id,
        "status": "refused",
        "refusal_code": "OPTION_STRUCTURE_ALREADY_OPEN",
    }
    ctx = _Ctx(_Client(_OptionsApi(chain=_spread_chain())), run)
    held = _run_row(
        legs=[
            {"tradingsymbol": "NIFTY26OCT22500CE", "transaction_type": "BUY", "quantity": 50},
            {"tradingsymbol": "NIFTY26OCT22600CE", "transaction_type": "SELL", "quantity": 50},
        ]
    )

    report = example._duplicate_entry_probe(ctx, "NIFTY", held, "NRML", "exit_before_cutoff", 1.0)

    assert "OPTION_STRUCTURE_ALREADY_OPEN" in report
    # The probe submits an ENTRY plan and nothing else: no close, no orders.
    assert run.last_payload["payload"]["phase"] == "entry"
    assert "-probe" in run.last_payload["evaluation_id"]


def test_hold_after_entry_finishes_with_the_structure_held_and_no_close(example):
    run = _Run(
        statuses=["executed"],
        owned_work={
            "coverage": "known",
            "pending": [],
            "option_runs_coverage": {"coverage": "known", "truncated": False, "reason": ""},
            "option_runs": [],
        },
        submitted=_close_submitted(),
    )
    ctx = _Ctx(
        _Client(_OptionsApi(chain=_spread_chain())),
        run,
        params={"hold_after_entry": True, "deadline_seconds": 1.0},
    )
    _ready_index_reads(ctx)

    assert example.main(ctx) == 0

    assert run.last_payload["payload"]["phase"] == "entry"
    assert any("hold_after_entry=true" in line for line in ctx.said)
    assert not any("close requested" in line for line in ctx.said)


def test_a_repeat_observation_with_pending_work_submits_nothing(example):
    run = _Run(
        statuses=["executed"],
        owned_work={
            "coverage": "known",
            "option_runs_coverage": {"coverage": "known", "truncated": False, "reason": ""},
            # A state the example does not recognise still blocks.
            "pending": [{"plan_id": "p-entry", "state": "some-new-state", "remaining_quantity": 50}],
            "option_runs": [],
        },
    )
    ctx = _Ctx(_Client(_OptionsApi()), run, params={"deadline_seconds": 0.01})
    ctx.client.get_candles = lambda *a, **k: {
        "candles": [
            {"timestamp": "2026-09-23T03:45:00Z", "open": 1, "high": 1, "low": 1, "close": 22520}
        ]
        * 30
    }
    ctx.client.calculate_indicator = lambda payload: {"ready": True, "values": {"rsi": [60.0, 60.0]}}

    # The loop's deadline expires with outstanding work: it must NOT submit.
    assert example.main(ctx) == 2
    assert not hasattr(run, "last_payload")


# =========================================================================
# Example 3: the dynamic straddle's own decision rule
# =========================================================================
#
# The structure manager is a four-way decision (entry / resize / roll / exit)
# read from the run the strategy already holds plus one fresh chain/Greeks read.
# These regressions pin the ways that decision could silently do the wrong
# thing: resize without a declared size, roll onto the expiry it already holds,
# exit by omission, and - the one the platform depends on - freezing an
# adjustment against a generation the run has already moved past.


def _load_dynamic_example():
    spec = importlib.util.spec_from_file_location("phase5_dynamic_straddle", DYNAMIC_EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def dynamic(monkeypatch):
    module = _load_dynamic_example()
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module


FRONT_EXPIRY = "2026-10-29"
NEXT_EXPIRY = "2026-11-26"
DYNAMIC_STRIKES = (22400, 22450, 22500, 22550, 22600, 22650, 22700, 22750)
_OPTION_LOT = 50


def _month_code(expiry: str) -> str:
    return f"{expiry[2:4]}{date.fromisoformat(expiry).strftime('%b').upper()}"


def _dynamic_chain_rows(expiry: str) -> list:
    code = _month_code(expiry)
    spot = 22520.0
    rows = []
    for strike in DYNAMIC_STRIKES:
        distance = abs(float(strike) - spot)
        delta = round(max(0.05, 1.0 - distance / 500.0), 4)
        rows.append(
            {
                "strike": float(strike),
                "ce": {
                    "token": 60000 + strike,
                    "tsym": f"NIFTY{code}{strike}CE",
                    "ltp": max(5.0, 250.0 - distance),
                    "delta": delta,
                    "iv": 0.14,
                },
                "pe": {
                    "token": 65000 + strike,
                    "tsym": f"NIFTY{code}{strike}PE",
                    "ltp": max(5.0, (250.0 - distance) * 0.9),
                    "delta": -delta,
                    "iv": 0.15,
                },
            }
        )
    return rows


class _DynamicOptionsApi:
    def __init__(self, *, expiries=(FRONT_EXPIRY, NEXT_EXPIRY), strikes=DYNAMIC_STRIKES):
        self.expiries = list(expiries)
        self.strikes = tuple(strikes)

    def _rows(self, expiry):
        return [row for row in _dynamic_chain_rows(expiry) if row["strike"] in self.strikes]

    def list_expiries(self, underlying):  # noqa: ANN001, ARG002
        return {"underlying": underlying, "expiries": list(self.expiries)}

    def get_chain(self, underlying, expiry=None):  # noqa: ANN001, ARG002
        selected = str(expiry or self.expiries[0])
        return {
            "underlying": underlying,
            "expiry": selected,
            "spot_ltp": 22520.0,
            "chain": self._rows(selected),
            "updated_at": _stamp(),
        }

    def get_greeks(self, underlying, expiry=None):  # noqa: ANN001, ARG002
        selected = str(expiry or self.expiries[0])
        return {
            "underlying": underlying,
            "expiry": selected,
            "contracts": [
                {
                    "strike": row["strike"],
                    "ce": {"delta": row["ce"]["delta"]},
                    "pe": {"delta": row["pe"]["delta"]},
                }
                for row in self._rows(selected)
            ],
            "updated_at": _stamp(),
        }


def _held_legs(*, expiry=FRONT_EXPIRY, units=1):
    code = _month_code(expiry)
    legs = []
    for index, (side, strike, option_type) in enumerate(
        [
            ("SELL", 22500, "CE"),
            ("SELL", 22500, "PE"),
            ("BUY", 22750, "CE"),
            ("BUY", 22400, "PE"),
        ]
    ):
        legs.append(
            {
                "leg_id": f"entry-plan:{index + 1}",
                "tradingsymbol": f"NIFTY{code}{strike}{option_type}",
                "transaction_type": side,
                "quantity": _OPTION_LOT * units,
                "lot_size": _OPTION_LOT,
                "lots": units,
                "exchange": "NFO",
                "expiry_key": expiry,
                "strike": float(strike),
                "option_type": option_type,
                "metadata": {"ratio": 1, "instrument_id": f"opt-{expiry}-{strike}-{option_type}"},
            }
        )
    return legs


def _held_run(*, generation=1, units=1, expiry=FRONT_EXPIRY, status="entered"):
    return {
        "option_run_id": "opt-dynamic-1",
        "plan_ids": ["entry-plan"],
        "phase": "entry",
        "originating_phase": "entry",
        "underlying": "NIFTY",
        "expiry": expiry,
        "expiry_policy": "exit_before_cutoff",
        "product": "NRML",
        "structure_id": "dynamic-straddle-NIFTY-" + expiry,
        "structure_digest": "digest-1",
        "structure_generation": generation,
        "status": status,
        "legs": _held_legs(expiry=expiry, units=units),
        "completed_legs": [],
        "pending_legs": [],
        "failed_legs": [],
        "protective_exit_unresolved": False,
        "coverage": "known",
    }


def _decision(dynamic, *, params=None, run=None, runs=None, net_delta=None, expiries=None):
    return dynamic._decide(
        params=dict(params or {}),
        run=run,
        runs=list(runs if runs is not None else ([run] if run else [])),
        held_expiry=dynamic._run_expiry(run) if run else "",
        held_units=dynamic._run_units(run) if run else 0,
        net_delta=net_delta,
        expiries=list(expiries or [FRONT_EXPIRY, NEXT_EXPIRY]),
        today=date(2026, 10, 20),
    )


# -- entry -------------------------------------------------------------------


def test_nothing_held_decides_an_entry_at_the_declared_size(dynamic):
    decision = _decision(dynamic, params={"base_units": 3})
    assert decision["action"] == "entry"
    assert decision["units"] == 3


def test_a_closed_run_is_never_re_entered_in_the_same_evaluation(dynamic):
    closed = _held_run(status="exited")
    decision = _decision(dynamic, run=None, runs=[closed], params={"base_units": 1})
    assert decision["action"] == "none"
    assert "already closed" in decision["reason"]


def test_an_unknown_status_is_never_managed_as_open(dynamic):
    decision = _decision(dynamic, run=_held_run(status="teleported"))
    assert decision["action"] == "none"
    assert "known vocabulary" in decision["reason"]


# -- resize ------------------------------------------------------------------


def test_a_net_delta_beyond_the_threshold_resizes_to_the_declared_size(dynamic):
    run = _held_run(generation=1, units=1)
    decision = _decision(
        dynamic,
        run=run,
        params={"resize_units": 2, "resize_delta_threshold": 0.5},
        net_delta=0.9,
    )
    assert decision["action"] == "resize"
    assert decision["units"] == 2
    assert decision["expiry"] == FRONT_EXPIRY
    assert decision["based_on_generation"] == 1


def test_a_net_delta_inside_the_threshold_does_not_resize(dynamic):
    decision = _decision(
        dynamic,
        run=_held_run(generation=1, units=1),
        params={"resize_units": 2, "resize_delta_threshold": 0.5},
        net_delta=0.1,
    )
    assert decision["action"] == "none"


def test_an_undeclared_resize_size_never_resizes(dynamic):
    """No ``resize_units`` means no target: a delta print alone is not a size."""
    decision = _decision(
        dynamic,
        run=_held_run(generation=1, units=1),
        params={"resize_delta_threshold": 0.0},
        net_delta=5.0,
    )
    assert decision["action"] == "none"


def test_the_harness_lever_fires_the_resize_without_a_delta_print(dynamic):
    decision = _decision(
        dynamic,
        run=_held_run(generation=2, units=1),
        params={"resize_units": 3, "force_resize": True},
        net_delta=None,
    )
    assert decision["action"] == "resize"
    assert decision["units"] == 3
    assert decision["based_on_generation"] == 2


def test_a_resize_at_the_size_already_held_is_not_re_submitted(dynamic):
    decision = _decision(
        dynamic,
        run=_held_run(generation=2, units=2),
        params={"resize_units": 2, "force_resize": True},
        net_delta=9.0,
    )
    assert decision["action"] == "none"


# -- roll --------------------------------------------------------------------


def test_the_calendar_trigger_rolls_to_the_next_listed_expiry(dynamic):
    decision = _decision(
        dynamic,
        run=_held_run(generation=1, units=2),
        params={"roll_days_to_expiry": 40},
    )
    assert decision["action"] == "roll"
    assert decision["expiry"] == NEXT_EXPIRY
    assert decision["units"] == 2  # a roll preserves the size it holds
    assert decision["based_on_generation"] == 1


def test_an_explicit_roll_target_wins_over_the_calendar(dynamic):
    decision = _decision(
        dynamic,
        run=_held_run(generation=1, units=1),
        params={"roll_to_expiry": NEXT_EXPIRY, "roll_days_to_expiry": 0},
    )
    assert decision["action"] == "roll"
    assert decision["expiry"] == NEXT_EXPIRY


def test_a_roll_target_equal_to_the_held_expiry_is_not_a_roll(dynamic):
    decision = _decision(
        dynamic,
        run=_held_run(generation=1, units=1),
        params={"roll_to_expiry": FRONT_EXPIRY},
    )
    assert decision["action"] == "none"


def test_no_roll_trigger_means_no_calendar_roll(dynamic):
    decision = _decision(dynamic, run=_held_run(generation=1, units=1), params={})
    assert decision["action"] == "none"


# -- exit --------------------------------------------------------------------


def test_exit_position_wins_over_every_other_declared_action(dynamic):
    decision = _decision(
        dynamic,
        run=_held_run(generation=2, units=2),
        params={"exit_position": True, "roll_to_expiry": NEXT_EXPIRY, "resize_units": 3},
    )
    assert decision["action"] == "exit"
    assert decision["units"] == 2


# -- the basis is never stale ------------------------------------------------


def test_every_adjustment_freezes_the_generation_it_just_read(dynamic):
    """The basis is the run's OWN generation, whatever it has already reached."""
    for generation in (1, 2, 3, 7):
        resize = _decision(
            dynamic,
            run=_held_run(generation=generation, units=1),
            params={"resize_units": 2, "force_resize": True},
        )
        assert resize["based_on_generation"] == generation
        roll = _decision(
            dynamic,
            run=_held_run(generation=generation, units=1),
            params={"roll_to_expiry": NEXT_EXPIRY},
        )
        assert roll["based_on_generation"] == generation
        assert roll["expiry"] == NEXT_EXPIRY


# -- legs and roles ----------------------------------------------------------


def test_the_entry_is_a_straddle_with_both_wings_on_the_same_expiry(dynamic):
    api = _DynamicOptionsApi()
    chain = api.get_chain("NIFTY")
    legs, why = dynamic._entry_legs(
        {"rows": chain["chain"], "spot": chain["spot_ltp"], "expiry": chain["expiry"]},
        api.get_greeks("NIFTY"),
        short_offset_points=0.0,
        wing_width_points=100.0,
    )
    assert legs is not None, why
    assert len(legs) == 4
    assert [leg["ratio"] for leg in legs] == [1, 1, 1, 1]
    sides = [(leg["option_type"], leg["side"]) for leg in legs]
    assert sides == [("CE", "SELL"), ("PE", "SELL"), ("CE", "BUY"), ("PE", "BUY")]
    # Never naked: each option type's protective long covers its short.
    for option_type in ("CE", "PE"):
        bought = sum(leg["ratio"] for leg in legs if leg["option_type"] == option_type and leg["side"] == "BUY")
        sold = sum(leg["ratio"] for leg in legs if leg["option_type"] == option_type and leg["side"] == "SELL")
        assert bought >= sold
    assert legs[0]["strike"] == 22500.0
    assert legs[2]["strike"] == 22600.0
    assert legs[3]["strike"] == 22400.0


def test_a_chain_too_narrow_for_the_wings_refuses_rather_than_going_naked(dynamic):
    api = _DynamicOptionsApi(strikes=(22400, 22500, 22600))
    chain = api.get_chain("NIFTY")
    legs, why = dynamic._entry_legs(
        {"rows": chain["chain"], "spot": chain["spot_ltp"], "expiry": chain["expiry"]},
        api.get_greeks("NIFTY"),
        short_offset_points=0.0,
        wing_width_points=1000.0,
    )
    assert legs is None
    assert "cannot be hedged" in why


def test_a_missing_delta_is_a_named_no_action(dynamic):
    api = _DynamicOptionsApi()
    chain = api.get_chain("NIFTY")
    greeks = api.get_greeks("NIFTY")
    for contract in greeks["contracts"]:
        contract.pop("ce", None)
    legs, why = dynamic._entry_legs(
        {"rows": chain["chain"], "spot": chain["spot_ltp"], "expiry": chain["expiry"]},
        greeks,
        short_offset_points=0.0,
        wing_width_points=100.0,
    )
    assert legs is None
    assert "delta" in why


def test_a_roll_keeps_the_same_legs_and_roles_on_the_new_expiry(dynamic):
    api = _DynamicOptionsApi()
    chain = api.get_chain("NIFTY", expiry=NEXT_EXPIRY)
    legs, why = dynamic._legs_from_run(
        _held_run(generation=2, units=2)["legs"],
        chain={"rows": chain["chain"], "spot": chain["spot_ltp"], "expiry": chain["expiry"]},
        rolling=True,
    )
    assert legs is not None, why
    assert [(leg["option_type"], leg["side"], leg["strike"]) for leg in legs] == [
        ("CE", "SELL", 22500.0),
        ("PE", "SELL", 22500.0),
        ("CE", "BUY", 22750.0),
        ("PE", "BUY", 22400.0),
    ]
    code = _month_code(NEXT_EXPIRY)
    assert all(leg["tradingsymbol"].startswith(f"NIFTY{code}") for leg in legs)


def test_the_exit_closes_every_held_leg_in_the_opposite_direction(dynamic):
    api = _DynamicOptionsApi()
    chain = api.get_chain("NIFTY")
    legs, why = dynamic._exit_legs(
        _held_run(generation=3, units=2)["legs"],
        {"rows": chain["chain"], "spot": chain["spot_ltp"], "expiry": chain["expiry"]},
    )
    assert legs is not None, why
    assert [(leg["option_type"], leg["side"], leg["ratio"]) for leg in legs] == [
        ("CE", "BUY", 2),
        ("PE", "BUY", 2),
        ("CE", "SELL", 2),
        ("PE", "SELL", 2),
    ]


# -- the market-session loop --------------------------------------------------


class _FakeSession:
    """A bound market session that stays open for a fixed number of iterations."""

    def __init__(self, open_iterations: int):
        self.schedule_id = "sched-1"
        self.date = "2026-09-26"
        self._remaining = open_iterations
        self._next_seq = 0

    def market_open(self, *, now=None):  # noqa: ANN001, ARG002
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True

    def next_evaluation_id(self) -> str:
        evaluation_id = f"session:{self.schedule_id}:{self.date}:{self._next_seq}"
        self._next_seq += 1
        return evaluation_id


class _SessionCtx(_Ctx):
    def __init__(self, *, client, run, session, params=None):
        super().__init__(client, run, params)
        self.session = session


def test_a_bound_session_makes_one_fresh_decision_per_iteration_then_stops(dynamic, monkeypatch):
    """Three open iterations decide against three distinct session ids, then stop."""
    calls = []

    def _spy(ctx, params, *, evaluation_id=None, evaluation_kind="run_now"):  # noqa: ANN001, ARG001
        calls.append((evaluation_id, evaluation_kind))
        return 0

    monkeypatch.setattr(dynamic, "_evaluate_once", _spy)
    session = _FakeSession(open_iterations=3)
    ctx = _SessionCtx(
        client=None, run=None, session=session, params={"session_loop_seconds": 0}
    )

    assert dynamic.main(ctx) == 0
    assert len(calls) == 3
    ids = [evaluation_id for evaluation_id, _ in calls]
    assert len(set(ids)) == 3  # a fresh id every iteration, never reused
    assert all(kind == "session_occurrence" for _, kind in calls)
    # The loop stopped because the session closed, not because of a call budget.
    assert session.market_open() is False


def test_no_session_binding_makes_exactly_one_run_now_decision(dynamic, monkeypatch):
    """Without ``ctx.session`` the example is unchanged: one decision, ``run_now``."""
    calls = []

    def _spy(ctx, params, *, evaluation_id=None, evaluation_kind="run_now"):  # noqa: ANN001, ARG001
        calls.append((evaluation_id, evaluation_kind))
        return 0

    monkeypatch.setattr(dynamic, "_evaluate_once", _spy)
    ctx = _Ctx(client=None, run=None, params={})

    assert dynamic.main(ctx) == 0
    assert calls == [(None, "run_now")]


def test_outstanding_work_skips_proposing_in_an_evaluation(dynamic):
    """A held run still mid-transition blocks a fresh proposal, session or not."""
    held = _held_run(generation=2, units=1, status="adjusting")
    run = _Run(
        statuses=[],
        owned_work={
            "option_runs": [held],
            "option_runs_coverage": {"coverage": "known"},
        },
    )
    ctx = _Ctx(client=_Client(_DynamicOptionsApi()), run=run, params={})

    result = dynamic._evaluate_once(ctx, dict(ctx.params))

    assert result == 0
    assert not hasattr(run, "last_payload")
    assert any("outstanding work" in text for text in ctx.said)
