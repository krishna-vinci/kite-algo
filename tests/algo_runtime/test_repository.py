import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from algo_runtime.models import (  # noqa: E402
    AlgoCheckpoint,
    AlgoInstance,
    AlgoLifecycleState,
    DependencySpec,
    ExecutionMode,
    MarketDataMode,
    OptionReadSpec,
    OrderScope,
)
from algo_runtime.repository import InMemoryAlgoRepository, SqlAlchemyAlgoRepository  # noqa: E402


class FakeResult:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class FakeRow:
    def __init__(self, **kwargs):
        self._mapping = kwargs


class FakeSqlSession:
    def __init__(self):
        self.instances = {}
        self.checkpoints = {}
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}

        if "FROM public.algo_instances" in sql and "WHERE status IN" in sql:
            rows = [
                FakeRow(**payload)
                for payload in sorted(self.instances.values(), key=lambda item: item["created_at"])
                if payload["status"] in {"enabled", "running", "paused"}
            ]
            return FakeResult(rows=rows)

        if "FROM public.algo_instances" in sql and "WHERE instance_id = :instance_id" in sql and "UPDATE" not in sql:
            payload = self.instances.get(params["instance_id"])
            return FakeResult(row=FakeRow(**payload) if payload else None)

        if "INSERT INTO public.algo_instances" in sql:
            payload = {
                "instance_id": params["instance_id"],
                "algo_type": params["algo_type"],
                "status": params["status"],
                "execution_mode": params["execution_mode"],
                "config_json": json.loads(params["config_json"]),
                "dependency_spec_json": json.loads(params["dependency_spec_json"]),
                "metadata_json": json.loads(params["metadata_json"]),
                "created_at": params["created_at"],
                "updated_at": params["updated_at"],
            }
            self.instances[params["instance_id"]] = payload
            return FakeResult(row=FakeRow(**payload))

        if "UPDATE public.algo_instances" in sql:
            payload = self.instances.get(params["instance_id"])
            if payload is None:
                return FakeResult(row=None)
            payload = {**payload, "status": params["status"], "updated_at": params["updated_at"]}
            self.instances[params["instance_id"]] = payload
            return FakeResult(row=FakeRow(**payload))

        if "FROM public.algo_instance_checkpoints" in sql and "WHERE instance_id = :instance_id" in sql:
            payload = self.checkpoints.get(params["instance_id"])
            return FakeResult(row=FakeRow(**payload) if payload else None)

        if "INSERT INTO public.algo_instance_checkpoints" in sql:
            payload = {
                "instance_id": params["instance_id"],
                "last_evaluated_at": params["last_evaluated_at"],
                "last_action_json": json.loads(params["last_action_json"]) if params["last_action_json"] is not None else None,
                "state_json": json.loads(params["state_json"]),
                "updated_at": params["updated_at"],
            }
            self.checkpoints[params["instance_id"]] = payload
            return FakeResult(row=FakeRow(**payload))

        raise AssertionError(f"Unhandled SQL in fake session: {sql}")


class InMemoryAlgoRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_and_get_instance_uses_copies(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(instance_id="algo-1", algo_type="demo")

        saved = await repository.save_instance(instance)
        saved.metadata["mutated"] = True
        fetched = await repository.get_instance("algo-1")

        self.assertEqual(fetched.instance_id, "algo-1")
        self.assertNotIn("mutated", fetched.metadata)

    async def test_update_status_refreshes_timestamp(self):
        repository = InMemoryAlgoRepository()
        instance = AlgoInstance(instance_id="algo-1", algo_type="demo")
        await repository.save_instance(instance)

        updated = await repository.update_status("algo-1", AlgoLifecycleState.RUNNING)

        self.assertEqual(updated.status, AlgoLifecycleState.RUNNING)
        self.assertGreaterEqual(updated.updated_at, instance.updated_at)

    async def test_checkpoint_round_trip_is_isolated(self):
        repository = InMemoryAlgoRepository()
        checkpoint = AlgoCheckpoint(instance_id="algo-1", state={"armed": True})

        saved = await repository.save_checkpoint(checkpoint)
        saved.state["armed"] = False
        fetched = await repository.get_checkpoint("algo-1")

        self.assertTrue(fetched.state["armed"])


class SqlAlchemyAlgoRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_and_list_active_instances(self):
        session = FakeSqlSession()
        repository = SqlAlchemyAlgoRepository(session_factory=lambda: session)
        instance = AlgoInstance(
            instance_id="algo-1",
            algo_type="demo",
            status=AlgoLifecycleState.RUNNING,
            config={"threshold": 12},
        )

        await repository.save_instance(instance)
        active = await repository.list_active_instances()

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].config["threshold"], 12)
        session.commit.assert_called()
        session.close.assert_called()

    async def test_update_status_returns_updated_instance(self):
        session = FakeSqlSession()
        now = datetime.now(timezone.utc)
        session.instances["algo-1"] = {
            "instance_id": "algo-1",
            "algo_type": "demo",
            "status": "enabled",
            "execution_mode": "live",
            "config_json": {"a": 1},
            "dependency_spec_json": {},
            "metadata_json": {},
            "created_at": now,
            "updated_at": now,
        }
        repository = SqlAlchemyAlgoRepository(session_factory=lambda: session)

        updated = await repository.update_status("algo-1", AlgoLifecycleState.PAUSED)

        self.assertEqual(updated.status, AlgoLifecycleState.PAUSED)
        self.assertEqual(session.instances["algo-1"]["status"], "paused")

    async def test_save_and_get_checkpoint_round_trip(self):
        session = FakeSqlSession()
        repository = SqlAlchemyAlgoRepository(session_factory=lambda: session)
        checkpoint = AlgoCheckpoint(
            instance_id="algo-1",
            last_action={"type": "notify"},
            state={"armed": True},
        )

        await repository.save_checkpoint(checkpoint)
        fetched = await repository.get_checkpoint("algo-1")

        self.assertEqual(fetched.last_action["type"], "notify")
        self.assertTrue(fetched.state["armed"])

    async def test_save_instance_serializes_enums_and_datetimes(self):
        session = FakeSqlSession()
        repository = SqlAlchemyAlgoRepository(session_factory=lambda: session)
        instance = AlgoInstance(
            instance_id="algo-2",
            algo_type="options_demo",
            execution_mode=ExecutionMode.DRY_RUN,
            config={"armed_at": datetime(2026, 4, 7, tzinfo=timezone.utc)},
            metadata={"scope": OrderScope.ACCOUNT_RELEVANT},
            dependency_spec=DependencySpec(
                market_tokens={256265: MarketDataMode.FULL},
                option_reads=[OptionReadSpec(underlying="NIFTY")],
                order_scope=OrderScope.ACCOUNT_RELEVANT,
            ),
        )

        saved = await repository.save_instance(instance)
        fetched = await repository.get_instance("algo-2")

        self.assertEqual(saved.dependency_spec.market_tokens[256265], MarketDataMode.FULL)
        self.assertEqual(saved.execution_mode, ExecutionMode.DRY_RUN)
        self.assertEqual(fetched.metadata["scope"], "account_relevant")
        self.assertEqual(fetched.execution_mode, ExecutionMode.DRY_RUN)
        self.assertEqual(fetched.dependency_spec.order_scope, OrderScope.ACCOUNT_RELEVANT)
        self.assertEqual(fetched.dependency_spec.option_reads[0].view.value, "snapshot")
        self.assertEqual(fetched.config["armed_at"], "2026-04-07T00:00:00+00:00")
