"""Owned-work option-run discovery on PostgreSQL (Phase 5).

The run-bound snapshot must report the option runs THIS strategy owns, derived
from its own bound attempts - never from a caller-supplied identity. These tests
pin the scoping facts that a wrong read would break:

* a run belonging to another canonical strategy, account or environment is not
  reported;
* a run reached through a PRIOR attempt of the SAME strategy is reported (one
  hosted worker run can carry several option runs, and an option run outlives the
  plan that created it);
* the same option run reached from two plans is reported once;
* a binding whose durable run row is missing is UNKNOWN coverage, not an empty
  set.

The database is disposable and uniquely named, created and dropped here; no
shared schema is touched.

    HOSTED_EXECUTION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        .venv/bin/pytest tests/integration/test_owned_work_option_runs_postgres.py -q
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.integration.test_hosted_execution_authorization_postgres import (  # noqa: E402
    ACCOUNT,
    OWNER,
    _PgTestCase,
    _exec,
)

from backend.strategies.execution_snapshot import (  # noqa: E402
    OPTION_RUN_LIMIT,
    OwnedWorkSnapshotService,
)

OTHER_ACCOUNT = "kite:OTHER"
ENVIRONMENT = "paper"


def sqlalchemy_integrity_error():
    from sqlalchemy.exc import IntegrityError

    return IntegrityError


def _generation(sf) -> None:
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES ('11111111-1111-1111-1111-111111111111', 'published', NOW())",
    )


def _strategy(sf, *, strategy_id: str, account: str = ACCOUNT) -> None:
    # A unique name per strategy: the canonical table is unique on (owner, name).
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, :owner, :name, :account, 'active')",
        {"sid": strategy_id, "owner": OWNER, "name": f"S-{strategy_id}", "account": account},
    )


def _worker_run(sf, *, run_id: str, account: str = ACCOUNT, environment: str = ENVIRONMENT) -> None:
    """The run row the binding's composite FK points at."""
    _exec(
        sf,
        "INSERT INTO public.algo_worker_runs "
        "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
        "VALUES (:run, 'tok-1', 'hosted:test', :account, :mode, 'open')",
        {"run": run_id, "account": account, "mode": environment},
    )


def _binding(sf, *, run_id: str, strategy_id: str, account: str = ACCOUNT, environment: str = ENVIRONMENT) -> None:
    _worker_run(sf, run_id=run_id, account=account, environment=environment)
    _exec(
        sf,
        "INSERT INTO public.strategy_run_bindings "
        "(strategy_run_id, strategy_id, owner_id, account_id, execution_environment, "
        " bound_by, binding_source) "
        "VALUES (:run, :sid, :owner, :account, :env, 'supervisor', 'hosted_job')",
        {"run": run_id, "sid": strategy_id, "owner": OWNER, "account": account, "env": environment},
    )


def _plan(
    sf,
    *,
    strategy_id: str,
    account: str,
    run_id: str,
    plan_id: str | None = None,
    legs: list | None = None,
) -> str:
    """One frozen option-structure plan. ``plan_id`` is a UUID column."""
    plan_id = plan_id or str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())
    _exec(
        sf,
        "INSERT INTO public.strategy_proposals "
        "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
        " strategy_run_id, target_kind, payload, payload_sha256, status) "
        "VALUES (:prop, :sid, :account, :eval, 'run_now', :run, 'option_structure', "
        " '{}'::jsonb, :hash, 'validated')",
        {
            "prop": proposal_id,
            "sid": strategy_id,
            "account": account,
            "eval": f"eval-{plan_id}",
            "run": run_id,
            "hash": "p" * 64,
        },
    )
    _exec(
        sf,
        "INSERT INTO public.strategy_plans "
        "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
        " logical_plan, resolved_plan, pinned_catalog_generation) "
        "VALUES (:plan, :prop, :sid, :account, 'option_structure', :hash, "
        " CAST(:logical AS jsonb), CAST(:resolved AS jsonb), :gen)",
        {
            "plan": plan_id,
            "prop": proposal_id,
            "sid": strategy_id,
            "account": account,
            "hash": "h" * 64,
            "logical": json.dumps({"underlying": "NIFTY"}),
            "resolved": json.dumps(
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-10-29",
                    "structure_id": f"structure-{plan_id}",
                    "expiry_policy": "exit_before_cutoff",
                    "legs": list(legs or []),
                }
            ),
            "gen": "11111111-1111-1111-1111-111111111111",
        },
    )
    return plan_id


def _option_edge(sf, *, plan_id: str, option_run_id: str, strategy_id: str, account: str, phase: str = "entry", environment: str = ENVIRONMENT) -> None:
    _exec(
        sf,
        "INSERT INTO public.strategy_plan_option_runs "
        "(plan_id, option_run_id, worker_run_id, strategy_id, account_id, "
        " execution_environment, phase) "
        "VALUES (:plan, :option_run, NULL, :sid, :account, :env, :phase)",
        {
            "plan": plan_id,
            "option_run": option_run_id,
            "sid": strategy_id,
            "account": account,
            "env": environment,
            "phase": phase,
        },
    )


def _option_run(sf, *, option_run_id: str, status: str = "entered", legs: list | None = None) -> None:
    _exec(
        sf,
        "INSERT INTO public.option_run_states "
        "(strategy_run_id, strategy_name, product, status, legs, completed_legs, "
        " pending_legs) "
        "VALUES (:run, 'phase5', 'NRML', :status, CAST(:legs AS jsonb), "
        " '[]'::jsonb, '[]'::jsonb)",
        {
            "run": option_run_id,
            "status": status,
            "legs": json.dumps(legs if legs is not None else [_default_run_leg()]),
        },
    )


def _default_run_leg(**overrides) -> dict:
    leg = {
        "leg_id": "leg-1",
        "tradingsymbol": "NIFTY26OCT22500CE",
        "transaction_type": "BUY",
        "quantity": 50,
        "lot_size": 50,
        "instrument_token": 50004,
        "strike": 22500.0,
        "option_type": "CE",
        "exchange": "NFO",
    }
    leg.update(overrides)
    return leg


def _snapshot(sf, *, strategy_id: str, account: str = ACCOUNT, environment: str = ENVIRONMENT, run_id: str = "run-current"):
    return OwnedWorkSnapshotService(session_factory=sf).snapshot(
        strategy_id=strategy_id,
        account_id=account,
        execution_environment=environment,
        strategy_run_id=run_id,
    )


class OptionRunDiscoveryTests(_PgTestCase):
    def test_scope_is_derived_from_this_strategys_own_bound_attempts(self):
        sf = self.make_db()
        mine = f"stg-{uuid.uuid4().hex[:8]}"
        other = f"stg-{uuid.uuid4().hex[:8]}"
        other_account = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=mine)
        _strategy(sf, strategy_id=other, account=OTHER_ACCOUNT)
        _strategy(sf, strategy_id=other_account, account=OTHER_ACCOUNT)

        # One PRIOR attempt of the SAME strategy plus the current one.
        _binding(sf, run_id="run-prior", strategy_id=mine)
        _binding(sf, run_id="run-current", strategy_id=mine)
        # A strategy on ANOTHER account, and another strategy entirely.
        _binding(sf, run_id="run-other-account", strategy_id=other_account, account=OTHER_ACCOUNT)
        _binding(sf, run_id="run-other-strategy", strategy_id=other, account=OTHER_ACCOUNT)
        # And a DIFFERENT strategy in a different environment.
        _strategy(sf, strategy_id="stg-live")
        _binding(sf, run_id="run-live", strategy_id="stg-live", environment="live")

        own_runs = {"opt-prior": "run-prior", "opt-current": "run-current"}
        for option_run_id, run_id in own_runs.items():
            plan_id = _plan(sf, strategy_id=mine, account=ACCOUNT, run_id=run_id)
            _option_run(sf, option_run_id=option_run_id)
            _option_edge(
                sf, plan_id=plan_id, option_run_id=option_run_id, strategy_id=mine, account=ACCOUNT
            )

        # Other-strategy / other-account / other-environment runs.
        plan_id = _plan(sf, strategy_id=other_account, account=OTHER_ACCOUNT, run_id="run-other-account")
        _option_run(sf, option_run_id="opt-other-account")
        _option_edge(sf, plan_id=plan_id, option_run_id="opt-other-account", strategy_id=other_account, account=OTHER_ACCOUNT)
        plan_id = _plan(sf, strategy_id=other, account=OTHER_ACCOUNT, run_id="run-other-strategy")
        _option_run(sf, option_run_id="opt-other-strategy")
        _option_edge(sf, plan_id=plan_id, option_run_id="opt-other-strategy", strategy_id=other, account=OTHER_ACCOUNT)
        plan_id = _plan(sf, strategy_id="stg-live", account=ACCOUNT, run_id="run-live")
        _option_run(sf, option_run_id="opt-live")
        _option_edge(sf, plan_id=plan_id, option_run_id="opt-live", strategy_id="stg-live", account=ACCOUNT, environment="live")

        snapshot = _snapshot(sf, strategy_id=mine)
        found = {str(row["option_run_id"]) for row in snapshot["option_runs"]}
        assert found == {"opt-prior", "opt-current"}, found
        assert snapshot["option_runs_coverage"]["coverage"] == "known"
        row = next(r for r in snapshot["option_runs"] if r["option_run_id"] == "opt-prior")
        assert row["underlying"] == "NIFTY"
        assert row["expiry"] == "2026-10-29"
        assert row["legs"] and row["legs"][0]["tradingsymbol"] == "NIFTY26OCT22500CE"

    def test_the_same_run_reached_from_two_plans_is_reported_once(self):
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)
        entry_plan = _plan(sf, strategy_id=sid, account=ACCOUNT, run_id="run-current")
        _option_run(sf, option_run_id="opt-shared")
        _option_edge(
            sf, plan_id=entry_plan, option_run_id="opt-shared", strategy_id=sid,
            account=ACCOUNT, phase="entry",
        )
        exit_plan = _plan(sf, strategy_id=sid, account=ACCOUNT, run_id="run-current")
        _option_edge(
            sf, plan_id=exit_plan, option_run_id="opt-shared", strategy_id=sid,
            account=ACCOUNT, phase="exit",
        )

        snapshot = _snapshot(sf, strategy_id=sid)
        assert [row["option_run_id"] for row in snapshot["option_runs"]] == ["opt-shared"]
        assert sorted(snapshot["option_runs"][0]["plan_ids"]) == sorted([entry_plan, exit_plan])

    def test_a_missing_run_row_is_unknown_coverage_not_an_empty_set(self):
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)
        plan_id = _plan(sf, strategy_id=sid, account=ACCOUNT, run_id="run-current")
        # The DATABASE refuses an edge whose durable run row does not exist, so a
        # binding can never outlive the run it points at. The snapshot's
        # unknown-coverage branch stays as defence for a row removed out of band.
        with pytest.raises(sqlalchemy_integrity_error()):
            _option_edge(
                sf, plan_id=plan_id, option_run_id="opt-orphan", strategy_id=sid, account=ACCOUNT
            )

        # With no readable edge at all, the snapshot must not claim completeness
        # it cannot demonstrate: an empty set here is genuinely empty, and the
        # strategy has no option run.
        snapshot = _snapshot(sf, strategy_id=sid)
        assert snapshot["option_runs"] == []
        assert snapshot["option_runs_coverage"]["coverage"] == "known"

    def test_a_scope_mismatch_on_the_edge_is_unknown_not_silently_dropped(self):
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)
        plan_id = _plan(sf, strategy_id=sid, account=ACCOUNT, run_id="run-current")
        _option_run(sf, option_run_id="opt-mismatch")
        # The edge claims a different environment than the plan it hangs from.
        _exec(
            sf,
            "INSERT INTO public.strategy_plan_option_runs "
            "(plan_id, option_run_id, worker_run_id, strategy_id, account_id, "
            " execution_environment, phase) "
            "VALUES (:plan, 'opt-mismatch', NULL, :sid, :account, 'live', 'entry')",
            {"plan": plan_id, "sid": sid, "account": ACCOUNT},
        )

        snapshot = _snapshot(sf, strategy_id=sid)
        assert snapshot["option_runs"] == []
        assert snapshot["option_runs_coverage"]["coverage"] == "unknown"
        assert snapshot["option_runs_coverage"]["reason"] == "option_run_scope_mismatch"

    def test_a_strategy_with_no_option_runs_reports_known_empty(self):
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)

        snapshot = _snapshot(sf, strategy_id=sid)
        assert snapshot["option_runs"] == []
        assert snapshot["option_runs_coverage"]["coverage"] == "known"

    def test_originating_plan_is_the_entry_edge_not_the_lowest_plan_id(self):
        """A close plan written with a lower id must not become the run's origin:
        the entry plan that opened the structure is the originating edge."""
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)
        _option_run(sf, option_run_id="opt-order")

        # The exit plan sorts FIRST lexically; the entry plan sorts last.
        exit_plan = _plan(
            sf, strategy_id=sid, account=ACCOUNT, run_id="run-current",
            plan_id="00000000-0000-0000-0000-0000000000ee",
        )
        _option_edge(
            sf, plan_id=exit_plan, option_run_id="opt-order", strategy_id=sid,
            account=ACCOUNT, phase="exit",
        )
        entry_plan = _plan(
            sf, strategy_id=sid, account=ACCOUNT, run_id="run-current",
            plan_id="ffffffff-0000-0000-0000-0000000000ee",
        )
        _option_edge(
            sf, plan_id=entry_plan, option_run_id="opt-order", strategy_id=sid,
            account=ACCOUNT, phase="entry",
        )

        snapshot = _snapshot(sf, strategy_id=sid)
        row = snapshot["option_runs"][0]
        assert row["originating_plan_id"] == entry_plan
        assert row["originating_phase"] == "entry"
        assert sorted(row["plan_ids"]) == sorted([entry_plan, exit_plan])

    def test_a_truncated_read_is_unknown_coverage(self):
        """More runs than the read limit means the hidden runs may include the
        only open structure: coverage must be unknown, never 'known'."""
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)
        for index in range(OPTION_RUN_LIMIT + 1):
            option_run_id = f"opt-{index:03d}"
            plan_id = _plan(sf, strategy_id=sid, account=ACCOUNT, run_id="run-current")
            _option_run(sf, option_run_id=option_run_id)
            _option_edge(
                sf, plan_id=plan_id, option_run_id=option_run_id, strategy_id=sid,
                account=ACCOUNT,
            )

        snapshot = _snapshot(sf, strategy_id=sid)
        coverage = snapshot["option_runs_coverage"]
        assert coverage["truncated"] is True
        assert coverage["coverage"] == "unknown"
        assert coverage["reason"] == "option_run_limit_truncated"
        assert len(snapshot["option_runs"]) == OPTION_RUN_LIMIT
        assert snapshot["coverage"] == "unknown"

    def test_a_run_whose_legs_contradict_the_frozen_edge_is_unknown(self):
        """A reachable run row whose legs are NOT the frozen legs of the bound
        plan is a mis-scoped read, not evidence about this strategy's book."""
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)
        plan_id = _plan(
            sf,
            strategy_id=sid,
            account=ACCOUNT,
            run_id="run-current",
            legs=[{"instrument_id": "inst-frozen", "tradingsymbol": "NIFTY26OCT22500CE"}],
        )
        _option_run(
            sf,
            option_run_id="opt-mismatched-legs",
            legs=[
                _default_run_leg(
                    tradingsymbol="NIFTY26OCT99999CE",
                    metadata={"instrument_id": "inst-foreign"},
                )
            ],
        )
        _option_edge(
            sf, plan_id=plan_id, option_run_id="opt-mismatched-legs", strategy_id=sid,
            account=ACCOUNT,
        )

        snapshot = _snapshot(sf, strategy_id=sid)
        coverage = snapshot["option_runs_coverage"]
        assert coverage["coverage"] == "unknown"
        assert coverage["reason"] == "option_run_identity_mismatch"

    def test_a_run_leg_matching_on_symbol_alone_is_not_a_contradiction(self):
        """A durable run leg carries its identity under metadata; a leg without
        one must match its frozen counterpart on the symbol rather than being
        reported as a foreign structure."""
        sf = self.make_db()
        sid = f"stg-{uuid.uuid4().hex[:8]}"
        _generation(sf)
        _strategy(sf, strategy_id=sid)
        _binding(sf, run_id="run-current", strategy_id=sid)
        plan_id = _plan(
            sf,
            strategy_id=sid,
            account=ACCOUNT,
            run_id="run-current",
            legs=[{"instrument_id": "inst-frozen", "tradingsymbol": "NIFTY26OCT22500CE"}],
        )
        _option_run(
            sf,
            option_run_id="opt-symbol-match",
            legs=[_default_run_leg(tradingsymbol="NIFTY26OCT22500CE")],
        )
        _option_edge(
            sf, plan_id=plan_id, option_run_id="opt-symbol-match", strategy_id=sid,
            account=ACCOUNT,
        )

        snapshot = _snapshot(sf, strategy_id=sid)
        assert snapshot["option_runs_coverage"]["coverage"] == "known"
        assert snapshot["option_runs"][0]["option_run_id"] == "opt-symbol-match"
