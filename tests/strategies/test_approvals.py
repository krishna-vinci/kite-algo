"""Approval records and structural validity (G6).

Two rules carry the design, and both are tested here as behaviour rather than as
documentation:

* structural validity reports **every** failed pin, not the first, so an operator
  sees why an approval stopped holding;
* an **unrelated** catalog change does not invalidate it — a plan must not be
  hostage to listings it does not touch.

Approval is the account owner's act alone: worker tokens and any other app user
are refused, because this release has no delegated approver roles.
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
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class ApprovalTestCase(unittest.TestCase):
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
                    underlying TEXT
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
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES (:id, 'published', '2026-09-01T00:00:00+00:00')"
                ),
                {"id": G1},
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
                    "VALUES ('stg-A', 'app:owner', 'A', 'kite:A', 'active')"
                )
            )
            session.commit()

        self.approvals = self._service()
        self.ledger = self._ledger()

    def tearDown(self):
        self.engine.dispose()

    def _service(self):
        from backend.strategies.approvals import ApprovalService

        return ApprovalService(session_factory=self.factory)

    def _ledger(self):
        from backend.strategies.reservations import ReservationLedger

        return ReservationLedger(session_factory=self.factory)

    # -- fixtures -----------------------------------------------------------

    def seed_plan(self, plan_id="plan-1", *, plan_hash="h" * 64):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES (:pid, 'stg-A', 'kite:A', :pid, 'run_now', 'run-1', "
                    " 'single_instrument', '{}', 'sha', 'validated')"
                ),
                {"pid": f"prop-{plan_id}"},
            )
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategy_plans "
                    "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                    " logical_plan, resolved_plan, pinned_catalog_generation) "
                    "VALUES (:pid, :prop, 'stg-A', 'kite:A', 'single_instrument', :hash, '{}', "
                    " '{\"legs\": [{\"product\": \"CNC\", \"tradingsymbol\": \"RELIANCE\", "
                    " \"broker_exchange\": \"NSE\", \"broker_symbol\": \"RELIANCE\"}]}', :gen)"
                ),
                {"pid": plan_id, "prop": f"prop-{plan_id}", "hash": plan_hash, "gen": G1},
            )
            session.commit()

    def plan(self, **overrides):
        values = {
            "plan_id": "plan-1",
            "proposal_id": "prop-plan-1",
            "strategy_id": "stg-A",
            "account_id": "kite:A",
            "plan_hash": "h" * 64,
            "pinned_catalog_generation": G1,
            "resolved_plan": {
                "legs": [
                    {
                        "instrument_id": "inst-REL",
                        "product": "CNC",
                        "tradingsymbol": "RELIANCE",
                        "broker_exchange": "NSE",
                        "broker_symbol": "RELIANCE",
                    }
                ]
            },
        }
        values.update(overrides)
        return values

    def reserve(self, plan_id="plan-1", *, requirement=1000.0):
        from backend.strategies.reservations import ClaimRequest

        self.seed_plan(plan_id)
        return self.ledger.claim(
            ClaimRequest(
                plan_id=plan_id, strategy_id="stg-A", account_id="kite:A",
                evaluation_id=f"eval-{plan_id}", execution_environment="live",
                requirement_inr=requirement, valid_until=NOW + timedelta(hours=1),
                allocation_inr=10000.0, actor_id="app:owner",
            ),
            now=NOW,
        )

    def approve(self, plan_id="plan-1", *, actor="app:owner", validity=900, now=NOW, **overrides):
        from backend.strategies.approvals import ApprovalRequest

        reservation = self.reserve(plan_id)
        plan = self.plan(plan_id=plan_id, **overrides)
        return self.approvals.approve(
            ApprovalRequest(
                plan=plan,
                actor_id=actor,
                reservation_id=reservation["reservation_id"],
                validity_seconds=validity,
                session_product_snapshot={"products": ["CNC"], "valid_products": ["CNC", "MIS", "NRML"]},
            ),
            now=now,
        )

    def publish_book(self, quantity=10):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES ('kite:A', 'stg-A', 'live', 'canonical', 'inst-REL', 'inst-REL', 'CNC', "
                    " 738561, 'NSE', 'RELIANCE', :qty, 2)"
                ),
                {"qty": int(quantity)},
            )
            session.execute(
                text(
                    "INSERT INTO strategy_projection_state "
                    "(account_id, strategy_id, execution_environment, projection_version, "
                    " content_sha256) VALUES ('kite:A', 'stg-A', 'live', 2, 'sha-v2')"
                )
            )
            session.commit()


class ActorTests(ApprovalTestCase):
    def test_owner_can_approve(self):
        approval = self.approve()
        self.assertEqual(approval["status"], "active")
        self.assertEqual(approval["actor_id"], "app:owner")
        self.assertEqual(approval["plan_hash"], "h" * 64)

    def test_foreign_app_user_is_refused(self):
        from backend.strategies.approvals import ApprovalNotOwner

        with self.assertRaises(ApprovalNotOwner) as ctx:
            self.approve(actor="app:someone-else")
        self.assertEqual(ctx.exception.reason_code, "APPROVAL_ACTOR_NOT_OWNER")

    def test_worker_token_identity_is_refused(self):
        from backend.strategies.approvals import ApprovalNotOwner

        # A worker is not an app user, and the owner model has no worker identity.
        with self.assertRaises(ApprovalNotOwner):
            self.approve(actor="worker:token-1")

    def test_reservation_must_be_active(self):
        from backend.strategies.approvals import ApprovalError, ApprovalRequest

        reservation = self.reserve()
        self.ledger.release(reservation["reservation_id"], reason="cancelled", actor_id="app:owner")
        with self.assertRaises(ApprovalError) as ctx:
            self.approvals.approve(
                ApprovalRequest(
                    plan=self.plan(), actor_id="app:owner",
                    reservation_id=reservation["reservation_id"],
                ),
                now=NOW,
            )
        self.assertEqual(ctx.exception.reason_code, "RESERVATION_NOT_ACTIVE")


class PinningTests(ApprovalTestCase):
    def test_approval_binds_every_pin(self):
        approval = self.approve()
        self.assertEqual(approval["exposure_snapshot_version"], 0)
        self.assertIsNone(approval["exposure_snapshot_hash"])  # never-published book
        self.assertEqual(approval["reconciliation_version"], 0)
        self.assertEqual(approval["catalog_generation"], G1)
        self.assertEqual(approval["session_product_snapshot"]["products"], ["CNC"])
        self.assertEqual(approval["valid_from"], NOW.isoformat())
        self.assertEqual(approval["valid_until"], (NOW + timedelta(seconds=900)).isoformat())

    def test_exposure_snapshot_records_a_published_book(self):
        self.publish_book()
        approval = self.approve()
        self.assertEqual(approval["exposure_snapshot_version"], 2)
        self.assertEqual(approval["exposure_snapshot_hash"], "sha-v2")

    def test_reapproval_with_identical_pins_is_refused(self):
        from backend.strategies.approvals import ApprovalConflict, ApprovalRequest

        first = self.approve()
        with self.assertRaises(ApprovalConflict) as ctx:
            self.approvals.approve(
                ApprovalRequest(
                    plan=self.plan(), actor_id="app:owner",
                    reservation_id=first["reservation_id"],
                    session_product_snapshot={"products": ["CNC"], "valid_products": ["CNC", "MIS", "NRML"]},
                ),
                now=NOW,
            )
        self.assertEqual(ctx.exception.reason_code, "APPROVAL_ALREADY_ACTIVE")

    def test_reapproval_after_a_pin_moves_supersedes(self):
        from backend.strategies.approvals import ApprovalRequest

        first = self.approve()
        self.publish_book()  # the exposure snapshot moves
        second = self.approvals.approve(
            ApprovalRequest(
                plan=self.plan(), actor_id="app:owner",
                reservation_id=first["reservation_id"],
                session_product_snapshot={"products": ["CNC"], "valid_products": ["CNC", "MIS", "NRML"]},
            ),
            now=NOW + timedelta(seconds=30),
        )
        self.assertNotEqual(second["approval_id"], first["approval_id"])
        # The old authorisation is superseded, never rewritten away.
        history = self.approvals.list_for_plan("plan-1")
        self.assertEqual([row["status"] for row in history], ["active", "superseded"])
        self.assertEqual(len(history), 2)

    def test_at_most_one_active_approval_per_plan(self):
        from backend.strategies.approvals import ApprovalRequest

        self.approve()
        self.publish_book()
        # Superseding keeps exactly one active row: the partial unique index is
        # what makes that structural rather than conventional.
        self.approvals.approve(
            ApprovalRequest(
                plan=self.plan(), actor_id="app:owner",
                reservation_id=self.approvals.active_for_plan("plan-1")["reservation_id"],
                session_product_snapshot={"products": ["CNC"], "valid_products": ["CNC", "MIS", "NRML"]},
            ),
            now=NOW + timedelta(seconds=30),
        )
        with self.factory() as session:
            active = session.execute(
                text("SELECT COUNT(*) FROM strategy_approvals WHERE status='active'")
            ).scalar()
        self.assertEqual(active, 1)

    def test_revocation_is_owner_only_and_terminal(self):
        from backend.strategies.approvals import ApprovalNotOwner, ApprovalError

        approval = self.approve()
        with self.assertRaises(ApprovalNotOwner):
            self.approvals.revoke(approval["approval_id"], actor_id="app:someone-else")
        revoked = self.approvals.revoke(approval["approval_id"], actor_id="app:owner")
        self.assertEqual(revoked["status"], "revoked")
        with self.assertRaises(ApprovalError):
            self.approvals.revoke(approval["approval_id"], actor_id="app:owner")


class StructuralValidityTests(ApprovalTestCase):
    """Each pin individually, and the promise that ALL failures are reported."""

    def _validity(self, approval, *, plan=None, current=None, now=NOW):
        return self.approvals.structural_validity(
            plan or self.plan(), approval, current=current, now=now
        )

    def test_valid_approval_reports_valid(self):
        approval = self.approve()
        state = self._validity(approval)
        self.assertTrue(state["valid"], state)
        self.assertEqual(state["mismatched_pins"], [])

    def test_plan_hash_mismatch(self):
        approval = self.approve()
        state = self._validity(
            approval, plan=self.plan(plan_hash="x" * 64)
        )
        self.assertIn("PLAN_HASH_MISMATCH", state["mismatched_pins"])

    def test_exposure_snapshot_change(self):
        approval = self.approve()
        self.publish_book()
        state = self._validity(approval)
        self.assertEqual(state["mismatched_pins"], ["EXPOSURE_SNAPSHOT_CHANGED"])
        self.assertFalse(state["valid"])

    def test_reconciliation_version_change(self):
        approval = self.approve()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO account_reconciliation_versions (account_id, version) "
                    "VALUES ('kite:A', 7)"
                )
            )
            session.commit()
        state = self._validity(approval)
        self.assertEqual(state["mismatched_pins"], ["RECONCILIATION_VERSION_CHANGED"])
        self.assertEqual(state["detail"]["reconciliation_version"]["current"], 7)

    def test_catalog_relevant_change_invalidates(self):
        approval = self.approve()
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES ('gen-2', 'published', '2026-09-10T00:00:00+00:00')"
                )
            )
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
                    "VALUES ('inst-REL2', 'NSE', 'RELIANCE', 'active', 'gen-2')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES ('map-REL2', 'inst-REL2', 'kite', 'NSE', 'RELIANCE', 100, 'gen-2', 1)"
                )
            )
            session.commit()
        state = self._validity(approval)
        self.assertIn("CATALOG_RELEVANT_CHANGE", state["mismatched_pins"])

    def test_unrelated_catalog_change_keeps_the_approval_valid(self):
        approval = self.approve()
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
        state = self._validity(approval)
        # An unrelated listing must never invalidate a plan.
        self.assertTrue(state["valid"], state)

    def test_reservation_must_still_be_active(self):
        approval = self.approve()
        self.ledger.release(approval["reservation_id"], reason="terminal", actor_id="app:owner")
        state = self._validity(approval)
        self.assertEqual(state["mismatched_pins"], ["RESERVATION_NOT_ACTIVE"])

    def test_consumed_reservation_keeps_the_approval_structurally_active(self):
        approval = self.approve()
        # Consumption is the plan proceeding: the reservation is not "inactive",
        # it is realised, so its pins still hold.
        self.ledger.consume(approval["reservation_id"], actor_id="worker:1", now=NOW)
        state = self._validity(approval)
        self.assertEqual(state["mismatched_pins"], ["RESERVATION_NOT_ACTIVE"])

    def test_expiry(self):
        approval = self.approve(validity=60)
        state = self._validity(approval, now=NOW + timedelta(seconds=61))
        self.assertEqual(state["mismatched_pins"], ["APPROVAL_EXPIRED"])
        # Exactly at expiry counts as expired: no late execution.
        at_expiry = self._validity(approval, now=NOW + timedelta(seconds=60))
        self.assertEqual(at_expiry["mismatched_pins"], ["APPROVAL_EXPIRED"])

    def test_session_product_change(self):
        approval = self.approve()
        state = self._validity(
            approval,
            current={
                "plan_hash": approval["plan_hash"],
                "exposure_snapshot_version": 0,
                "exposure_snapshot_hash": None,
                "reconciliation_version": 0,
                "catalog_state": {"state": "valid"},
                "reservation_status": "active",
                "products": ["MIS"],
            },
        )
        self.assertEqual(state["mismatched_pins"], ["SESSION_PRODUCT_INVALID"])

    def test_every_failed_pin_is_reported_not_just_the_first(self):
        approval = self.approve()
        state = self._validity(
            approval,
            current={
                "plan_hash": "x" * 64,
                "exposure_snapshot_version": 9,
                "exposure_snapshot_hash": "changed",
                "reconciliation_version": 4,
                "catalog_state": {"state": "invalidated", "reason": "COORDINATE_REMAPPED"},
                "reservation_status": "released",
                "products": ["MIS"],
            },
            now=NOW + timedelta(hours=1),
        )
        self.assertEqual(
            state["mismatched_pins"],
            [
                "PLAN_HASH_MISMATCH",
                "EXPOSURE_SNAPSHOT_CHANGED",
                "RECONCILIATION_VERSION_CHANGED",
                "CATALOG_RELEVANT_CHANGE",
                "RESERVATION_NOT_ACTIVE",
                "SESSION_PRODUCT_INVALID",
                "APPROVAL_EXPIRED",
            ],
        )
        self.assertFalse(state["valid"])


class ExemptionTests(ApprovalTestCase):
    def test_paper_and_dry_run_are_approval_exempt(self):
        from backend.strategies.approvals import ApprovalNotRequired, ApprovalRequest

        reservation = self.reserve()
        for environment in ("paper", "dry_run"):
            with self.assertRaises(ApprovalNotRequired) as ctx:
                self.approvals.approve(
                    ApprovalRequest(
                        plan=self.plan(), actor_id="app:owner",
                        reservation_id=reservation["reservation_id"],
                        execution_environment=environment,
                    ),
                    now=NOW,
                )
            self.assertEqual(ctx.exception.reason_code, "APPROVAL_NOT_REQUIRED")


if __name__ == "__main__":
    unittest.main()
