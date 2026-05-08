import unittest

from fastapi import HTTPException

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from api.routers.worker_shared import DEFAULT_WORKER_ACTIONS, _assert_run_access  # noqa: E402


class WorkerRunAccessTests(unittest.TestCase):
    def test_worker_cannot_access_run_owned_by_different_token(self):
        token = WorkerToken(
            token_id="worker-a",
            name="worker-a",
            account_scope="kite:paper-a",
            allowed_modes=["paper"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        run = {
            "strategy_run_id": "run-b",
            "token_id": "worker-b",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
        }
        with self.assertRaises(HTTPException) as ctx:
            _assert_run_access(token, run)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_worker_can_access_own_run(self):
        token = WorkerToken(
            token_id="worker-a",
            name="worker-a",
            account_scope="kite:paper-a",
            allowed_modes=["paper"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        run = {
            "strategy_run_id": "run-a",
            "token_id": "worker-a",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
        }
        _assert_run_access(token, run)


if __name__ == "__main__":
    unittest.main()
