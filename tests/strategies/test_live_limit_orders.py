"""C1.2 S4: bounded LIMIT dispatch for gated live legs.

Two lenses, both on the FAKE broker boundary:

* the shared price derivation (``live_limit_orders``) - the refusal and admit
  twins for an unavailable price, a price outside the frozen band and an unknown
  broker tick, plus the ask/bid clamp, the LTP fallback and passive tick
  rounding;
* the adapter's dispatch and timeout lifecycle - a gated leg leaves as a LIMIT at
  the derived price, a non-gated leg is untouched, and a working LIMIT that
  outlives the platform timeout is cancelled exactly once with an explicit
  terminal outcome.

No real broker, no network and no database are used.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

from backend.strategies.live_adapter import LivePlanAdapter, LiveRefusal
from backend.strategies.live_limit_orders import (
    LIVE_LIMIT_PRICE_BOUND_EXCEEDED,
    LIVE_LIMIT_PRICE_UNAVAILABLE,
    LIVE_LIMIT_TICK_UNKNOWN,
    LimitOrderRefusal,
    derive_bounded_limit,
    gated_limit_timeout_seconds,
    is_gated_limit_release_rule,
    option_limit_max_drift_pct,
    round_toward_passive,
    staged_buy_max_price_drift_pct,
)
from backend.strategies.live_sequence import (
    RULE_HEDGE_FILL_GATE,
    RULE_IMMEDIATE,
    RULE_MIS_SQUAREOFF,
    RULE_STAGED_FUNDING_GATE,
    StepSpec,
    prerequisites_met,
)


NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
TERMINAL = ("filled", "rejected", "no_op", "residual_abandoned")


def _spec(
    *,
    step_no: int = 2,
    side: str = "BUY",
    quantity: int = 100,
    notional_inr: float = 10000.0,
    release_rule: str = RULE_STAGED_FUNDING_GATE,
    lane: str = "target_weights",
    depends_on: tuple = (1,),
    detail: dict | None = None,
) -> StepSpec:
    return StepSpec(
        step_no=step_no,
        step_ref=f"live-plan:plan-1:step:{step_no}",
        lane=lane,
        domain="CNC",
        instrument_id="inst-REL",
        exchange="NSE",
        tradingsymbol="RELIANCE",
        broker_exchange="NSE",
        broker_symbol="RELIANCE",
        product="CNC",
        variety="regular",
        side=side,
        quantity=quantity,
        lot_size=1,
        target_quantity=quantity,
        current_quantity=0,
        delta=quantity,
        notional_inr=notional_inr,
        increases_exposure=True,
        depends_on=depends_on,
        release_rule=release_rule,
        detail=detail or {},
    )


class _FakeSubmissions:
    """An in-memory stand-in for the durable per-step claim."""

    def __init__(self, rows: list | None = None):
        self.rows: dict = {}
        for row in rows or []:
            self.rows[(str(row["plan_id"]), int(row["step_no"]))] = {
                "broker_order_ids": [],
                "delta_snapshot": {},
                **row,
                "detail": dict(row.get("detail") or {}),
            }

    def record_outcome(
        self,
        *,
        plan_id,
        step_no,
        state,
        broker_order_ids=(),
        detail=None,
        merge_detail=True,
        consumer_token=None,
        db=None,
    ):
        key = (str(plan_id), int(step_no))
        row = self.rows.setdefault(
            key,
            {
                "plan_id": str(plan_id),
                "step_no": int(step_no),
                "state": "pending",
                "broker_order_ids": [],
                "delta_snapshot": {},
                "detail": {},
            },
        )
        if str(row.get("state") or "") not in TERMINAL:
            row["state"] = str(state)
        if broker_order_ids:
            row["broker_order_ids"] = [str(value) for value in broker_order_ids]
        merged = dict(row.get("detail") or {}) if merge_detail else {}
        merged.update(dict(detail or {}))
        row["detail"] = merged
        return dict(row)

    def working_limit_steps(self, *, limit: int = 100):
        return [
            dict(row)
            for row in self.rows.values()
            if str(row.get("state") or "") in ("pending", "partial")
            and str((row.get("detail") or {}).get("execution_order", {}).get("order_type") or "")
            == "LIMIT"
        ][: int(limit)]


class _FakeBroker:
    """The intent boundary. Records every intent, and can fail a cancel."""

    def __init__(self, *, fail_cancel: bool = False):
        self.intents: list = []
        self.fail_cancel = fail_cancel

    async def handle(self, intent, *, context=None):
        self.intents.append((intent, dict(context or {})))
        if intent.intent_type == "cancel_order" and self.fail_cancel:
            raise RuntimeError("transport lost during cancel")
        return {"result": {"order_id": f"O-{len(self.intents)}", "status": "success"}}

    def intents_of(self, kind: str):
        return [intent for intent, _ctx in self.intents if intent.intent_type == kind]


def _adapter(*, broker=None, submissions=None, fill_reader=None, tick_reader=0.05):
    return LivePlanAdapter(
        session_factory=lambda: None,
        admission=object(),
        approvals=object(),
        ledger=object(),
        barrier=object(),
        submissions=submissions if submissions is not None else _FakeSubmissions(),
        intent_handler=broker if broker is not None else _FakeBroker(),
        fill_reader=fill_reader,
        clock=lambda: NOW,
        tick_reader=(None if tick_reader is None else (lambda plan, leg: tick_reader)),
    )


def _quote(**overrides):
    quote = {"instrument_id": "inst-REL", "ltp": 100.0, "as_of": NOW.isoformat()}
    quote.update(overrides)
    return quote


class DerivationTests(unittest.TestCase):
    """The shared derivation: admit and refusal twins per gate."""

    def test_a_buy_and_a_sell_are_bounded_and_rounded_to_the_passive_side(self):
        buy = derive_bounded_limit(
            "BUY", 100.0, {"ask": 100.2, "bid": 99.9, "ltp": 100.0}, 0.005, 0.05
        )
        self.assertEqual(buy["order_type"], "LIMIT")
        self.assertEqual(buy["reference_source"], "ask")
        self.assertEqual(buy["price"], 100.2)
        self.assertLessEqual(buy["price"], buy["bound_price_inr"] + 1e-9)

        sell = derive_bounded_limit(
            "SELL", 100.0, {"bid": 99.8, "ask": 100.2, "ltp": 100.0}, 0.005, 0.05
        )
        self.assertEqual(sell["reference_source"], "bid")
        self.assertEqual(sell["price"], 99.8)
        self.assertGreaterEqual(sell["price"], sell["bound_price_inr"] - 1e-9)

    def test_a_book_side_beyond_the_band_is_clamped_not_widened(self):
        buy = derive_bounded_limit("BUY", 100.0, {"ask": 130.0, "ltp": 130.0}, 0.005, 0.05)
        self.assertEqual(buy["price"], 100.5)
        self.assertEqual(buy["price"], buy["bound_price_inr"])

        sell = derive_bounded_limit("SELL", 100.0, {"bid": 70.0, "ltp": 70.0}, 0.005, 0.05)
        self.assertEqual(sell["price"], 99.5)
        self.assertEqual(sell["price"], sell["bound_price_inr"])

    def test_an_absent_book_side_uses_the_fresh_ltp_as_reference(self):
        buy = derive_bounded_limit("BUY", 100.0, {"ltp": 100.0}, 0.005, 0.05)
        self.assertEqual(buy["reference_source"], "ltp")
        self.assertEqual(buy["price"], 100.5)

    def test_price_unavailable_and_bound_exceeded_are_named_twins(self):
        with self.assertRaises(LimitOrderRefusal) as unavailable:
            derive_bounded_limit("BUY", 100.0, {"bid": 99.0}, 0.005, 0.05)
        self.assertEqual(unavailable.exception.reason_code, LIVE_LIMIT_PRICE_UNAVAILABLE)

        with self.assertRaises(LimitOrderRefusal) as bound:
            derive_bounded_limit("BUY", 1500.0, {"ltp": 1520.0}, 0.005, 0.05)
        self.assertEqual(bound.exception.reason_code, LIVE_LIMIT_PRICE_BOUND_EXCEEDED)

        with self.assertRaises(LimitOrderRefusal) as sell_bound:
            derive_bounded_limit("SELL", 1500.0, {"ltp": 1480.0}, 0.005, 0.05)
        self.assertEqual(sell_bound.exception.reason_code, LIVE_LIMIT_PRICE_BOUND_EXCEEDED)

    def test_an_unknown_tick_refuses_rather_than_guessing_a_grid(self):
        with self.assertRaises(LimitOrderRefusal) as refusal:
            derive_bounded_limit("BUY", 100.0, {"ltp": 100.0}, 0.005, None)
        self.assertEqual(refusal.exception.reason_code, LIVE_LIMIT_TICK_UNKNOWN)

        with self.assertRaises(LimitOrderRefusal) as zero:
            derive_bounded_limit("BUY", 100.0, {"ltp": 100.0}, 0.005, 0.0)
        self.assertEqual(zero.exception.reason_code, LIVE_LIMIT_TICK_UNKNOWN)

        self.assertEqual(round_toward_passive(100.49, "BUY", 0.05), 100.45)
        self.assertEqual(round_toward_passive(99.51, "SELL", 0.05), 99.55)

    def test_only_dependent_release_rules_take_the_bounded_limit_path(self):
        self.assertTrue(is_gated_limit_release_rule(RULE_STAGED_FUNDING_GATE))
        self.assertTrue(is_gated_limit_release_rule(RULE_HEDGE_FILL_GATE))
        self.assertFalse(is_gated_limit_release_rule(RULE_IMMEDIATE))
        self.assertFalse(is_gated_limit_release_rule(RULE_MIS_SQUAREOFF))

    def test_configuration_defaults_and_names_are_shared(self):
        saved = {
            key: os.environ.pop(key, None)
            for key in (
                "LIVE_OPTION_LIMIT_MAX_DRIFT_PCT",
                "LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT",
                "LIVE_GATED_LIMIT_TIMEOUT_SECONDS",
            )
        }
        try:
            self.assertEqual(option_limit_max_drift_pct(), 0.005)
            self.assertEqual(staged_buy_max_price_drift_pct(), 0.005)
            self.assertEqual(gated_limit_timeout_seconds(), 10.0)
            os.environ["LIVE_GATED_LIMIT_TIMEOUT_SECONDS"] = "3"
            self.assertEqual(gated_limit_timeout_seconds(), 3.0)
            os.environ["LIVE_GATED_LIMIT_TIMEOUT_SECONDS"] = "not-a-number"
            self.assertEqual(gated_limit_timeout_seconds(), 10.0)
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value


class GatedDispatchTests(unittest.TestCase):
    """A gated leg leaves as a LIMIT at the derived price; a plain leg does not."""

    def test_a_c11_staged_cnc_dependent_buy_is_sent_as_a_bounded_limit(self):
        broker = _FakeBroker()
        submissions = _FakeSubmissions()
        adapter = _adapter(broker=broker, submissions=submissions)

        outcome = asyncio.run(
            adapter.dispatch_step(
                {"plan_id": "plan-1", "account_id": "kite:A", "plan_kind": "target_weights"},
                _spec(),
                binding={"strategy_run_id": "run-1"},
                account_id="kite:A",
                strategy_id="stg-A",
                released_by="live-sequence",
                quote=_quote(ask=100.2, bid=99.9, ltp=100.0),
            )
        )
        self.assertEqual(outcome["state"], "pending")
        placed = broker.intents_of("place_order")
        self.assertEqual(len(placed), 1)
        order = placed[0].payload["order"]
        self.assertEqual(order["order_type"], "LIMIT")
        self.assertEqual(order["price"], 100.2)
        self.assertEqual(order["quantity"], 100)

        evidence = outcome["detail"]["execution_order"]
        self.assertEqual(evidence["order_type"], "LIMIT")
        self.assertEqual(evidence["reference_price_inr"], 100.0)
        self.assertEqual(evidence["bound_price_inr"], 100.5)
        self.assertEqual(evidence["tick_size"], 0.05)
        self.assertEqual(evidence["tick_source"], "catalog")
        self.assertEqual(evidence["reference_source"], "ask")
        self.assertEqual(evidence["max_drift_env"], "LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT")
        self.assertTrue(evidence["submitted_at"])

    def test_an_option_gated_short_uses_the_option_drift_env(self):
        os.environ["LIVE_OPTION_LIMIT_MAX_DRIFT_PCT"] = "0.02"
        try:
            broker = _FakeBroker()
            adapter = _adapter(broker=broker)
            outcome = asyncio.run(
                adapter.dispatch_step(
                    {
                        "plan_id": "plan-1",
                        "account_id": "kite:A",
                        "plan_kind": "option_structure",
                    },
                    _spec(
                        side="SELL",
                        lane="option_structure",
                        release_rule=RULE_HEDGE_FILL_GATE,
                        quantity=75,
                        notional_inr=7500.0,
                    ),
                    binding={"strategy_run_id": "run-1"},
                    account_id="kite:A",
                    strategy_id="stg-A",
                    released_by="live-sequence",
                    quote=_quote(ltp=100.0),
                )
            )
        finally:
            os.environ.pop("LIVE_OPTION_LIMIT_MAX_DRIFT_PCT", None)
        order = broker.intents_of("place_order")[0].payload["order"]
        self.assertEqual(order["order_type"], "LIMIT")
        self.assertEqual(order["price"], 98.0)
        self.assertEqual(
            outcome["detail"]["execution_order"]["max_drift_env"],
            "LIVE_OPTION_LIMIT_MAX_DRIFT_PCT",
        )

    def test_a_non_gated_leg_keeps_its_market_dispatch(self):
        broker = _FakeBroker()
        adapter = _adapter(broker=broker)
        asyncio.run(
            adapter.dispatch_step(
                {"plan_id": "plan-1", "account_id": "kite:A", "plan_kind": "target_weights"},
                _spec(step_no=1, release_rule=RULE_IMMEDIATE, depends_on=()),
                binding={"strategy_run_id": "run-1"},
                account_id="kite:A",
                strategy_id="stg-A",
                quote=_quote(),
            )
        )
        order = broker.intents_of("place_order")[0].payload["order"]
        self.assertEqual(order["order_type"], "MARKET")
        self.assertNotIn("price", order)

    def test_tick_unknown_and_price_refusals_reach_the_adapter_by_name(self):
        for tick_reader, quote, expected in (
            (None, _quote(), LIVE_LIMIT_TICK_UNKNOWN),
            (0.05, _quote(ltp=0.0), LIVE_LIMIT_PRICE_UNAVAILABLE),
            (0.05, _quote(ltp=1520.0), LIVE_LIMIT_PRICE_BOUND_EXCEEDED),
        ):
            with self.subTest(expected=expected):
                broker = _FakeBroker()
                adapter = _adapter(broker=broker, tick_reader=tick_reader)
                with self.assertRaises(LiveRefusal) as refusal:
                    asyncio.run(
                        adapter.dispatch_step(
                            {
                                "plan_id": "plan-1",
                                "account_id": "kite:A",
                                "plan_kind": "target_weights",
                            },
                            _spec(notional_inr=150000.0),
                            binding={"strategy_run_id": "run-1"},
                            account_id="kite:A",
                            strategy_id="stg-A",
                            released_by="live-sequence",
                            quote=quote,
                        )
                    )
                self.assertEqual(refusal.exception.reason_code, expected)
                self.assertEqual(broker.intents, [], "a refused leg reached the broker")


class LimitTimeoutTests(unittest.TestCase):
    """The timeout lifecycle: cancel once, explicit outcomes, no replacement."""

    def _row(self, *, state="pending", age_seconds=30, increases_exposure=True, quantity=100):
        return {
            "plan_id": "plan-1",
            "step_no": 2,
            "step_ref": "live-plan:plan-1:step:2",
            "state": state,
            "broker_order_ids": ["O-1"],
            "delta_snapshot": {
                "quantity": quantity,
                "increases_exposure": increases_exposure,
            },
            "detail": {
                "execution_order": {
                    "order_type": "LIMIT",
                    "price": 100.5,
                    "quantity": quantity,
                    "session_id": "sess-1",
                    "variety": "regular",
                    "submitted_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
                }
            },
        }

    def test_a_working_limit_inside_its_timeout_is_left_alone(self):
        submissions = _FakeSubmissions([self._row(age_seconds=3)])
        broker = _FakeBroker()
        counts = asyncio.run(
            _adapter(broker=broker, submissions=submissions).expire_timed_out_limits()
        )
        self.assertEqual(counts["expired"], 0)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(broker.intents, [])

    def test_a_zero_filled_cancel_is_terminal_and_releases_no_dependent(self):
        submissions = _FakeSubmissions([self._row()])
        broker = _FakeBroker()
        adapter = _adapter(broker=broker, submissions=submissions, fill_reader=lambda **_: [])

        counts = asyncio.run(adapter.expire_timed_out_limits())
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(counts["declared"][0]["outcome"], "rejected")
        cancels = broker.intents_of("cancel_order")
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0].payload["order"]["order_id"], "O-1")

        stored = submissions.rows[("plan-1", 2)]
        self.assertEqual(stored["state"], "rejected")
        self.assertEqual(stored["detail"]["limit_timeout"]["terminal"], "cancelled")
        self.assertEqual(stored["detail"]["limit_timeout"]["cancel_state"], "accepted")
        self.assertFalse(stored["detail"]["limit_timeout"]["action_required"])

        dependent = _spec(step_no=3, depends_on=(2,))
        self.assertFalse(
            prerequisites_met(dependent, {2: stored["state"]}),
            "a cancelled hedge released the short it defends",
        )

    def test_a_partial_fill_retains_the_remainder_and_releases_nothing(self):
        submissions = _FakeSubmissions([self._row(quantity=100)])
        broker = _FakeBroker()
        adapter = _adapter(
            broker=broker,
            submissions=submissions,
            fill_reader=lambda **_: [{"quantity": 40}],
        )
        counts = asyncio.run(adapter.expire_timed_out_limits())
        self.assertEqual(counts["partial"], 1)
        stored = submissions.rows[("plan-1", 2)]
        self.assertEqual(stored["state"], "partial")
        self.assertEqual(stored["detail"]["limit_timeout"]["filled_quantity"], 40)
        self.assertEqual(stored["detail"]["limit_timeout"]["residual_quantity"], 60)
        self.assertEqual(
            stored["detail"]["limit_timeout"]["blocking"],
            "limit_timeout_partial_residual_retained",
        )
        dependent = _spec(step_no=3, depends_on=(2,))
        self.assertFalse(prerequisites_met(dependent, {2: stored["state"]}))

    def test_an_unfilled_risk_reduction_becomes_named_action_required(self):
        submissions = _FakeSubmissions([self._row(increases_exposure=False)])
        broker = _FakeBroker()
        adapter = _adapter(broker=broker, submissions=submissions, fill_reader=lambda **_: [])
        counts = asyncio.run(adapter.expire_timed_out_limits())
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(counts["declared"][0]["outcome"], "repair_required")
        stored = submissions.rows[("plan-1", 2)]
        self.assertEqual(stored["state"], "repair_required")
        self.assertTrue(stored["detail"]["limit_timeout"]["action_required"])
        self.assertEqual(
            stored["detail"]["limit_timeout"]["blocking"], "limit_timeout_reduction_unfilled"
        )

    def test_an_uncertain_cancel_is_fenced_and_never_repeated(self):
        submissions = _FakeSubmissions([self._row()])
        broker = _FakeBroker(fail_cancel=True)
        adapter = _adapter(broker=broker, submissions=submissions, fill_reader=lambda **_: [])
        first = asyncio.run(adapter.expire_timed_out_limits())
        second = asyncio.run(adapter.expire_timed_out_limits())
        self.assertEqual(first["uncertain"], 1)
        self.assertEqual(second["uncertain"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(broker.intents_of("cancel_order")), 1, "the cancel was repeated")
        stored = submissions.rows[("plan-1", 2)]
        self.assertEqual(stored["state"], "pending", "uncertain work must stay unresolved")
        self.assertEqual(stored["detail"]["limit_timeout"]["cancel_state"], "uncertain")

    def test_a_restarted_process_never_sends_a_second_cancel(self):
        submissions = _FakeSubmissions([self._row()])
        broker = _FakeBroker(fail_cancel=True)
        first_adapter = _adapter(broker=broker, submissions=submissions, fill_reader=lambda **_: [])
        asyncio.run(first_adapter.expire_timed_out_limits())

        # A restart: a NEW adapter object over the SAME durable claim.
        restarted = _adapter(broker=broker, submissions=submissions, fill_reader=lambda **_: [])
        counts = asyncio.run(restarted.expire_timed_out_limits())
        self.assertEqual(counts["uncertain"], 0)
        self.assertEqual(len(broker.intents_of("cancel_order")), 1)

    def test_a_cancel_that_raced_a_complete_fill_leaves_ingestion_the_outcome(self):
        submissions = _FakeSubmissions([self._row(quantity=100)])
        broker = _FakeBroker()
        adapter = _adapter(
            broker=broker,
            submissions=submissions,
            fill_reader=lambda **_: [{"quantity": 100}],
        )
        counts = asyncio.run(adapter.expire_timed_out_limits())
        self.assertEqual(counts["filled"], 1)
        stored = submissions.rows[("plan-1", 2)]
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(stored["detail"]["limit_timeout"]["filled_quantity"], 100)

    def test_an_unreadable_fill_source_refuses_to_claim_a_terminal_outcome(self):
        submissions = _FakeSubmissions([self._row()])
        broker = _FakeBroker()
        adapter = _adapter(broker=broker, submissions=submissions, fill_reader=None)
        counts = asyncio.run(adapter.expire_timed_out_limits())
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual(submissions.rows[("plan-1", 2)]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
