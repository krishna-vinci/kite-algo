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
                    current_generation_id TEXT
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
        leg = {
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

    def margin(self, *, usable=10000.0, age_seconds=0.0):
        return {"usable": usable, "as_of": NOW - timedelta(seconds=age_seconds)}

    def policy(self, **overrides):
        values = {"allocation_inr": 1000.0}
        values.update(overrides)
        return self.service.upsert_policy(
            strategy_id="stg-A", account_id="kite:A", updated_by="app:o", **values
        )

    def book(self, quantity, *, price=None, token=738561):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES ('kite:A', 'stg-A', 'live', 'canonical', :key, :key, 'CNC', :token, "
                    " 'NSE', 'RELIANCE', :qty, 1)"
                ),
                {"key": f"inst-{token}", "token": token, "qty": int(quantity)},
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
        # 10 units at 100 = 1000 already attributed; a new 1000 cannot fit in 1500.
        self.policy(allocation_inr=1500.0)
        self.book(10)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "ALLOCATION_EXCEEDED")
        self.assertEqual(verdict.detail["attributed_consumption_inr"], 1000.0)

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

    def test_consumed_capacity_is_never_released(self):
        # Capital backing an open position counts against allocation forever.
        self.policy(allocation_inr=1500.0)
        self.reserve(600.0, status="consumed", plan_id="plan-c")
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "ALLOCATION_EXCEEDED")
        self.assertEqual(verdict.detail["active_reserved_inr"], 600.0)

    def test_zero_allocation_is_a_real_limit_not_null(self):
        self.policy(allocation_inr=0.0)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "ALLOCATION_EXCEEDED")


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
        self.book(10, token=999)
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
        self.book(10)  # 1000 already gross
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "GROSS_NOTIONAL_EXCEEDED")
        self.assertEqual(verdict.detail["projected_gross_inr"], 2000.0)

    def test_max_open_instruments_axis(self):
        self.policy(allocation_inr=100000.0, max_open_instruments=1)
        self.book(10, token=999)
        verdict = self.service.evaluate(self.plan(), now=NOW, margin_evidence=self.margin())
        self.assertEqual(verdict.refusal_reason, "MAX_OPEN_INSTRUMENTS_EXCEEDED")

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
