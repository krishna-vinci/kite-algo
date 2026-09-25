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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "hosted_platform" / (
    "options_index_setup_adjustment.py"
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
