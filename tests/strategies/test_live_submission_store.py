"""The durable live-submission claim is portable (SQLite) and once-only.

PostgreSQL is where the claim's uniqueness is enforced in production (proved in
``tests/integration/test_live_adapter_preparation_postgres.py``); this keeps the
store's SQL working on the established SQLite fixture too, so the adapter cannot
silently depend on one dialect's JSON/`NOW()` handling.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class LiveSubmissionStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _attach_public(dbapi_connection, connection_record):
            _ = connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            cursor.execute(
                """
                CREATE TABLE public.live_plan_submissions (
                    submission_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    step_ref TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    execution_environment TEXT NOT NULL,
                    state TEXT NOT NULL,
                    broker_order_ids TEXT NOT NULL DEFAULT '[]',
                    delta_snapshot TEXT NOT NULL DEFAULT '{}',
                    detail TEXT NOT NULL DEFAULT '{}',
                    consumer_token TEXT,
                    consumer_until TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (plan_id, step_no)
                )
                """
            )
            dbapi_connection.commit()

        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def _store(self):
        from backend.strategies.live_adapter import LiveSubmissionStore

        return LiveSubmissionStore(session_factory=self.factory)

    def _claim(self, store, *, plan_id="plan-1", state="pending", delta=None):
        row, created = store.claim(
            plan_id=plan_id,
            step_no=1,
            step_ref=f"live-plan:{plan_id}:step:1",
            strategy_id="stg-A",
            account_id="kite:A",
            execution_environment="live",
            delta_snapshot={"delta": 10} if delta is None else delta,
            state=state,
        )
        return row, created


    def test_the_first_claim_wins_and_a_second_one_reads_it(self):
        store = self._store()
        first, created = self._claim(store)
        self.assertTrue(created)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["delta_snapshot"], {"delta": 10})

        second, created_again = self._claim(store, state="rejected", delta={"delta": 99})
        self.assertFalse(created_again)
        # The winner's row is returned verbatim: a second caller cannot rewrite
        # the claim, its state or its authorised delta.
        self.assertEqual(second["submission_id"], first["submission_id"])
        self.assertEqual(second["state"], "pending")
        self.assertEqual(second["delta_snapshot"], {"delta": 10})

    def test_an_outcome_is_recorded_on_the_existing_claim(self):
        store = self._store()
        self._claim(store)

        updated = store.record_outcome(
            plan_id="plan-1",
            step_no=1,
            state="uncertain",
            detail={"note": "no order reference"},
        )

        self.assertEqual(updated["state"], "uncertain")
        self.assertEqual(updated["detail"], {"note": "no order reference"})
        self.assertEqual(store.get(plan_id="plan-1", step_no=1)["state"], "uncertain")

    def test_a_staged_portfolio_buy_is_withheld_behind_its_reductions(self):
        """C1.1 §1: a staged live buy waits behind its reductions, never first.

        The portfolio lane freezes every dependent increase against EVERY
        reducing step and puts it behind ``RULE_STAGED_FUNDING_GATE`` - NOT the
        generic prerequisite rule. A filled reduction only proves the sequence
        moved; the staged gate (S2) is what proves the buy is funded, so until it
        lands nothing releases the buy at all.
        """
        from backend.strategies.live_sequence import (
            RULE_STAGED_FUNDING_GATE,
            LaneContext,
            build_portfolio_steps,
            prerequisites_met,
        )

        deltas = {
            "inst-REL": {
                "target": 0, "current": 100, "delta": -100, "side": "SELL",
                "quantity": 100, "lot_size": 1, "notional_inr": 10000.0,
                "increases_exposure": False,
            },
            "inst-INFY": {
                "target": 100, "current": 0, "delta": 100, "side": "BUY",
                "quantity": 100, "lot_size": 1, "notional_inr": 10000.0,
                "increases_exposure": True,
            },
        }
        plan = {
            "plan_id": "plan-1",
            "plan_kind": "target_weights",
            "resolved_plan": {
                "capital_basis_inr": 20000.0,
                "legs": [
                    {"instrument_id": "inst-REL", "product": "CNC", "target_weight": 0.0, "reference_price": 100.0},
                    {"instrument_id": "inst-INFY", "product": "CNC", "target_weight": 0.5, "reference_price": 100.0},
                ],
            },
        }
        ctx = LaneContext(
            plan=plan,
            binding={},
            authority={},
            execution_id="ex-1",
            size_leg=lambda leg: dict(deltas[str(leg.get("instrument_id"))]),
            attributed_quantity=lambda leg: 0,
            staged_financing=True,
        )

        specs = {spec.instrument_id: spec for spec in build_portfolio_steps(ctx)}
        reduction, increase = specs["inst-REL"], specs["inst-INFY"]
        self.assertFalse(reduction.increases_exposure)
        self.assertTrue(increase.increases_exposure)
        # The reduction is ordinary immediate work; the buy is NOT released by
        # the generic rule even when every prerequisite is filled.
        self.assertEqual(reduction.release_rule, "immediate")
        self.assertEqual(increase.release_rule, RULE_STAGED_FUNDING_GATE)
        self.assertEqual(tuple(increase.depends_on), (reduction.step_no,))
        self.assertFalse(prerequisites_met(increase, {reduction.step_no: "pending"}))
        self.assertFalse(prerequisites_met(increase, {reduction.step_no: "rejected"}))
        self.assertFalse(prerequisites_met(increase, {reduction.step_no: "partial"}))
        self.assertTrue(prerequisites_met(increase, {reduction.step_no: "filled"}))

    def test_a_non_staged_portfolio_buy_keeps_the_generic_prerequisite_rule(self):
        """A fully-funded CNC twin: no staged admission, generic dependency rule.

        This is the C1.1 regression guard: a CNC rebalance whose admission did
        not defer financing keeps its original all-prerequisites-filled release
        path, rather than being dragged into the staged placeholder.
        """
        from backend.strategies.live_sequence import (
            RULE_ALL_PREREQUISITES_FILLED,
            LaneContext,
            build_portfolio_steps,
        )

        deltas = {
            "inst-REL": {
                "target": 0, "current": 100, "delta": -100, "side": "SELL",
                "quantity": 100, "lot_size": 1, "notional_inr": 10000.0,
                "increases_exposure": False,
            },
            "inst-INFY": {
                "target": 100, "current": 0, "delta": 100, "side": "BUY",
                "quantity": 100, "lot_size": 1, "notional_inr": 10000.0,
                "increases_exposure": True,
            },
        }
        plan = {
            "plan_id": "plan-2",
            "plan_kind": "target_weights",
            "resolved_plan": {
                "capital_basis_inr": 20000.0,
                "legs": [
                    {"instrument_id": "inst-REL", "product": "CNC", "target_weight": 0.0, "reference_price": 100.0},
                    {"instrument_id": "inst-INFY", "product": "CNC", "target_weight": 0.5, "reference_price": 100.0},
                ],
            },
        }
        ctx = LaneContext(
            plan=plan,
            binding={},
            authority={},
            execution_id="ex-2",
            size_leg=lambda leg: dict(deltas[str(leg.get("instrument_id"))]),
            attributed_quantity=lambda leg: 0,
            staged_financing=False,
        )

        specs = {spec.instrument_id: spec for spec in build_portfolio_steps(ctx)}
        increase = specs["inst-INFY"]
        self.assertEqual(increase.release_rule, RULE_ALL_PREREQUISITES_FILLED)

    def test_only_a_dead_funding_leg_names_the_blocked_steps(self):
        """C1.1 §5: the blocked funding steps are named only once they are DEAD.

        A staged buy whose reduction is pending, partial, finalizing or
        ``repair_required`` is waiting - the protocol working - so nothing is
        reported. Only a terminal reduction that did not COMPLETE a fill leaves
        the buy permanently unfunded, and that is the shape the owner view must
        be able to explain.
        """
        from backend.strategies.live_sequence import staged_funding_blocked_steps

        class _Spec:
            depends_on = (1,)

        def _blocked(state):
            return staged_funding_blocked_steps(_Spec(), {1: state})

        self.assertEqual(_blocked("rejected"), [1])
        self.assertEqual(_blocked("cancelled"), [1])
        self.assertEqual(_blocked("residual_abandoned"), [1])
        for waiting in ("withheld", "pending", "releasing", "partial", "finalizing", "uncertain", "repair_required"):
            self.assertEqual(_blocked(waiting), [], waiting)
        # A reduction that FILLED funded the buy: it is not a blocker.
        self.assertEqual(_blocked("filled"), [])
        self.assertEqual(_blocked("no_op"), [])

        # EVERY funding leg has to be dead: one dead reduction behind a live one
        # still leaves the buy waiting rather than permanently unfunded.
        class _TwoSpec:
            depends_on = (1, 2)

        self.assertEqual(
            staged_funding_blocked_steps(_TwoSpec(), {1: "rejected", 2: "residual_abandoned"}),
            [1, 2],
        )
        self.assertEqual(
            staged_funding_blocked_steps(_TwoSpec(), {1: "rejected", 2: "partial"}), []
        )
        self.assertEqual(
            staged_funding_blocked_steps(_TwoSpec(), {1: "rejected", 2: "filled"}), []
        )

        class _NoDeps:
            depends_on = ()

        self.assertEqual(staged_funding_blocked_steps(_NoDeps(), {}), [])




class _Admission:
    def evaluate(self, _plan, **_kwargs):
        from backend.strategies.admission import AdmissionVerdict

        return AdmissionVerdict(True)


class _Durable:
    def __init__(self, *args, **kwargs):
        pass


class StagedFundingGateTests(unittest.TestCase):
    """Named refusal/admit twins for the executable staged funding stages."""

    NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)

    def setUp(self):
        from backend.strategies.live_adapter import LivePlanAdapter

        self.adapter = LivePlanAdapter(
            session_factory=lambda: None,
            admission=_Admission(),
            approvals=_Durable(),
            ledger=_Durable(),
            barrier=_Durable(),
            submissions=_Durable(),
            clock=lambda: self.NOW,
        )

    def tearDown(self):
        os.environ.pop("LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT", None)

    @staticmethod
    def _spec():
        from backend.strategies.live_sequence import (
            RULE_STAGED_FUNDING_GATE,
            StepSpec,
        )

        return StepSpec(
            step_no=2,
            step_ref="live-plan:plan-1:step:2",
            lane="target_weights",
            domain="CNC",
            instrument_id="inst-REL",
            exchange="NSE",
            tradingsymbol="RELIANCE",
            broker_exchange="NSE",
            broker_symbol="RELIANCE",
            product="CNC",
            variety="regular",
            side="BUY",
            quantity=100,
            lot_size=1,
            target_quantity=100,
            current_quantity=0,
            delta=100,
            notional_inr=10000.0,
            increases_exposure=True,
            depends_on=(1,),
            release_rule=RULE_STAGED_FUNDING_GATE,
        )

    def _gate(self, *, states, quote=None, funds=None):
        plan = {"plan_id": "plan-1", "account_id": "kite:A", "plan_kind": "target_weights"}
        base_quote = {
            "instrument_id": "inst-REL",
            "ltp": 100.0,
            "as_of": self.NOW.isoformat(),
        }
        base_funds = {
            "usable": 10000.0,
            "as_of": self.NOW.isoformat(),
            "account_scope": "kite:A",
        }
        return self.adapter._staged_funding_gate(
            plan,
            self._spec(),
            states=states,
            quote=dict(base_quote if quote is None else quote),
            quote_reader=None,
            funds_reader=lambda: None if funds is False else dict(
                base_funds if funds is None else funds
            ),
            margin_evidence=None,
            catalog_state=None,
        )

    def test_each_gate_refuses_by_name_and_its_admissible_twin_allows(self):
        valid_quote = {
            "instrument_id": "inst-REL", "ltp": 100.0,
            "as_of": self.NOW.isoformat(),
        }
        valid_funds = {
            "usable": 10000.0, "as_of": self.NOW.isoformat(),
            "account_scope": "kite:A",
        }
        cases = (
            (
                "STAGED_FUNDING_REDUCTION_NOT_CONFIRMED",
                {1: "pending"}, valid_quote, valid_funds,
                {1: "filled"}, valid_quote, valid_funds,
            ),
            (
                "STAGED_FUNDING_EVIDENCE_UNAVAILABLE",
                {1: "filled"}, valid_quote, False,
                {1: "filled"}, valid_quote, valid_funds,
            ),
            (
                "STAGED_FUNDING_EVIDENCE_STALE",
                {1: "filled"}, valid_quote,
                {**valid_funds, "as_of": (self.NOW - timedelta(seconds=61)).isoformat()},
                {1: "filled"}, valid_quote, valid_funds,
            ),
            (
                "LIVE_QUOTE_STALE",
                {1: "filled"},
                {**valid_quote, "as_of": (self.NOW - timedelta(seconds=6)).isoformat()},
                valid_funds,
                {1: "filled"}, valid_quote, valid_funds,
            ),
            (
                "LIVE_FINANCING_PRICE_DRIFT",
                {1: "filled"}, {**valid_quote, "ltp": 101.0}, valid_funds,
                {1: "filled"}, valid_quote, valid_funds,
            ),
        )
        for reason, ref_states, ref_quote, ref_funds, admit_states, admit_quote, admit_funds in cases:
            with self.subTest(reason=reason):
                os.environ["LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT"] = "0.005"
                with self.assertRaises(Exception) as refused:
                    asyncio.run(self._gate(states=ref_states, quote=ref_quote, funds=ref_funds))
                self.assertEqual(refused.exception.reason_code, reason)
                allowed_quote, allowed_funds = asyncio.run(
                    self._gate(states=admit_states, quote=admit_quote, funds=admit_funds)
                )
                self.assertEqual(allowed_quote["ltp"], 100.0)
                self.assertEqual(allowed_funds["usable"], 10000.0)

    def test_missing_quote_and_parent_capacity_refuse_by_name(self):
        from backend.strategies.live_sequence import capacity_covers

        with self.assertRaises(Exception) as missing:
            asyncio.run(self._gate(states={1: "filled"}, quote={}, funds=None))
        self.assertEqual(missing.exception.reason_code, "LIVE_QUOTE_MISSING")

        covered, detail = capacity_covers({"reserved_notional_inr": 99.0}, [self._spec()])
        self.assertFalse(covered)
        self.assertEqual(detail["required_inr"], 10000.0)

class _FakeOptionMarket:
    """A canonical session shape with exactly the two frozen legs."""

    def __init__(self, *, now):
        self.snapshot_updated_at = now
        self.greek_updated_at = now
        self.packets = {
            "opt-a": {"token": "opt-a", "tsym": "A", "ltp": 101.0, "iv": 0.12, "delta": 0.3, "updated_at": now},
            "opt-b": {"token": "opt-b", "tsym": "B", "ltp": 41.0, "iv": 0.13, "delta": -0.2, "updated_at": now},
        }

    def _payload(self):
        return [
            {"strike": 100.0, "ce": self.packets["opt-a"], "pe": None},
            {"strike": 110.0, "ce": None, "pe": self.packets["opt-b"]},
        ]

    def get_chain(self, _underlying, _expiry):
        return {
            "underlying": "NIFTY", "expiry": "2026-10-29", "chain": self._payload(),
            "updated_at": self.snapshot_updated_at,
        }

    def get_greeks(self, _underlying, _expiry):
        return {"contracts": self._payload(), "updated_at": self.greek_updated_at}


class LiveOptionChainFreshnessTests(unittest.TestCase):
    """Freeze binds the plan; the release pass rechecks the immutable evidence."""

    NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)

    def _plan(self, *, age_seconds=0.0, greek_age_seconds=0.0, drop=False):
        market = _FakeOptionMarket(now=self.NOW)
        market.snapshot_updated_at = self.NOW - timedelta(seconds=age_seconds)
        market.greek_updated_at = self.NOW - timedelta(seconds=greek_age_seconds)
        for packet in market.packets.values():
            packet["updated_at"] = market.greek_updated_at
        if drop:
            market.packets.pop("opt-b")
        plan = {
            "plan_id": "plan-opt", "plan_kind": "option_structure",
            "resolved_plan": {
                "target_kind": "option_structure",
                "underlying": "NIFTY", "expiry": "2026-10-29",
                "option_run": {"phase": "exit"},
                "legs": [
                    {"instrument_id": "opt-a", "broker_token": "opt-a"},
                    {"instrument_id": "opt-b", "broker_token": "opt-b"},
                ],
            },
        }
        from backend.options.market.freshness import option_chain_freeze_evidence

        plan["resolved_plan"]["option_chain_evidence"] = option_chain_freeze_evidence(
            market, plan, now=self.NOW
        )
        return plan, market

    def test_freeze_has_the_design_shape_and_leg_digest(self):
        plan, _market = self._plan()
        evidence = plan["resolved_plan"]["option_chain_evidence"]

        self.assertEqual(set(evidence), {
            "underlying", "expiry", "snapshot_updated_at", "snapshot_digest", "legs"
        })
        self.assertTrue(evidence["snapshot_digest"])
        self.assertEqual(evidence["legs"]["opt-a"]["ltp"], 101.0)

    def test_a_missing_leg_is_chain_snapshot_unavailable(self):
        from backend.options.market.freshness import OptionChainEvidenceRefusal

        with self.assertRaises(OptionChainEvidenceRefusal) as ctx:
            self._plan(drop=True)
        self.assertEqual(ctx.exception.reason_code, "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE")

    def test_stale_snapshot_and_stale_greeks_are_separate_refusals(self):
        from backend.strategies.live_adapter import LivePlanAdapter, LiveRefusal

        adapter = LivePlanAdapter(
            session_factory=lambda: None,
            admission=_Admission(),
            approvals=_Durable(),
            ledger=_Durable(),
            barrier=_Durable(),
            submissions=_Durable(),
            clock=lambda: self.NOW,
        )
        stale_plan, _ = self._plan(age_seconds=6)
        with self.assertRaises(LiveRefusal) as stale:
            adapter._check_admission(stale_plan, margin_evidence=None, catalog_state=None)
        self.assertEqual(stale.exception.reason_code, "OPTION_CHAIN_SNAPSHOT_STALE")

        stale_greeks, _ = self._plan(greek_age_seconds=6)
        with self.assertRaises(LiveRefusal) as greeks:
            adapter._check_admission(stale_greeks, margin_evidence=None, catalog_state=None)
        self.assertEqual(greeks.exception.reason_code, "OPTION_GREEKS_STALE")

        fresh_plan, _ = self._plan()
        result = adapter._check_admission(
            fresh_plan, margin_evidence=None, catalog_state=None
        )
        self.assertTrue(result["admitted"])


if __name__ == "__main__":
    unittest.main()
