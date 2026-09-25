"""Admission: deterministic ordered checks with named refusals (G9).

Admission is a pure function of the plan, the policy and the evidence handed to
it. These tests pin the order (first refusal wins), the NULL-means-unenforced
rule, and — most importantly — the places where V1 cannot prove something and
therefore refuses instead of silently treating the axis as satisfied.

SQLite runs with the established ``public.`` ATTACH fixture.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the new tables

G1 = "11111111-1111-1111-1111-111111111111"
T1 = "2026-09-01T00:00:00+00:00"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class AdmissionTestCase(unittest.TestCase):
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
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_generations (
                    id TEXT PRIMARY KEY, status TEXT, published_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_records (
                    instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,
                    lifecycle_status TEXT NOT NULL DEFAULT 'active',
                    current_generation_id TEXT,
                    instrument_type TEXT, expiry TEXT, lot_size INTEGER, tick_size REAL,
                    underlying TEXT,
                    strike REAL, option_type TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_broker_mappings (
                    mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT,
                    broker_exchange TEXT, broker_symbol TEXT, broker_token INTEGER,
                    valid_from_generation TEXT, valid_to_generation TEXT, is_current INTEGER
                )
                """
            )
            dbapi_connection.commit()

        from backend.strategies.attribution_models import (
            AccountReconciliationVersion,
            Strategy,
            StrategyAdmissionPolicy,
            StrategyApproval,
            StrategyPlan,
            StrategyPositionProjection,
            StrategyProjectionState,
            StrategyProposal,
            StrategyReservation,
            StrategyReservationEvent,
        )

        _Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyProposal.__table__,
                StrategyPlan.__table__,
                StrategyAdmissionPolicy.__table__,
                StrategyReservation.__table__,
                StrategyReservationEvent.__table__,
                StrategyApproval.__table__,
                AccountReconciliationVersion.__table__,
                StrategyPositionProjection.__table__,
                StrategyProjectionState.__table__,
            ],
        )
        self.factory = sessionmaker(bind=self.engine)
        self.service = self._service()

        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES (:id, 'published', :published_at)"
                ),
                {"id": G1, "published_at": T1},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, current_generation_id) "
                    "VALUES ('inst-REL', 'NSE', 'RELIANCE', 'active', :id)"
                ),
                {"id": G1},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES ('map-REL', 'inst-REL', 'kite', 'NSE', 'RELIANCE', 100, :id, 1)"
                ),
                {"id": G1},
            )
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def _service(self):
        from backend.strategies.admission import AdmissionService

        return AdmissionService(session_factory=self.factory)

    # -- fixtures -----------------------------------------------------------

    def plan(self, **overrides):
        leg = self._leg()
        values = {
            "plan_id": "plan-1",
            "proposal_id": "prop-1",
            "strategy_id": "stg-A",
            "account_id": "kite:A",
            "plan_kind": "single_instrument",
            "plan_hash": "h" * 64,
            "resolved_plan": {"legs": [leg]},
            "pinned_catalog_generation": G1,
        }
        if "leg" in overrides:
            values["resolved_plan"] = {"legs": [overrides.pop("leg")]}
        values.update(overrides)
        return values

    def _leg(self):
        return {
            "instrument_id": "inst-REL",
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "broker_exchange": "NSE",
            "broker_symbol": "RELIANCE",
            "broker_token": 100,
            "product": "CNC",
            "signed_quantity": 10,
            "reference_price": 100.0,
        }

    def margin(self, *, usable=10000.0, age_seconds=0.0):
        return {"usable": usable, "as_of": NOW - timedelta(seconds=age_seconds)}

    def policy(self, **overrides):
        values = {"allocation_inr": 1000.0}
        values.update(overrides)
        return self.service.upsert_policy(
            strategy_id="stg-A", account_id="kite:A", updated_by="app:o", **values
        )

    def book(self, quantity, *, canonical_id="inst-REL", token=100, environment="live"):
        """Seed this strategy's attributed book for ONE canonical coordinate.

        Defaults to the plan's own coordinate (``inst-REL``); pass another
        ``canonical_id`` to seed an unrelated held name.
        """
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES ('kite:A', 'stg-A', :env, 'canonical', :key, :key, 'CNC', :token, "
                    " 'NSE', 'RELIANCE', :qty, 1)"
                ),
                {
                    "key": canonical_id,
                    "token": int(token),
                    "qty": int(quantity),
                    "env": str(environment),
                },
            )
            session.commit()

    def publish_state(self, *, at=None, environment="live"):
        """Publish this book (the normal publication marker) at a given instant."""
        moment = at or NOW
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_projection_state "
                    "(account_id, strategy_id, execution_environment, projection_version, "
                    " content_sha256, last_rebuild_at, updated_at) "
                    "VALUES ('kite:A', 'stg-A', :env, 1, 'content-hash', :at, :at) "
                    "ON CONFLICT (account_id, strategy_id, execution_environment) DO UPDATE "
                    "SET last_rebuild_at = EXCLUDED.last_rebuild_at, "
                    "    content_sha256 = EXCLUDED.content_sha256"
                ),
                {"at": moment, "env": str(environment)},
            )
            session.commit()

    def seed_plan_row(self, plan_id, *, strategy_id="stg-A"):
        """Reservations FK to a real plan, so the ledger needs one to point at."""
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES (:pid, :sid, 'kite:A', :pid, 'run_now', 'run-1', "
                    " 'single_instrument', '{}', 'sha', 'validated')"
                ),
                {"pid": f"prop-{plan_id}", "sid": strategy_id},
            )
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation) "
                    "VALUES (:pid, :prop, :sid, 'kite:A', 'single_instrument', 'h', '{}', '{}', :gen)"
                ),
                {"pid": plan_id, "prop": f"prop-{plan_id}", "sid": strategy_id, "gen": G1},
            )
            session.commit()

    def reserve(self, notional, *, status="active", strategy_id="stg-A", plan_id="plan-old"):
        self.seed_plan_row(plan_id, strategy_id=strategy_id)
        return self._reserve(notional, status=status, strategy_id=strategy_id, plan_id=plan_id)

    def _reserve(self, notional, *, status, strategy_id, plan_id, consumed_at=None):
        self.seed_plan_row(plan_id, strategy_id=strategy_id)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_reservations "
                    "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                    " execution_environment, status, reserved_notional_inr, valid_until, created_at) "
                    "VALUES (:rid, :pid, :sid, 'kite:A', 'eval-x', 'live', :status, :notional, "
                    " :valid, :created)"
                ),
                {
                    "rid": f"res-{plan_id}",
                    "pid": plan_id,
                    "sid": strategy_id,
                    "status": status,
                    "notional": float(notional),
                    "valid": NOW + timedelta(hours=1),
                    "created": NOW - timedelta(seconds=30),
                },
            )
            if consumed_at is not None:
                # The IMMUTABLE consumption timestamp: the server-written
                # ``consumed`` event. A reservation row alone carries no proof of
                # when (or whether) its fill landed.
                session.execute(
                    text(
                        "INSERT INTO strategy_reservation_events "
                        "(id, reservation_id, event, actor_id, detail, created_at) "
                        "VALUES (:eid, :rid, 'consumed', 'test', '{}', :at)"
                    ),
                    {"eid": f"evt-{plan_id}", "rid": f"res-{plan_id}", "at": consumed_at},
                )
            session.commit()


class PolicyRequirementTests(AdmissionTestCase):
    def test_live_without_policy_refuses(self):
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.refusal_reason, "ADMISSION_POLICY_MISSING")

    def test_live_policy_without_allocation_refuses(self):
        self.policy(allocation_inr=None)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "ADMISSION_POLICY_MISSING")

    def test_paper_needs_no_policy(self):
        verdict = self.service.evaluate(
            self.plan(), execution_environment="paper", now=NOW,
            paper_funds={"available_funds": 100000.0},
        )
        self.assertTrue(verdict.admitted, verdict.detail)


class AllocationTests(AdmissionTestCase):
    def test_allocation_admits_when_projection_fits(self):
        self.policy(allocation_inr=1000.0)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertTrue(verdict.admitted, verdict.detail)
        self.assertEqual(verdict.detail["plan_requirement_inr"], 1000.0)

    def test_allocation_counts_the_attributed_book(self):
        # The enforced requirement is the INCREMENTAL funding. Growing a held 10
        # to 15 needs only 500 more (not the whole 1500 target), so 1500 admits;
        # growing it to 20 needs 1000 more than the 500 already attributed, so
        # 1000 attributed + 500 pending headroom refuses.
        self.policy(allocation_inr=1500.0)
        self.book(10)
        grow = self.plan(leg={**self._leg(), "signed_quantity": 15})
        verdict = self.service.evaluate(grow, now=NOW, margin_evidence=self.margin())
        self.assertTrue(verdict.admitted, verdict.detail)
        self.assertEqual(verdict.detail["current_exposure_inr"], 1000.0)
        self.assertEqual(verdict.detail["plan_requirement_inr"], 500.0)

        tighter = self.plan(leg={**self._leg(), "signed_quantity": 20})
        refused = self.service.evaluate(tighter, now=NOW, margin_evidence=self.margin())
        self.assertEqual(refused.refusal_reason, "ALLOCATION_EXCEEDED")
        self.assertEqual(refused.detail["attributed_consumption_inr"], 1000.0)

    def test_reapplying_an_unchanged_target_costs_nothing(self):
        # The deployed doubling: a full-target plan re-applying the book it is
        # already holding was charged the whole book again and refused
        # ALLOCATION_EXCEEDED. Re-applying the unchanged target is now a
        # zero-order, zero-additional-capital operation.
        self.policy(allocation_inr=1000.0)
        self.book(10)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertTrue(verdict.admitted, verdict.detail)
        self.assertEqual(verdict.detail["plan_requirement_inr"], 0.0)
        self.assertEqual(verdict.detail["incremental_funding_inr"], 0.0)

    def test_a_sell_only_rebalance_funds_nothing_until_it_fills(self):
        # A removal (held 10 -> target 0) releases no cash in admission: the sell
        # contributes zero incremental funding, and the buys that depend on it
        # must fund themselves.
        self.policy(allocation_inr=1000.0)
        self.book(10)
        removal = self.plan(leg={**self._leg(), "signed_quantity": 0})
        verdict = self.service.evaluate(removal, now=NOW, margin_evidence=self.margin())
        self.assertTrue(verdict.admitted, verdict.detail)
        self.assertEqual(verdict.detail["incremental_funding_inr"], 0.0)
        # And an unfilled sale never becomes headroom for a replacement buy.
        self.reserve(1000.0, status="active", plan_id="plan-a")
        replacement = self.service.evaluate(
            self.plan(leg={**self._leg(), "instrument_id": "inst-REL", "signed_quantity": 10}),
            now=NOW,
            margin_evidence=self.margin(),
        )
        self.assertEqual(replacement.refusal_reason, "ALLOCATION_EXCEEDED")

    def test_allocation_counts_active_reservations_but_not_released_ones(self):
        self.policy(allocation_inr=1500.0)
        self.reserve(600.0, status="active", plan_id="plan-a")
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "ALLOCATION_EXCEEDED")
        self.assertEqual(verdict.detail["active_reserved_inr"], 600.0)

        # A released reservation holds nothing, so the same plan now fits.
        with self.factory() as session:
            session.execute(
                text("UPDATE strategy_reservations SET status='released' WHERE plan_id='plan-a'")
            )
            session.commit()
        self.assertTrue(
            self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin()).admitted
        )

    def test_consumed_capacity_is_history_not_a_second_charge(self):
        # A consumed reservation holds capacity until the exposure it backed is
        # actually VISIBLE in the published attributed book. Only then does the
        # position carry the exposure and stop the reservation being charged a
        # second time.
        self.policy(allocation_inr=1500.0)
        consumed_at = NOW - timedelta(seconds=10)
        self._reserve(600.0, status="consumed", strategy_id="stg-A", plan_id="plan-c", consumed_at=consumed_at)
        # A publication that predates the CONSUMPTION event proves nothing about
        # the fill, so the reservation still holds.
        self.publish_state(at=NOW - timedelta(seconds=20))
        refused = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(refused.refusal_reason, "ALLOCATION_EXCEEDED")
        self.assertEqual(refused.detail["consumed_unpublished_inr"], 600.0)
        self.assertEqual(refused.detail["consumed_published_inr"], 0.0)

        # Published AFTER consumption: the position now carries it.
        self.publish_state(at=NOW)
        allowed = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertTrue(allowed.admitted, allowed.detail)
        self.assertEqual(allowed.detail["consumed_published_inr"], 600.0)
        self.assertEqual(allowed.detail["consumed_unpublished_inr"], 0.0)
        self.assertEqual(allowed.detail["consumed_history_inr"], 600.0)

    def test_a_consumed_reservation_without_a_consumption_event_still_holds(self):
        """The reservation row's ``created_at`` is not consumption evidence."""
        self.policy(allocation_inr=1500.0)
        self.reserve(600.0, status="consumed", plan_id="plan-d")
        self.publish_state(at=NOW)
        refused = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(refused.refusal_reason, "ALLOCATION_EXCEEDED")
        self.assertEqual(refused.detail["consumed_unpublished_inr"], 600.0)

    def test_zero_allocation_is_a_real_limit_not_null(self):
        self.policy(allocation_inr=0.0)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "ALLOCATION_EXCEEDED")

    def test_a_fully_allocated_sell_and_buy_rebalance_is_admitted_but_staged(self):
        """A sell-A / buy-B rebalance on a fully allocated book.

        The budget test is on the POST-PLAN book, so this fits. The funding that
        is not covered by free headroom must come from A's own confirmed release,
        which is reported as a shortfall for the executor to enforce staged -
        never credited as if the projected sale had already paid.
        """
        self.policy(allocation_inr=1000.0)
        self.book(10)  # A: 10 @ 100 = the whole 1000 allocation
        rebalance = self.plan(
            **{
                "resolved_plan": {
                    "legs": [
                        {**self._leg(), "signed_quantity": 0},  # remove A
                        {
                            **self._leg(),
                            "instrument_id": "inst-REL2",
                            "broker_token": 101,
                            "signed_quantity": 10,  # add B for the same notional
                        },
                    ]
                }
            }
        )
        verdict = self.service.evaluate(rebalance, now=NOW, margin_evidence=self.margin())
        self.assertTrue(verdict.admitted, verdict.detail)
        self.assertEqual(verdict.detail["desired_exposure_inr"], 1000.0)
        self.assertEqual(verdict.detail["free_headroom_inr"], 0.0)
        self.assertEqual(verdict.detail["funding_shortfall_inr"], 1000.0)
        self.assertIs(verdict.detail["requires_staged_financing"], True)
        # The pre-plan shape would have refused the same plan (the old doubling).
        self.assertGreater(verdict.detail["pre_plan_projected_inr"], 1000.0)


class OptionalAxisTests(AdmissionTestCase):
    def test_null_axes_are_skipped(self):
        """NULL means not enforced, and it is distinguishable from a limit of 0."""
        # First prove these axes WOULD refuse if configured...
        strict = self.policy(
            allocation_inr=100000.0,
            per_instrument_notional_inr=500.0,
            gross_notional_inr=500.0,
            max_open_instruments=0,
            admissions_per_window=1,
            admission_window_seconds=60,
        )
        refused = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertFalse(refused.admitted)
        self.assertEqual(refused.refusal_reason, "INSTRUMENT_NOTIONAL_EXCEEDED")
        self.assertEqual(strict["per_instrument_notional_inr"], 500.0)

        # ...then the same plan with every optional axis NULL is admitted, with no
        # evidence gathered for them at all.
        self.policy(allocation_inr=100000.0)
        self.book(10)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertTrue(verdict.admitted, verdict.detail)

        # And a configured limit of zero still refuses, so NULL and 0 differ.
        self.policy(allocation_inr=100000.0, max_open_instruments=0)
        zero_limit = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(zero_limit.refusal_reason, "MAX_OPEN_INSTRUMENTS_EXCEEDED")

    def test_per_instrument_notional_axis(self):
        self.policy(allocation_inr=100000.0, per_instrument_notional_inr=500.0)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "INSTRUMENT_NOTIONAL_EXCEEDED")
        self.assertEqual(verdict.detail["worst_leg_notional_inr"], 1000.0)

    def test_gross_notional_axis(self):
        self.policy(allocation_inr=100000.0, gross_notional_inr=1500.0)
        # The post-plan gross includes the unchanged held coordinate AND the leg
        # this plan grows, valued per instrument.
        self.book(15)  # 1500 already held at this coordinate
        grow = self.plan(leg={**self._leg(), "signed_quantity": 20})
        verdict = self.service.evaluate(grow, now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "GROSS_NOTIONAL_EXCEEDED")
        self.assertEqual(verdict.detail["projected_gross_inr"], 2000.0)

    def test_max_open_instruments_axis(self):
        self.policy(allocation_inr=100000.0, max_open_instruments=1)
        # Both post-plan coordinates are priced by the plan, so the refusal is the
        # instrument COUNT and not a valuation gap.
        second = {**self._leg(), "instrument_id": "inst-REL2", "broker_token": 101}
        two_legs = self.plan(**{"resolved_plan": {"legs": [self._leg(), second]}})
        verdict = self.service.evaluate(two_legs, now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "MAX_OPEN_INSTRUMENTS_EXCEEDED")

    def test_an_unpriced_held_coordinate_refuses_instead_of_reading_as_zero(self):
        # A configured notional limit with a held coordinate the plan does not
        # price is unknown evidence: refusing by name is honest, and valuing it as
        # zero would under-state this strategy's own projection.
        self.policy(allocation_inr=100000.0)
        self.book(5, canonical_id="inst-OTHER", token=999)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "POSITION_VALUATION_UNAVAILABLE")
        self.assertEqual(verdict.detail["unvalued"][0]["coordinate"], ["inst-OTHER", "CNC"])

    def test_an_unattributed_raw_fact_refuses_instead_of_being_ignored(self):
        # A raw (unattributed) projection fact is real exposure the platform
        # cannot reconcile to a canonical instrument. It must refuse, not be
        # counted and dropped.
        self.policy(allocation_inr=100000.0)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES ('kite:A', 'stg-A', 'live', 'raw', 'era-1:unknown', NULL, 'CNC', 999, "
                    " 'NSE', 'UNKNOWN', 7, 1)"
                )
            )
            session.commit()
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "POSITION_VALUATION_UNAVAILABLE")
        reasons = {entry["reason"] for entry in verdict.detail["unvalued"]}
        self.assertIn("unresolved_projection_fact", reasons)
        self.assertEqual(verdict.detail["unresolved_projection_facts"], 1)

    def test_a_fully_allocated_rebalance_with_no_free_cash_is_staged_not_refused(self):
        """The zero-free-cash sell-A/buy-B rebalance is ADMITTED, staged.

        The account has no spare cash, so the whole incremental requirement must
        come from this plan's own reduction. Admission records the shortfall and
        admits; it never credits a projected sale, and the executor refuses each
        dependent buy unless the reduction actually confirms.
        """
        self.policy(allocation_inr=20000.0)
        self.book(100, environment="paper")  # 100 x 100 = 10000 of RELIANCE held
        self.publish_state(at=NOW, environment="paper")
        buy_leg = {
            "instrument_id": "inst-INFY",
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "broker_exchange": "NSE",
            "broker_symbol": "INFY",
            "broker_token": 200,
            "product": "CNC",
            "signed_quantity": 100,
            "reference_price": 100.0,
        }
        sell_leg = {**self._leg(), "signed_quantity": 0}

        verdict = self.service.evaluate(
            self.plan(
                plan_kind="intent_bundle",
                resolved_plan={"legs": [sell_leg, buy_leg]},
            ),
            execution_environment="paper",
            now=NOW,
            paper_funds={"available_funds": 0.0},
        )

        self.assertTrue(verdict.admitted, verdict.detail)
        self.assertTrue(verdict.detail["staged_financing_lane"])
        self.assertEqual(verdict.detail["staged_financing_shortfall_inr"], 10000.0)
        self.assertEqual(verdict.detail["account_available_inr"], 0.0)

    def test_a_buy_only_plan_with_no_free_cash_is_still_refused(self):
        """Staging needs a reduction to fund: a naked buy keeps the refusal."""
        self.policy(allocation_inr=20000.0)
        # A genuine INCREASE (flat book, buy 10) with no free cash: there is no
        # reduction to stage it against, so the refusal stands.
        verdict = self.service.evaluate(
            self.plan(plan_kind="intent_bundle"),
            execution_environment="paper",
            now=NOW,
            paper_funds={"available_funds": 0.0},
        )

        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.refusal_reason, "MARGIN_UNAVAILABLE")
        self.assertFalse(verdict.detail["staged_financing_lane"])

    def test_live_staged_rebalance_refuses_by_name(self):
        """Staged sell-before-buy financing is PAPER-only today.

        The live adapter has no confirmed-release authorization path, so a live
        rebalance whose increases are not covered by available margin refuses by
        NAME instead of trading on money the platform has not proved. This is the
        explicit live boundary root accepted, not a silent fallback.
        """
        self.policy(allocation_inr=20000.0)
        self.book(100)
        self.publish_state(at=NOW)
        buy_leg = {
            "instrument_id": "inst-INFY",
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "broker_exchange": "NSE",
            "broker_symbol": "INFY",
            "broker_token": 200,
            "product": "CNC",
            "signed_quantity": 100,
            "reference_price": 100.0,
        }
        sell_leg = {**self._leg(), "signed_quantity": 0}

        verdict = self.service.evaluate(
            self.plan(
                plan_kind="intent_bundle",
                resolved_plan={"legs": [sell_leg, buy_leg]},
            ),
            execution_environment="live",
            now=NOW,
            margin_evidence=self.margin(usable=0.0),
        )

        self.assertFalse(verdict.admitted)
        self.assertEqual(verdict.refusal_reason, "STAGED_LIVE_FINANCING_UNSUPPORTED")
        self.assertTrue(verdict.detail["staged_financing_lane"])

    def test_a_non_cnc_intent_bundle_is_not_staged(self):
        """MIS/NRML bundles must not adopt the CNC portfolio sequencing.

        The same sell-A/buy-B shape in MIS is margined and sequenced by its own
        domain rules, so the generic rule must not classify it as a staged CNC
        rebalance (which would also reorder it).
        """
        self.policy(allocation_inr=20000.0)
        self.book(100, environment="paper")
        self.publish_state(at=NOW, environment="paper")
        mis_buy = {
            "instrument_id": "inst-INFY",
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "broker_exchange": "NSE",
            "broker_symbol": "INFY",
            "broker_token": 200,
            "product": "MIS",
            "signed_quantity": 100,
            "reference_price": 100.0,
        }
        mis_sell = {**self._leg(), "product": "MIS", "signed_quantity": 0}

        verdict = self.service.evaluate(
            self.plan(
                plan_kind="intent_bundle",
                resolved_plan={"legs": [mis_sell, mis_buy]},
            ),
            execution_environment="paper",
            now=NOW,
            paper_funds={"available_funds": 0.0},
        )

        # The classification is the contract under test: a MIS bundle must never
        # be treated as a staged CNC portfolio (which would also reorder it).
        self.assertFalse(verdict.detail["staged_financing_lane"])
        self.assertIsNone(verdict.detail["staged_increase_inr"])
        self.assertFalse(verdict.admitted)

    def test_admission_sizing_matches_the_executor_for_a_lot_floored_weight_leg(self):
        """Admission and the executor must derive the SAME order quantity.

        The financing contract is only coherent if the number admission funds is
        the number the executor will actually send, so this is a deliberate
        cross-check against ``PaperPlanExecutor._plan_steps`` rather than a
        re-assertion of admission's own arithmetic.
        """
        from backend.strategies.execution import PaperPlanExecutor

        self.policy(allocation_inr=100000.0)
        leg = {
            **self._leg(),
            "signed_quantity": None,
            "target_weight": 0.9,
            "reference_price": 100.0,
            "lot_size": 6,
        }
        plan = self.plan(**{"resolved_plan": {"legs": [leg], "capital_basis_inr": 1000.0}})

        exposure = self.service.plan_exposure(plan, execution_environment="live")
        admitted = exposure["per_instrument"][0]

        executor = PaperPlanExecutor(session_factory=self.factory)
        steps = executor._plan_steps(plan, {})
        _index, _step_leg, quantity, _side = steps[0]

        self.assertEqual(quantity, 6, "0.9 x 1000 / 100 = 9 units, floored to a lot of 6")
        self.assertEqual(admitted["order_quantity"], quantity)
        self.assertEqual(admitted["target_quantity"], 6)
        self.assertEqual(exposure["plan_requirement_inr"], 600.0)

    def test_order_rate_axis_counts_prior_admissions_in_window(self):
        self.policy(
            allocation_inr=100000.0, admissions_per_window=2, admission_window_seconds=60
        )
        for index in range(2):
            self.reserve(10.0, status="released", plan_id=f"plan-r{index}")
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "ORDER_RATE_EXCEEDED")
        self.assertEqual(verdict.detail["recent_admissions"], 2)

    def test_order_rate_ignores_admissions_outside_the_window(self):
        self.policy(
            allocation_inr=100000.0, admissions_per_window=2, admission_window_seconds=60
        )
        self.seed_plan_row("plan-old")
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_reservations "
                    "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                    " execution_environment, status, reserved_notional_inr, valid_until, created_at) "
                    "VALUES ('res-old', 'plan-old', 'stg-A', 'kite:A', 'e', 'live', 'released', 10, "
                    " :valid, :created)"
                ),
                {"valid": NOW, "created": NOW - timedelta(hours=5)},
            )
            session.commit()
        self.assertTrue(
            self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin()).admitted
        )

    # -- option adjustments own their whole post-plan book ------------------

    def _option_adjust_plan(self, *, new_price=100.0):
        """An ADJUST whose desired state is one NEW coordinate, not the held one."""
        new_leg = {
            "instrument_id": "inst-NEW",
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26NOV22500CE",
            "product": "NRML",
            "signed_quantity": -50,
            "reference_price": new_price,
            "lot_size": 50,
            "strike": 22500.0,
            "option_type": "CE",
        }
        return self.plan(
            plan_kind="option_structure",
            resolved_plan={
                "legs": [new_leg],
                "option_run": {"phase": "adjust", "option_run_id": "run-opt", "based_on_generation": 1},
            },
        )

    def _book_option_leg(self, canonical_id, quantity):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES ('kite:A', 'stg-A', 'live', 'canonical', :key, :key, 'NRML', 700, "
                    " 'NFO', 'NIFTY26OCT22500CE', :qty, 1)"
                ),
                {"key": canonical_id, "qty": int(quantity)},
            )
            session.commit()

    def test_an_option_adjust_releases_the_legs_its_target_does_not_name(self):
        """A roll's OLD generation is released, not 'held unchanged'.

        Leaving it in the post-plan book made the roll refuse
        POSITION_VALUATION_UNAVAILABLE: the released legs carry no reference price
        in the plan that closes them.
        """
        self.policy(allocation_inr=100000.0)
        self._book_option_leg("inst-OLD", -50)
        self.publish_state()
        plan = self._option_adjust_plan()

        exposure = self.service.plan_exposure(plan, execution_environment="live")

        assert exposure["unvalued"] == []
        released = [
            row for row in exposure["per_instrument"] if row["coordinate"][0] == "inst-OLD"
        ]
        assert released, exposure["per_instrument"]
        assert released[0]["target_quantity"] == 0
        assert released[0]["order_quantity"] == 50
        assert released[0]["increases_exposure"] is False
        # Only the leg the target names survives into the post-plan book.
        assert exposure["post_instruments"] == 1
        verdict = self.service.evaluate(plan, now=NOW, margin_evidence=self.margin())
        assert verdict.admitted, verdict.detail

    def test_an_option_adjust_that_names_the_held_coordinate_keeps_it(self):
        """The release rule zeroes only the coordinates the target OMITS: a
        coordinate the desired state names is still the resize the plan describes."""
        self.policy(allocation_inr=100000.0)
        self._book_option_leg("inst-OLD", -50)
        self.publish_state()
        plan = self._option_adjust_plan()
        plan["resolved_plan"]["legs"][0]["instrument_id"] = "inst-OLD"

        exposure = self.service.plan_exposure(plan, execution_environment="live")

        held = [
            row for row in exposure["per_instrument"] if row["coordinate"][0] == "inst-OLD"
        ]
        assert held and held[0]["target_quantity"] == -50
        assert held[0]["order_quantity"] == 0
        assert exposure["post_instruments"] == 1


class FailClosedTests(AdmissionTestCase):
    def test_live_daily_loss_budget_refuses_as_unavailable(self):
        # The honest V1 behaviour: there is no attributed live realized-loss
        # source, so a configured budget refuses rather than silently not applying.
        self.policy(allocation_inr=100000.0, daily_loss_budget_inr=5000.0)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "DAILY_LOSS_BUDGET_UNAVAILABLE")

    def test_paper_daily_loss_budget_is_enforced_from_paper_evidence(self):
        self.policy(allocation_inr=None, daily_loss_budget_inr=500.0)
        under = self.service.evaluate(
            self.plan(), execution_environment="paper", now=NOW,
            paper_funds={"available_funds": 100000.0}, realized_loss_inr=100.0,
        )
        self.assertTrue(under.admitted, under.detail)
        over = self.service.evaluate(
            self.plan(), execution_environment="paper", now=NOW,
            paper_funds={"available_funds": 100000.0}, realized_loss_inr=900.0,
        )
        self.assertEqual(over.refusal_reason, "DAILY_LOSS_BUDGET_UNAVAILABLE")

    def test_missing_margin_evidence_refuses(self):
        self.policy(allocation_inr=100000.0)
        verdict = self.service.evaluate(self.plan(), now=NOW)
        self.assertEqual(verdict.refusal_reason, "MARGIN_UNAVAILABLE")

    def test_stale_margin_quote_refuses(self):
        self.policy(allocation_inr=100000.0)
        verdict = self.service.evaluate(
            self.plan(), now=NOW, margin_evidence=self.margin(age_seconds=120)
        )
        self.assertEqual(verdict.refusal_reason, "MARGIN_QUOTE_STALE")

    def test_insufficient_margin_refuses(self):
        self.policy(allocation_inr=100000.0)
        verdict = self.service.evaluate(
            self.plan(), now=NOW, margin_evidence=self.margin(usable=10.0)
        )
        self.assertEqual(verdict.refusal_reason, "MARGIN_UNAVAILABLE")

    def test_reference_price_missing_refuses_when_a_notional_axis_is_configured(self):
        self.policy(allocation_inr=100000.0)
        leg = dict(self.plan()["resolved_plan"]["legs"][0])
        leg.pop("reference_price")
        verdict = self.service.evaluate(self.plan(leg=leg), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "REFERENCE_PRICE_UNAVAILABLE")
        self.assertEqual(verdict.detail["missing_for"], ["RELIANCE"])

    def test_catalog_invalid_refuses_and_unrelated_change_does_not(self):
        from backend.strategies.proposals import plan_invalidation_state

        self.policy(allocation_inr=100000.0)
        plan = self.plan()
        # An unrelated newer generation must NOT refuse (Phase 3 derived rule).
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES ('gen-2', 'published', '2026-09-10T00:00:00+00:00')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, current_generation_id) "
                    "VALUES ('inst-OTHER', 'NSE', 'INFY', 'active', 'gen-2')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES ('map-OTHER', 'inst-OTHER', 'kite', 'NSE', 'INFY', 200, 'gen-2', 1)"
                )
            )
            session.commit()

        state = plan_invalidation_state(plan, session_factory=self.factory)
        self.assertEqual(state["state"], "valid")
        self.assertTrue(
            self.service.evaluate(plan, now=NOW, margin_evidence=self.margin()).admitted
        )

        # Re-mapping the PINNED instrument refuses.
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE public.instrument_broker_mappings SET is_current = 0, "
                    "valid_to_generation = 'gen-2' WHERE instrument_id = 'inst-REL'"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, current_generation_id) "
                    "VALUES ('inst-REL-NEW', 'NSE', 'RELIANCE', 'active', 'gen-2')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES ('map-REL2', 'inst-REL-NEW', 'kite', 'NSE', 'RELIANCE', 100, 'gen-2', 1)"
                )
            )
            session.commit()
        verdict = self.service.evaluate(plan, now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "CATALOG_INVALID")


class ProductTests(AdmissionTestCase):
    def test_unknown_product_refuses(self):
        self.policy(allocation_inr=100000.0)
        leg = dict(self.plan()["resolved_plan"]["legs"][0], product="BO")
        verdict = self.service.evaluate(self.plan(leg=leg), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "SESSION_PRODUCT_INVALID")
        self.assertEqual(verdict.detail["invalid_products"], ["BO"])

    def test_known_products_admit(self):
        self.policy(allocation_inr=100000.0)
        for product in ("CNC", "MIS", "NRML"):
            leg = dict(self.plan()["resolved_plan"]["legs"][0], product=product)
            verdict = self.service.evaluate(
                self.plan(leg=leg), now=NOW, margin_evidence=self.margin()
            )
            self.assertTrue(verdict.admitted, (product, verdict.detail))


class OrderingTests(AdmissionTestCase):
    """The order is the contract: the first refusal wins, deterministically."""

    def test_first_refusal_wins(self):
        # No policy AND no margin: the policy check runs first.
        verdict = self.service.evaluate(self.plan(), now=NOW)
        self.assertEqual(verdict.refusal_reason, "ADMISSION_POLICY_MISSING")

        # Policy present, allocation blown AND margin missing: allocation wins.
        self.policy(allocation_inr=10.0)
        verdict = self.service.evaluate(self.plan(), now=NOW)
        self.assertEqual(verdict.refusal_reason, "ALLOCATION_EXCEEDED")

        # Allocation fine, catalog invalid AND margin missing: catalog wins.
        self.policy(allocation_inr=100000.0)
        verdict = self.service.evaluate(
            self.plan(), now=NOW, catalog_state={"state": "invalidated", "reason": "COORDINATE_REMAPPED"}
        )
        self.assertEqual(verdict.refusal_reason, "CATALOG_INVALID")

        # Catalog fine, product invalid AND margin missing: product wins.
        leg = dict(self.plan()["resolved_plan"]["legs"][0], product="BO")
        verdict = self.service.evaluate(self.plan(leg=leg), now=NOW)
        self.assertEqual(verdict.refusal_reason, "SESSION_PRODUCT_INVALID")

        # Everything fine except margin: margin is last, and it still refuses.
        verdict = self.service.evaluate(self.plan(), now=NOW)
        self.assertEqual(verdict.refusal_reason, "MARGIN_UNAVAILABLE")

    def test_verdict_is_repeatable(self):
        self.policy(allocation_inr=100000.0)
        first = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        second = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(first.as_dict(), second.as_dict())


if __name__ == "__main__":
    unittest.main()
