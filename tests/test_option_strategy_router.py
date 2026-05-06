import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

if "broker_api.broker_api" not in sys.modules:
    broker_api = types.ModuleType("broker_api.broker_api")
    broker_api.get_kite = lambda: None
    sys.modules["broker_api.broker_api"] = broker_api

if "broker_api.kite_orders" in sys.modules:
    sys.modules["broker_api.kite_orders"].realtime_positions_service = types.SimpleNamespace(
        get_positions=AsyncMock(return_value={}),
        initialize_positions=AsyncMock(return_value={}),
    )

from strategies.indexstoploss.router import BuildPositionRequest, SelectedStrikeData, build_position  # noqa: E402


async def _run_kite_write_action_inline(_action, _corr_id, callback, meta=None):
    return callback()


class _FakeBuilder:
    async def build_position_plan_from_strikes(self, **kwargs):
        return {
            "orders": [
                {
                    "tradingsymbol": "NIFTY24APR22500CE",
                    "transaction_type": "BUY",
                    "quantity": 50,
                    "exchange": "NFO",
                    "product": "MIS",
                    "order_type": "MARKET",
                }
            ],
            "strategy_legs": kwargs["selected_strikes"],
            "estimated_cost": 5000,
            "estimated_margin": 1250,
        }

    async def build_position_plan(self, **kwargs):
        return {
            "orders": [
                {
                    "tradingsymbol": "NIFTY24APR22500CE",
                    "transaction_type": "BUY",
                    "quantity": 50,
                    "exchange": "NFO",
                    "product": "MIS",
                    "order_type": "MARKET",
                }
            ],
            "strategy_legs": [
                {
                    "instrument_token": 101,
                    "tradingsymbol": "NIFTY24APR22500CE",
                    "strike": 22500,
                    "option_type": "CE",
                    "transaction_type": "BUY",
                    "ltp": 100.0,
                    "lot_size": 50,
                    "lots": 1,
                    "quantity": 50,
                }
            ],
            "estimated_cost": 5000,
            "estimated_margin": 1250,
        }


class _FakeStore:
    def __init__(self):
        self.created = []
        self.updated = []

    def create_run(self, **kwargs):
        self.created.append(kwargs)
        return "run-123"

    def update_execution_result(self, run_id, **kwargs):
        self.updated.append((run_id, kwargs))

    def get_linked_journal_run_id(self, run_id):
        return None

    def get_strategy_run(self, run_id):
        return {"id": run_id}


class OptionStrategyRouterTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, *, include_paper_runtime: bool = False):
        state = SimpleNamespace(
            position_builder=_FakeBuilder(),
            option_strategy_store=_FakeStore(),
        )
        if include_paper_runtime:
            state.paper_runtime_service = SimpleNamespace(place_basket=AsyncMock(return_value={"status": "success", "results": []}))
        return SimpleNamespace(app=SimpleNamespace(state=state), headers={}, cookies={})

    def _payload(self, *, execution_mode: str = "dry_run"):
        return BuildPositionRequest(
            underlying="NIFTY",
            expiry="2026-04-30",
            strategy_type="single_leg",
            template_id="buy_call",
            execution_mode=execution_mode,
            current_spot=22540,
            selected_strikes=[
                SelectedStrikeData(
                    instrument_token=101,
                    tradingsymbol="NIFTY24APR22500CE",
                    strike=22500,
                    option_type="CE",
                    ltp=100.0,
                    lot_size=50,
                    delta=0.52,
                    lots=1,
                    transaction_type="BUY",
                )
            ],
        )

    async def test_build_position_dry_run_returns_strategy_preview(self):
        request = self._request()

        response = await build_position(self._payload(), request, db=None, corr_id="corr-1")

        self.assertEqual(response["mode"], "dry_run")
        self.assertIn("strategy", response)
        self.assertEqual(response["strategy"]["inferred_family"], "directional")
        self.assertEqual(request.app.state.option_strategy_store.created, [])

    async def test_build_position_paper_executes_through_paper_runtime_and_store(self):
        request = self._request(include_paper_runtime=True)

        with patch("strategies.indexstoploss.router.require_app_user", return_value=SimpleNamespace(username="admin")), \
            patch("strategies.indexstoploss.router._build_runtime_instance_for_plan", return_value=SimpleNamespace(model_copy=lambda update=None: SimpleNamespace(status=update.get("status") if update else None))), \
            patch("strategies.indexstoploss.router._arm_runtime_monitoring", AsyncMock(return_value="option-strategy:run-123")):
            response = await build_position(self._payload(execution_mode="paper"), request, db=None, corr_id="corr-1")

        self.assertEqual(response["mode"], "paper")
        self.assertEqual(response["strategy_run_id"], "run-123")
        self.assertEqual(response["strategy_id"], "run-123")
        self.assertEqual(response["algo_instance_id"], "option-strategy:run-123")
        self.assertEqual(len(request.app.state.option_strategy_store.created), 1)
        self.assertEqual(len(request.app.state.option_strategy_store.updated), 1)
        self.assertEqual(request.app.state.option_strategy_store.created[0]["entry_surface"], None)
        request.app.state.paper_runtime_service.place_basket.assert_awaited_once()

    async def test_paper_execution_arms_runtime_monitoring_before_entry(self):
        request = self._request(include_paper_runtime=True)

        with patch("strategies.indexstoploss.router.require_app_user", return_value=SimpleNamespace(username="admin")), \
            patch("strategies.indexstoploss.router._build_runtime_instance_for_plan", return_value=SimpleNamespace(instance_id="option-strategy:run-123")) as build_runtime, \
            patch("strategies.indexstoploss.router._arm_runtime_monitoring", AsyncMock(return_value="option-strategy:run-123")) as arm_runtime:
            response = await build_position(self._payload(execution_mode="paper"), request, db=None, corr_id="corr-1")

        build_runtime.assert_called_once()
        arm_runtime.assert_awaited_once()
        self.assertEqual(response["algo_instance_id"], "option-strategy:run-123")

    async def test_live_failure_updates_store_status(self):
        request = self._request()

        class _BrokenKite:
            VARIETY_REGULAR = "regular"
            EXCHANGE_NFO = "NFO"
            PRODUCT_MIS = "MIS"
            ORDER_TYPE_MARKET = "MARKET"

            def place_order(self, **_kwargs):
                raise RuntimeError("broker down")

        with patch("strategies.indexstoploss.router._build_runtime_instance_for_plan", return_value=SimpleNamespace(model_copy=lambda update=None: SimpleNamespace(status=update.get("status") if update else None))), \
            patch("strategies.indexstoploss.router._arm_runtime_monitoring", AsyncMock(return_value="option-strategy:run-123")), \
            patch("strategies.indexstoploss.router._disarm_runtime_monitoring", AsyncMock()) as disarm, \
            patch("strategies.indexstoploss.router.run_kite_write_action", _run_kite_write_action_inline), \
            patch("strategies.indexstoploss.router.get_kite", return_value=_BrokenKite()):
            response = await build_position(self._payload(execution_mode="live"), request, db=None, corr_id="corr-1")

        disarm.assert_awaited_once_with(request, "option-strategy:run-123")

        self.assertEqual(response["status"], "failed")
        self.assertEqual(request.app.state.option_strategy_store.updated[0][1]["status"], "failed")
        self.assertEqual(request.app.state.option_strategy_store.updated[0][1]["execution_result"]["strategy_run_id"], "run-123")

    async def test_live_success_passes_strategy_run_id_through_local_dispatch_metadata(self):
        request = self._request()

        with patch("strategies.indexstoploss.router._build_runtime_instance_for_plan", return_value=SimpleNamespace(model_copy=lambda update=None: SimpleNamespace(status=update.get("status") if update else None))), \
            patch("strategies.indexstoploss.router._arm_runtime_monitoring", AsyncMock(return_value="option-strategy:run-123")), \
            patch("strategies.indexstoploss.router._activate_runtime_monitoring", AsyncMock()), \
            patch("strategies.indexstoploss.router.run_kite_write_action", AsyncMock(return_value="OID-123")) as write_action, \
            patch("strategies.indexstoploss.router.get_kite", return_value=SimpleNamespace()):
            response = await build_position(self._payload(execution_mode="live"), request, db=None, corr_id="corr-1")

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["strategy_run_id"], "run-123")
        self.assertEqual(response["orders_placed"][0]["strategy_run_id"], "run-123")
        self.assertEqual(request.app.state.option_strategy_store.updated[0][1]["execution_result"]["strategy_run_id"], "run-123")
        self.assertEqual(write_action.await_args.kwargs["meta"]["strategy_run_id"], "run-123")


if __name__ == "__main__":
    unittest.main()
