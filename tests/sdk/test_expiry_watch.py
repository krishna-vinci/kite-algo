import unittest
from datetime import datetime, timezone

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.services.expiry_watch import ExpiryWatchService

NOW_ON_EXPIRY_DAY_BEFORE_CUTOFF = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)  # 13:30 IST
NOW_ON_EXPIRY_DAY_AFTER_CUTOFF = datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc)  # 15:00 IST
NOW_T_MINUS_1 = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def _owner_run(*, expiry="2026-09-26", expiry_policy="exit_before_cutoff", runtime_state=None, status="open"):
    return {
        "strategy_run_id": "run-1",
        "account_scope": "kite:A",
        "runtime_state": dict(runtime_state or {}),
        "protection_owner": {
            "option_run_id": "opt-1",
            "owner_run_id": "run-1",
            "policy": {"expiry": expiry, "expiry_policy": expiry_policy},
            "option_run_status": status,
        },
    }


class _Repo:
    def __init__(self, runs):
        self.runs = list(runs)
        self.saved = []

    async def list_protection_owners(self):
        return [dict(run) for run in self.runs]

    async def update_run_runtime_state(self, strategy_run_id, runtime_state):
        self.saved.append((strategy_run_id, dict(runtime_state)))
        for run in self.runs:
            if run["strategy_run_id"] == strategy_run_id:
                run["runtime_state"] = dict(runtime_state)
        return None


class ExpiryWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_warning_emitted_once_per_day(self):
        repo = _Repo([_owner_run(expiry="2026-09-26")])  # T-1 relative to NOW_T_MINUS_1
        notified = []
        published = []
        service = ExpiryWatchService(
            repo=repo,
            notify_option_run=lambda **kw: notified.append(kw) or True,
            publish_timeline=lambda row: published.append(row),
            now_fn=lambda: NOW_T_MINUS_1,
        )

        result = await service.evaluate_once()
        self.assertEqual(result["warned"], 1)
        self.assertEqual(result["escalated"], 0)
        self.assertEqual(len(notified), 1)
        self.assertEqual(published[-1]["event_type"], "EXPIRY_WARNING")

        # A second tick the SAME day must not warn again.
        result_again = await service.evaluate_once()
        self.assertEqual(result_again["warned"], 0)
        self.assertEqual(len(notified), 1)

    async def test_cutoff_exit_submitted_once(self):
        repo = _Repo([_owner_run(expiry="2026-09-26", expiry_policy="exit_before_cutoff")])
        submissions = []

        async def submitter(run, state):
            submissions.append((run["strategy_run_id"], state))
            return {"submitted": True, "complete": True}

        service = ExpiryWatchService(
            repo=repo,
            structure_exit_submitter=submitter,
            now_fn=lambda: NOW_ON_EXPIRY_DAY_AFTER_CUTOFF,
        )

        result = await service.evaluate_once()
        self.assertEqual(result["escalated"], 1)
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0][1]["status"], "triggered")

        # Ticking again on the same (or a later) pass never resubmits.
        result_again = await service.evaluate_once()
        self.assertEqual(result_again["escalated"], 0)
        self.assertEqual(len(submissions), 1)

    async def test_before_cutoff_time_does_not_escalate_yet(self):
        repo = _Repo([_owner_run(expiry="2026-09-26", expiry_policy="exit_before_cutoff")])
        submissions = []

        async def submitter(run, state):
            submissions.append(run)
            return {"submitted": True, "complete": True}

        service = ExpiryWatchService(
            repo=repo,
            structure_exit_submitter=submitter,
            now_fn=lambda: NOW_ON_EXPIRY_DAY_BEFORE_CUTOFF,
        )
        result = await service.evaluate_once()
        self.assertEqual(result["escalated"], 0)
        self.assertEqual(submissions, [])
        # Still warns, since expiry day is within the warning window.
        self.assertEqual(result["warned"], 1)

    async def test_policy_without_exit_before_cutoff_only_warns(self):
        repo = _Repo([_owner_run(expiry="2026-09-26", expiry_policy="allow_cash_settlement")])
        submissions = []

        async def submitter(run, state):
            submissions.append(run)
            return {"submitted": True, "complete": True}

        service = ExpiryWatchService(
            repo=repo,
            structure_exit_submitter=submitter,
            now_fn=lambda: NOW_ON_EXPIRY_DAY_AFTER_CUTOFF,
        )
        result = await service.evaluate_once()
        self.assertEqual(result["warned"], 1)
        self.assertEqual(result["escalated"], 0)
        self.assertEqual(submissions, [])

    async def test_terminal_option_run_is_skipped(self):
        repo = _Repo([_owner_run(expiry="2026-09-26", status="exited")])
        service = ExpiryWatchService(repo=repo, now_fn=lambda: NOW_ON_EXPIRY_DAY_AFTER_CUTOFF)
        result = await service.evaluate_once()
        self.assertEqual(result["warned"], 0)
        self.assertEqual(result["escalated"], 0)
        self.assertEqual(repo.saved, [])

    async def test_outside_the_window_does_nothing(self):
        repo = _Repo([_owner_run(expiry="2026-10-10")])
        service = ExpiryWatchService(repo=repo, now_fn=lambda: NOW_T_MINUS_1)
        result = await service.evaluate_once()
        self.assertEqual(result["warned"], 0)
        self.assertEqual(result["escalated"], 0)


class FuturesRollEscalationTests(unittest.IsolatedAsyncioTestCase):
    async def test_roll_escalator_is_driven_once_per_tick(self):
        calls = []

        async def escalator(roll):
            calls.append(roll["roll_id"])
            return True

        async def lister():
            return [{"roll_id": "roll-1"}]

        repo = _Repo([])
        service = ExpiryWatchService(
            repo=repo,
            roll_lister=lister,
            roll_escalator=escalator,
        )
        result = await service.evaluate_once()
        self.assertEqual(result["warned"], 1)
        self.assertEqual(calls, ["roll-1"])


if __name__ == "__main__":
    unittest.main()
