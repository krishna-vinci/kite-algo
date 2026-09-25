import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.services.protection_runtime import WorkerProtectionRuntime, submit_worker_protection_exit


class _Repo:
    def __init__(self):
        self.saved = []
        self.runs = [
            {
                "strategy_run_id": "run-1",
                "account_scope": "kite:paper-a",
                "execution_mode": "paper",
                "status": "open",
                "runtime_state": {
                    "backend_protection": {
                        "enabled": True,
                        "positions": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "entry_price": 100, "stoploss_pct": 5}],
                    },
                    "backend_protection_state": {"generation": 1},
                },
                "last_heartbeat_at": datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
            }
        ]

    async def list_protection_enabled_runs(self):
        return [dict(item) for item in self.runs]

    async def update_run_runtime_state(self, strategy_run_id, runtime_state):
        self.saved.append((strategy_run_id, runtime_state))
        self.runs[0]["runtime_state"] = runtime_state
        return dict(self.runs[0])

    async def update_run_backend_protection_state(self, strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
        current = dict(self.runs[0].get("runtime_state", {}).get("backend_protection_state") or {})
        if expected_generation is not None and int(current.get("generation") or 0) != int(expected_generation):
            return None
        if expected_triggered_rule is not None and str(current.get("triggered_rule") or "") != str(expected_triggered_rule):
            return None
        if expected_exit_claim_id is not None and str(current.get("exit_claim_id") or "") != str(expected_exit_claim_id):
            return None
        runtime_state = dict(self.runs[0]["runtime_state"])
        runtime_state["backend_protection_state"] = dict(protection_state)
        self.saved.append((strategy_run_id, runtime_state))
        self.runs[0]["runtime_state"] = runtime_state
        return dict(self.runs[0])


class _StructureRepo(_Repo):
    """A paper run whose protection carries a STRUCTURE identity."""

    def __init__(self, *, structure=None):
        super().__init__()
        protection = dict(self.runs[0]["runtime_state"]["backend_protection"])
        if structure is not None:
            protection["structure"] = structure
        self.runs[0]["runtime_state"]["backend_protection"] = protection


_OWNED_STRUCTURE = {
    "structure_digest": "digest-owned",
    "legs": [
        {"tradingsymbol": "SHORT-CE", "side": "SELL", "quantity": -75,
         "exchange": "NFO", "product": "NRML"},
    ],
    "closed_short_quantities": {},
}


class _OwnerRowRepo(_StructureRepo):
    """A CLOSED worker run that still has an ACTIVE protection owner row (B2.4 S2a).

    The generic per-run list cannot reach this run at all - its status is not
    ``open`` - so anything the loop does here, it does because it read the owner
    row.
    """

    def __init__(self, *, owner_state="active", also_in_run_list=True, owner_policy=None):
        super().__init__(structure=dict(_OWNED_STRUCTURE))
        self.runs[0]["status"] = "closed"
        self.owner_state = owner_state
        self.also_in_run_list = also_in_run_list
        self.owner_policy = dict(owner_policy) if owner_policy is not None else {
            "structure_digest": "digest-owned"
        }

    async def list_protection_enabled_runs(self):
        if not self.also_in_run_list:
            return []
        # Deliberately overlapping: if the loop did not give the owner row
        # precedence this run would be evaluated twice.
        return [dict(self.runs[0])]

    async def list_protection_owners(self):
        if self.owner_state != "active":
            return []
        return [
            {
                **dict(self.runs[0]),
                "protection_owner": {
                    "option_run_id": "opt_run_abc123",
                    "owner_run_id": "run-1",
                    "owner_epoch": 1,
                    "action_state": "none",
                    "policy_version": "v" * 64,
                    "policy": dict(self.owner_policy),
                    "option_run_status": "entered",
                },
            }
        ]


class _OwnerMirrorStore:
    """Records what the loop mirrored onto the owner row."""

    def __init__(self):
        self.calls = []

    def record_action(self, option_run_id, action_state, stage_digest, observed_epoch):
        self.calls.append((option_run_id, action_state, stage_digest, observed_epoch))


class _Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=int(seconds))


class WorkerProtectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_submits_exit_and_persists_state_when_triggered(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["triggered"], 1)
        self.assertEqual(repo.saved[-1][1]["backend_protection_state"]["triggered_rule"], "position_stoploss")
        self.assertEqual(repo.saved[-1][1]["backend_protection_state"]["action"], "exit_strategy")
        self.assertTrue(repo.saved[-1][1]["backend_protection_state"]["exit_submitted"])

    async def test_a_structure_aware_trigger_claims_and_submits_through_the_seam(self):
        """Gap C: a structure-aware trigger must actually SUBMIT its exits.

        The evaluator recommending orders is not a submission, and a protection rule
        that only recommends is a rule that does not protect.
        """
        structure = {
            "structure_digest": "digest-abc",
            "legs": [
                {"tradingsymbol": "SHORT-CE", "side": "SELL", "quantity": -75,
                 "exchange": "NFO", "product": "NRML", "structure_leg_id": "short"},
                {"tradingsymbol": "HEDGE-CE", "side": "BUY", "quantity": 75,
                 "exchange": "NFO", "product": "NRML", "structure_leg_id": "hedge",
                 "hedge_for": "SHORT-CE"},
            ],
            "closed_short_quantities": {"SHORT-CE": 75},
        }
        repo = _StructureRepo(structure=structure)
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 1)
        # The exit went through the SAME durable claim path the generic exit uses,
        # so idempotency and single-claim-per-exit are inherited rather than
        # re-implemented.
        exit_submitter.assert_awaited_once()
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertTrue(state["exit_submitted"])
        # And the whole chain is traced, so a reader can see which link ran.
        trace = state["structure_exit"]
        self.assertEqual(trace["trace"]["structure_digest"], "digest-abc")
        self.assertEqual(trace["trace"]["order_count"], 1)  # the hedge releases
        self.assertEqual(trace["trace"]["naked_short_quantity"], 0)
        self.assertEqual(
            [order["tradingsymbol"] for order in trace["orders"]], ["HEDGE-CE"]
        )

    async def test_a_structure_trigger_with_no_claim_path_records_the_failure(self):
        """Fail-closed: an unavailable seam must not look like a submission."""
        repo = _StructureRepo(structure={
            "structure_digest": "digest-abc",
            "legs": [{"tradingsymbol": "SHORT-CE", "side": "SELL", "quantity": -75,
                      "exchange": "NFO", "product": "NRML"}],
        })
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=AsyncMock(side_effect=RuntimeError("claim path down")),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        # A failed submission is NOT counted as triggered, exactly as the generic
        # path behaves for a failing submitter — the counter means "an exit was
        # submitted", not "a rule fired".
        self.assertEqual(result["triggered"], 0)
        state = repo.saved[-1][1]["backend_protection_state"]
        # And the failure is recorded as evidence rather than dropped: the run never
        # reaches a pretend-submitted state.
        self.assertNotEqual(state.get("exit_submission_status"), "submitted")
        self.assertFalse(state["exit_submitted"])
        self.assertEqual(state["exit_submission_status"], "seam_failed")
        self.assertFalse(state["structure_exit"]["submitted"])

    async def test_a_run_without_a_structure_keeps_todays_behaviour(self):
        """Regression: the non-structure path is untouched, key for key."""
        repo = _Repo()
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 1)
        exit_submitter.assert_awaited_once()
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertTrue(state["exit_submitted"])
        # No structure keys appear anywhere on a non-structure run.
        self.assertNotIn("structure_exit", state)


    async def test_runtime_persists_error_without_breaking_loop(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(side_effect=RuntimeError("pnl broken")),
            exit_submitter=AsyncMock(),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["errors"], 1)
        self.assertEqual(repo.saved[0][1]["backend_protection_state"]["status"], "error")

    async def test_already_exit_submitted_does_not_duplicate_exit(self):
        repo = _Repo()
        repo.runs[0]["runtime_state"]["backend_protection_state"] = {"generation": 1, "exit_submitted": True, "status": "triggered"}
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        exit_submitter.assert_not_awaited()

    async def test_generation_conflict_skips_stale_exit_submission(self):
        repo = _Repo()

        async def stale_generation_update(strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
            return None

        repo.update_run_backend_protection_state = stale_generation_update
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        exit_submitter.assert_not_awaited()

    async def test_recent_exit_claim_prevents_duplicate_exit_submission(self):
        repo = _Repo()
        repo.runs[0]["runtime_state"]["backend_protection_state"] = {
            "generation": 1,
            "status": "triggered",
            "triggered_rule": "position_stoploss",
            "exit_claim_id": "claim-1",
            "exit_claimed_at": "2026-04-25T12:00:45+00:00",
            "exit_submitted": False,
        }
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=exit_submitter,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        exit_submitter.assert_not_awaited()

    async def test_submit_exception_records_unknown_terminal_state_with_claim(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(side_effect=RuntimeError("broker timeout")),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertEqual(state["status"], "error")
        self.assertTrue(state["exit_submitted"])
        self.assertEqual(state["exit_submission_status"], "unknown")
        self.assertTrue(state["exit_claim_id"])

    async def test_deferred_exit_result_keeps_claim_for_retry_without_marking_exit_submitted(self):
        repo = _Repo()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(return_value={"status": "deferred", "deferred": True, "message": "attribution pending"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertEqual(state["status"], "triggered")
        self.assertFalse(state["exit_submitted"])
        self.assertEqual(state["exit_submission_status"], "deferred")
        self.assertEqual(state["exit_result"]["status"], "deferred")
        self.assertTrue(state["exit_claim_id"])

    async def test_successful_exit_forces_terminal_state_if_final_cas_fails(self):
        repo = _Repo()
        calls = {"count": 0}

        async def flaky_update(strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
            calls["count"] += 1
            if calls["count"] == 2:
                return None
            runtime_state = dict(repo.runs[0]["runtime_state"])
            runtime_state["backend_protection_state"] = dict(protection_state)
            repo.saved.append((strategy_run_id, runtime_state))
            repo.runs[0]["runtime_state"] = runtime_state
            return dict(repo.runs[0])

        repo.update_run_backend_protection_state = flaky_update
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 94}]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 1)
        self.assertTrue(repo.saved[-1][1]["backend_protection_state"]["exit_submitted"])
        self.assertEqual(calls["count"], 3)

    async def test_non_trigger_update_does_not_overwrite_concurrent_claim(self):
        repo = _Repo()

        async def claimed_update(strategy_run_id, protection_state, *, expected_generation=None, expected_triggered_rule=None, expected_exit_claim_id=None):
            self.assertEqual(expected_triggered_rule, "")
            self.assertEqual(expected_exit_claim_id, "")
            return None

        repo.update_run_backend_protection_state = claimed_update
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [{"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1, "net_quantity": 1, "average_price": 100, "last_price": 100}]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result["triggered"], 0)

    async def test_submit_worker_protection_exit_forwards_claimed_idempotency_key(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), headers={})
        run = {"strategy_run_id": "run-1", "account_scope": "kite:paper-a"}
        state = {"triggered_rule": "basket_stoploss", "exit_idempotency_key": "backend-protection:run-1:g1:basket_stoploss:abc"}

        with patch("backend.api.services.control_plane.exit_control_strategy", new=AsyncMock(return_value={"status": "closed"})) as exit_mock:
            result = await submit_worker_protection_exit(request, run, state)

        self.assertEqual(result["status"], "closed")
        self.assertIsNotNone(exit_mock.await_args)
        kwargs = getattr(exit_mock.await_args, "kwargs", {})
        self.assertEqual(kwargs["idempotency_key"], "backend-protection:run-1:g1:basket_stoploss:abc")

    async def test_an_active_owner_row_keeps_a_closed_structure_protected(self):
        """B2.4 S2a: protection follows the OWNER ROW, not the worker run's status.

        The worker run here is ``closed``, so it is not in the generic
        per-run list. The structure is still owed its protective exit, and the
        owner row is the only reason the loop evaluates it at all.
        """
        repo = _OwnerRowRepo()
        owner_store = _OwnerMirrorStore()
        structure_exit = AsyncMock(
            return_value={
                "submitted": True,
                "complete": True,
                "reason": "submitted",
                "option_run_id": "opt_run_abc123",
                "stage_digest": "stage-1",
            }
        )
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            structure_exit_submitter=structure_exit,
            owner_store=owner_store,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result, {"evaluated": 1, "triggered": 1, "errors": 0})
        structure_exit.assert_awaited_once()
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertTrue(state["exit_submitted"])
        # The claim is mirrored BEFORE the stage, and the stage verdict after it,
        # onto the option run's own owner row at its observed epoch.
        self.assertEqual(
            [(call[0], call[1]) for call in owner_store.calls],
            [("opt_run_abc123", "claimed"), ("opt_run_abc123", "none")],
        )
        self.assertEqual(owner_store.calls[0][3], 1)
        self.assertEqual(owner_store.calls[1][2], "stage-1")

    async def test_an_owner_row_supplies_the_missing_run_structure_identity(self):
        """An active option owner never falls through to whole-book liquidation."""
        repo = _OwnerRowRepo()
        repo.runs[0]["runtime_state"]["backend_protection"].pop("structure")
        owner_store = _OwnerMirrorStore()
        structure_exit = AsyncMock(
            return_value={
                "submitted": True,
                "complete": True,
                "reason": "submitted",
                "option_run_id": "opt_run_abc123",
                "stage_digest": "stage-1",
            }
        )
        exit_submitter = AsyncMock(return_value={"status": "closed"})
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=exit_submitter,
            structure_exit_submitter=structure_exit,
            owner_store=owner_store,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result, {"evaluated": 1, "triggered": 1, "errors": 0})
        structure_exit.assert_awaited_once()
        exit_submitter.assert_not_awaited()

    async def test_an_owner_row_without_a_resolvable_structure_refuses(self):
        """Unknown structure identity is an error, never generic liquidation."""
        repo = _OwnerRowRepo(owner_policy={})
        repo.runs[0]["runtime_state"]["backend_protection"].pop("structure")
        exit_submitter = AsyncMock()
        structure_exit = AsyncMock()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=exit_submitter,
            structure_exit_submitter=structure_exit,
            owner_store=_OwnerMirrorStore(),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result, {"evaluated": 1, "triggered": 0, "errors": 1})
        exit_submitter.assert_not_awaited()
        structure_exit.assert_not_awaited()
        state = repo.saved[-1][1]["backend_protection_state"]
        self.assertEqual(state["status"], "error")
        self.assertIn("OPTION_PROTECTION_STRUCTURE_UNKNOWN", state["error"])

    async def test_a_released_owner_row_is_not_evaluated(self):
        """Twin: no owner row, no evaluation - even for a structure we know about."""
        repo = _OwnerRowRepo(owner_state="released", also_in_run_list=False)
        owner_store = _OwnerMirrorStore()
        structure_exit = AsyncMock()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": []}),
            exit_submitter=AsyncMock(),
            structure_exit_submitter=structure_exit,
            owner_store=owner_store,
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result, {"evaluated": 0, "triggered": 0, "errors": 0})
        structure_exit.assert_not_awaited()
        self.assertEqual(owner_store.calls, [])

    async def test_an_owner_row_whose_run_has_protection_off_is_not_evaluated(self):
        """Twin: the owner row carries ownership, not the on/off switch.

        A structure whose run declares no protection rule has nothing to fire and
        no structure identity to exit, so it stays on the path it had before the
        owner row existed - otherwise every plan-created structure would be
        re-written every pass for no decision at all.
        """
        repo = _OwnerRowRepo(also_in_run_list=False)
        repo.runs[0]["runtime_state"]["backend_protection"] = {"enabled": False}
        structure_exit = AsyncMock()
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": []}),
            exit_submitter=AsyncMock(),
            structure_exit_submitter=structure_exit,
            owner_store=_OwnerMirrorStore(),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result, {"evaluated": 0, "triggered": 0, "errors": 0})
        structure_exit.assert_not_awaited()

    async def test_a_structure_with_an_owner_row_is_evaluated_exactly_once(self):
        """One structure, one evaluation: the owner row wins over the run list.

        A worker run that appears in BOTH enumerations must not be claimed - and
        could not be submitted - twice by the same pass.
        """
        repo = _OwnerRowRepo(also_in_run_list=True)
        structure_exit = AsyncMock(
            return_value={"submitted": True, "complete": True, "reason": "submitted"}
        )
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            structure_exit_submitter=structure_exit,
            owner_store=_OwnerMirrorStore(),
            now_fn=lambda: datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc),
            squareoff_schedule={},
        )

        result = await runtime.evaluate_once()

        self.assertEqual(result, {"evaluated": 1, "triggered": 1, "errors": 0})
        structure_exit.assert_awaited_once()

    async def test_a_still_owed_stage_is_staging_and_an_unknown_send_is_unresolved(self):
        """The mirror carries the run's own stage verdict onto the owner row."""
        repo = _OwnerRowRepo()
        owner_store = _OwnerMirrorStore()
        structure_exit = AsyncMock(side_effect=[
            {
                "submitted": True,
                "complete": False,
                "reason": "submitted",
                "option_run_id": "opt_run_abc123",
                "stage_digest": "stage-2",
            },
            {
                "submitted": False,
                "complete": False,
                "reason": "stage_send_unknown",
                "option_run_id": "opt_run_abc123",
                "stage_digest": "stage-3",
            },
        ])
        clock = _Clock(datetime(2026, 4, 25, 12, 1, tzinfo=timezone.utc))
        runtime = WorkerProtectionRuntime(
            repo=repo,
            pnl_loader=AsyncMock(return_value={"legs": [
                {"symbol": "NSE:INFY", "product": "CNC", "side": "BUY", "quantity": 1,
                 "net_quantity": 1, "average_price": 100, "last_price": 94}
            ]}),
            exit_submitter=AsyncMock(return_value={"status": "closed"}),
            structure_exit_submitter=structure_exit,
            owner_store=owner_store,
            now_fn=clock,
            squareoff_schedule={},
        )

        await runtime.evaluate_once()
        self.assertEqual(owner_store.calls[-1][1], "staging")
        self.assertEqual(owner_store.calls[-1][2], "stage-2")

        # Past the claim's own throttle window, so the next pass re-derives the
        # stage instead of inheriting it, and this time cannot resolve it.
        clock.advance(180)
        await runtime.evaluate_once()
        self.assertEqual(owner_store.calls[-1][1], "unresolved")
        self.assertEqual(owner_store.calls[-1][2], "stage-3")


if __name__ == "__main__":
    unittest.main()
