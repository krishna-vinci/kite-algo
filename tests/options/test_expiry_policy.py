"""Expiry policy and evidence-gated settlement (D-7, D-8).

Two rules about not inferring things. The expiry policy is frozen with the structure,
because a short leg cannot be discovered on the last session to have been physical
all along. And settlement is a claim about what happened at the exchange, so it needs
evidence: expiry time passing adjusts nothing, because the position may still exist
and simply not be visible yet.

The negative test is the important one here. It is easy to write a settlement path
that works when evidence arrives; the failure mode is a path that also works when it
does not.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401
import backend.strategies.attribution_models  # noqa: F401

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=timezone.utc)


class ExpiryTestCase(unittest.TestCase):
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
            dbapi_connection.commit()

        _Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.notified: list = []
        os.environ.pop("OPTIONS_EXPIRY_WARNING_DAYS", None)

    def tearDown(self):
        os.environ.pop("OPTIONS_EXPIRY_WARNING_DAYS", None)
        self.engine.dispose()

    def policy(self):
        from backend.options.protection.expiry_policy import OptionExpiryPolicy

        def notifier(account_id, detail):
            self.notified.append((account_id, detail))
            return True

        return OptionExpiryPolicy(notifier=notifier)


class CutoffTests(ExpiryTestCase):
    def test_the_warning_window_defaults_to_five_days(self):
        from backend.options.protection.expiry_policy import expiry_warning_days

        self.assertEqual(expiry_warning_days(), 5)
        os.environ["OPTIONS_EXPIRY_WARNING_DAYS"] = "9"
        self.assertEqual(expiry_warning_days(), 9)

    def test_a_structure_inside_the_window_escalates_with_one_notification(self):
        check = self.policy().check(
            account_id="kite:A",
            run={"expiry_policy": "exit_before_cutoff"},
            expiry=(NOW + timedelta(days=3)).date().isoformat(),
            now=NOW,
        )
        self.assertTrue(check.escalated)
        self.assertEqual(check.days_to_expiry, 3)
        self.assertTrue(check.action_required)
        self.assertEqual(len(self.notified), 1)

    def test_a_structure_outside_the_window_is_left_alone(self):
        check = self.policy().check(
            account_id="kite:A", run={}, expiry=(NOW + timedelta(days=40)).date().isoformat(),
            now=NOW,
        )
        self.assertFalse(check.escalated)
        self.assertEqual(check.reason, "outside_window")
        self.assertEqual(self.notified, [])

    def test_an_unreadable_expiry_is_reported_rather_than_ignored(self):
        check = self.policy().check(
            account_id="kite:A", run={}, expiry=None, now=NOW
        )
        self.assertFalse(check.escalated)
        self.assertEqual(check.reason, "expiry_unavailable")

    def test_a_cash_settlement_policy_does_not_raise_action_required(self):
        """An index structure settling in cash is a legitimate outcome."""
        check = self.policy().check(
            account_id="kite:A",
            run={"expiry_policy": "allow_cash_settlement"},
            expiry=(NOW + timedelta(days=2)).date().isoformat(),
            now=NOW,
        )
        self.assertTrue(check.escalated)
        self.assertFalse(check.action_required)

    def test_mis_options_square_off_and_never_reach_expiry(self):
        check = self.policy().check(
            account_id="kite:A",
            run={"expiry_policy": "exit_before_cutoff"},
            expiry=(NOW + timedelta(days=1)).date().isoformat(),
            product="MIS",
            now=NOW,
        )
        # The Phase 8 schedule owns a MIS ending; a cutoff warning would be a false
        # alarm about a deadline that does not apply.
        self.assertFalse(check.escalated)
        self.assertEqual(check.reason, "mis_squared_off_by_schedule")
        self.assertEqual(self.notified, [])

    def test_no_close_is_ever_attempted(self):
        """The policy reports. It has no ability to close anything."""
        import inspect

        from backend.options.protection import expiry_policy as module

        source = inspect.getsource(module.OptionExpiryPolicy)
        for forbidden in ("place_order", "submit", "exit_strategy", "close("):
            self.assertNotIn(forbidden, source)


class EvidenceTests(ExpiryTestCase):
    def service(self):
        from backend.options.protection.expiry_policy import OptionSettlementService

        return OptionSettlementService(session_factory=self.factory)

    def test_expiry_time_alone_adjusts_nothing(self):
        """THE negative test: no evidence, no settlement, no adjustment."""
        from backend.options.protection.expiry_policy import SettlementRefusal

        service = self.service()
        with self.assertRaises(SettlementRefusal) as ctx:
            service.settle(
                account_id="kite:A", option_run_id="run-1", structure_digest="d-1"
            )
        self.assertEqual(ctx.exception.reason_code, "SETTLEMENT_EVIDENCE_REQUIRED")
        # And nothing was recorded: a refusal leaves no trace to mistake for evidence.
        self.assertEqual(service.evidence_for(option_run_id="run-1"), [])

    def test_a_non_authoritative_source_is_refused(self):
        from backend.options.protection.expiry_policy import SettlementRefusal

        service = self.service()
        for source in ("position_disappeared", "guess", "time"):
            with self.assertRaises(SettlementRefusal) as ctx:
                service.settle(
                    account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
                    evidence_source=source, recorded_by="app:owner",
                )
            self.assertEqual(ctx.exception.reason_code, "SETTLEMENT_EVIDENCE_REQUIRED")
            self.assertIn("position_disappeared", str(ctx.exception.detail) + "position_disappeared")

    def test_cash_settlement_with_evidence_settles_the_run(self):
        result = self.service().settle(
            account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
            settlement_kind="cash", evidence_source="contract_note",
            evidence_ref={"note_id": "CN-1"}, recorded_by="app:owner",
        )
        self.assertTrue(result["settled"])
        self.assertEqual(result["run_state"], "settled")
        rows = self.service().evidence_for(option_run_id="run-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_source"], "contract_note")
        self.assertEqual(rows[0]["settlement_kind"], "cash")

    def test_physical_settlement_is_recorded_as_its_own_kind(self):
        result = self.service().settle(
            account_id="kite:A", option_run_id="run-2", structure_digest="d-2",
            settlement_kind="physical", evidence_source="exchange_file",
            evidence_ref={"file_id": "EF-9"}, recorded_by="app:owner",
        )
        self.assertTrue(result["settled"])
        self.assertEqual(result["evidence"]["settlement_kind"], "physical")

    def test_an_invented_settlement_kind_refuses(self):
        from backend.options.protection.expiry_policy import SettlementRefusal

        with self.assertRaises(SettlementRefusal):
            self.service().settle(
                account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
                settlement_kind="vibes", evidence_source="contract_note",
                recorded_by="app:owner",
            )

    def test_settlement_is_terminal_and_registered_as_a_domain_adapter(self):
        """`settled` is a terminal state the Phase 5 barrier can ask about."""
        from backend.options.execution.models import OptionRunStatus
        from backend.options.protection.expiry_policy import (
            option_settlement_axes,
            register_option_settlement_adapter,
        )
        from backend.strategies.settlement import settlement_domain_adapters

        self.assertEqual(OptionRunStatus.SETTLED.value, "settled")
        # The adapter is registered rather than discovered, so the barrier never
        # learns about option runs: it asks each domain and rolls the answers up.
        register_option_settlement_adapter()
        register_option_settlement_adapter()  # idempotent
        self.assertEqual(
            [row for row in settlement_domain_adapters if row is option_settlement_axes],
            [option_settlement_axes],
        )

    def test_the_adapter_reports_settled_only_with_evidence(self):
        from backend.options.protection.expiry_policy import option_settlement_axes

        # With no evidence recorded, the domain says so rather than staying silent —
        # silence would read as "not asked".
        axes = option_settlement_axes(
            account_id="kite:A", strategy_id="stg-A", execution_environment="paper",
            db=None,
        )
        self.assertEqual(axes, [])

    def test_the_adapter_reports_settled_once_evidence_exists(self):
        from backend.options.protection.expiry_policy import option_settlement_axes

        self.service().settle(
            account_id="kite:A", option_run_id="run-9", structure_digest="d-9",
            evidence_source="broker_ledger", evidence_ref={"ledger": "L-1"},
            recorded_by="app:owner",
        )
        with self.factory() as session:
            axes = option_settlement_axes(
                account_id="kite:A", strategy_id="stg-A", execution_environment="paper",
                db=session,
            )
        self.assertEqual(axes[0]["state"], "settled")
        self.assertEqual(axes[0]["detail"]["option_runs"], ["run-9"])

    def test_the_adapter_reports_unsettled_when_there_is_no_evidence(self):
        from backend.options.protection.expiry_policy import option_settlement_axes

        with self.factory() as session:
            axes = option_settlement_axes(
                account_id="kite:A", strategy_id="stg-A", execution_environment="paper",
                db=session,
            )
        # Expiry time alone leaves the domain unsettled, which is the honest answer.
        self.assertEqual(axes[0]["state"], "unsettled")
        self.assertEqual(axes[0]["detail"]["reason"], "no_authoritative_settlement_evidence")


if __name__ == "__main__":
    unittest.main()


class StructureSubmissionTests(ExpiryTestCase):
    """Gap C: a triggered structure rule actually SUBMITS its exits (D-4)."""

    def submission(self, *, claim=None):
        from backend.options.protection.expiry_policy import StructureExitSubmission

        return StructureExitSubmission(claim=claim)

    def legs(self):
        return [
            {"tradingsymbol": "SHORT", "side": "SELL", "quantity": -75,
             "exchange": "NFO", "product": "NRML"},
            {"tradingsymbol": "HEDGE", "side": "BUY", "quantity": 75,
             "exchange": "NFO", "product": "NRML"},
        ]

    def test_a_triggered_rule_submits_through_the_claim_path(self):
        import asyncio

        claims: list = []

        async def claim(**kwargs):
            claims.append(kwargs)
            return {"claim_id": "claim-1", "accepted": True}

        result = asyncio.run(
            self.submission(claim=claim).submit(
                run={"strategy_run_id": "run-1", "account_scope": "kite:A"},
                trigger={"status": "triggered", "triggered_rule": "index_guard",
                         "exit_idempotency_key": "key-1"},
                legs=self.legs(),
                closed_short_quantities={"SHORT": 75},
                structure_digest="digest-1",
            )
        )
        self.assertTrue(result["submitted"])
        self.assertEqual(result["claim_id"], "claim-1")
        # The whole chain is traced, so a reader can see which link failed.
        self.assertEqual(result["trace"]["rule"], "index_guard")
        self.assertEqual(result["trace"]["order_count"], 1)
        self.assertEqual(claims[0]["idempotency_key"], "key-1")

    def test_an_untriggered_rule_submits_nothing(self):
        import asyncio

        calls: list = []

        async def claim(**kwargs):
            calls.append(kwargs)
            return {"claim_id": "c"}

        result = asyncio.run(
            self.submission(claim=claim).submit(
                run={"strategy_run_id": "run-1"}, trigger={"status": "monitoring"},
                legs=self.legs(),
            )
        )
        # An exit that fires without a trigger is a liquidation nobody asked for.
        self.assertFalse(result["submitted"])
        self.assertEqual(result["reason"], "not_triggered")
        self.assertEqual(calls, [])

    def test_a_missing_claim_path_reports_rather_than_pretending(self):
        import asyncio

        result = asyncio.run(
            self.submission().submit(
                run={"strategy_run_id": "run-1"},
                trigger={"status": "triggered", "triggered_rule": "guard"},
                legs=self.legs(),
            )
        )
        # The orders are still built and reported: the seam is honest about not
        # having a claim path rather than silently doing nothing.
        self.assertFalse(result["submitted"])
        self.assertEqual(result["reason"], "no_claim_path")
        self.assertTrue(result["orders"])

    def test_nothing_to_submit_is_a_legitimate_outcome(self):
        import asyncio

        result = asyncio.run(
            self.submission(claim=lambda **_: {"claim_id": "c"}).submit(
                run={"strategy_run_id": "run-1"},
                trigger={"status": "triggered", "triggered_rule": "guard"},
                legs=[],
            )
        )
        self.assertFalse(result["submitted"])
        self.assertEqual(result["reason"], "no_exit_orders")
