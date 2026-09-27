"""The hosted-strategy -> auto-journal bridge."""

from __future__ import annotations

import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.strategies import journal_bridge  # noqa: E402


class _Repo:
    def __init__(self):
        self.live_intents = []

    def ensure_live_strategy_run_for_intent(self, *, intent):
        self.live_intents.append(dict(intent))
        return "jr-live"


class _Journal:
    def __init__(self):
        self.repository = _Repo()
        self.paper = []
        self.events = []

    def ensure_paper_strategy_run(self, *, attribution):
        self.paper.append(dict(attribution))
        return "jr-paper"

    def append_decision_event(self, run_id, event):
        self.events.append((run_id, event))
        return len(self.events)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.journal = _Journal()
        self.recorder = journal_bridge.JournalDecisionRecorder(journal_service=self.journal)

    def test_live_decision_binds_to_the_live_order_run(self):
        self.recorder(event="request_approved", environment="live", strategy_run_id="run-1",
                      account_id="kite:A", summary="approved", context={"request_id": "r1"})
        self.assertEqual(self.journal.repository.live_intents,
                         [{"account_id": "kite:A", "strategy_run_id": "run-1"}])
        run_id, event = self.journal.events[0]
        self.assertEqual(run_id, "jr-live")
        self.assertEqual(event.decision_type, "review")
        self.assertEqual(event.actor_type, "user")
        self.assertEqual(event.context["request_id"], "r1")
        self.assertEqual(event.context["event"], "request_approved")

    def test_paper_decision_binds_to_the_paper_strategy_run(self):
        self.recorder(event="request_awaiting_approval", environment="paper",
                      strategy_run_id="run-2", account_id="kite:P", summary="s", context={})
        self.assertEqual(self.journal.paper,
                         [{"strategy_run_id": "run-2", "account_ref": "kite:P", "execution_mode": "paper"}])
        run_id, event = self.journal.events[0]
        self.assertEqual(run_id, "jr-paper")
        self.assertEqual((event.decision_type, event.actor_type), ("algo_trigger", "algo"))

    def test_dry_run_and_missing_ids_write_nothing(self):
        self.recorder(event="request_approved", environment="dry_run", strategy_run_id="r",
                      account_id="a", summary="s", context={})
        self.recorder(event="request_approved", environment="live", strategy_run_id="",
                      account_id="a", summary="s", context={})
        self.assertEqual(self.journal.events, [])


class HookTests(unittest.TestCase):
    def tearDown(self):
        journal_bridge.set_recorder(None)

    def test_default_recorder_is_a_no_op(self):
        journal_bridge.set_recorder(None)
        journal_bridge.record_decision(event="request_approved", environment="live",
                                       strategy_run_id="r", account_id="a", summary="s", context={})

    def test_a_failing_recorder_never_raises(self):
        def boom(**_kwargs):
            raise RuntimeError("journal down")

        journal_bridge.set_recorder(boom)
        journal_bridge.record_decision(event="request_approved", environment="live",
                                       strategy_run_id="r", account_id="a", summary="s", context={})

    def test_request_event_mapping(self):
        f = journal_bridge.request_event_for
        self.assertEqual(f({"status": "awaiting_approval"}), "request_awaiting_approval")
        self.assertEqual(f({"status": "queued", "decision_kind": "automatic"}), "request_auto_queued")
        self.assertEqual(f({"status": "queued", "decision_kind": "manual"}), "request_approved")
        self.assertEqual(f({"status": "rejected"}), "request_rejected")
        self.assertEqual(f({"status": "refused"}), "request_refused")
        self.assertIsNone(f({"status": "dispatching"}))


class ProtectionExitJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        journal_bridge.set_recorder(None)

    async def test_protection_exit_is_journaled(self):
        from unittest import mock

        from backend.api.services import protection_runtime

        calls = []
        journal_bridge.set_recorder(lambda **kwargs: calls.append(kwargs))
        with mock.patch(
            "backend.api.services.control_plane.exit_control_strategy",
            new=mock.AsyncMock(return_value={"status": "exit_requested"}),
        ):
            await protection_runtime.submit_worker_protection_exit(
                object(),
                {"strategy_run_id": "run-9", "account_scope": "kite:A", "execution_mode": "live"},
                {"triggered_rule": "index_stop", "exit_idempotency_key": "k1"},
            )
        self.assertEqual(calls[0]["event"], "protection_exit")
        self.assertEqual(calls[0]["environment"], "live")
        self.assertEqual(calls[0]["context"]["triggered_rule"], "index_stop")
