# pyright: reportArgumentType=false
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.routers.algo_workers import (  # noqa: E402
    DEFAULT_WORKER_ACTIONS,
    WorkerIntentRequest,
    get_worker_run_pnl,
    WorkerRiskPatchRequest,
    WorkerRunCreateRequest,
    WorkerToken,
    WorkerTokenCreateRequest,
    create_worker_run,
    create_worker_token,
    patch_worker_run_risk,
    exit_worker_run,
    stream_worker_run_pnl,
    submit_worker_intent,
    WorkerExitRequest,
)
from api.routers.algo_workers import _hash_token  # noqa: E402


async def _run_to_thread_inline(func, /, *args, **kwargs):
    return func(*args, **kwargs)


class _FakeWorkerRepository:
    def __init__(self, *, raw_token="secret-token", token=None):
        self.raw_token = raw_token
        self.token = token or WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:paper-a",
            allowed_modes=["paper", "dry_run"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        self.tokens = {}
        self.runs = {}
        self.intent_results = {}
        self.touched = []
        self.live_open_legs = {}

    async def create_token(self, payload, *, raw_token, token_id):
        self.tokens[token_id] = {
            "token_id": token_id,
            "name": payload.name,
            "account_scope": payload.account_scope,
            "allowed_modes": payload.allowed_modes,
            "allowed_actions": payload.allowed_actions,
            "allowed_templates": payload.allowed_templates,
            "status": "active",
            "created_at": None,
            "expires_at": payload.expires_at,
            "last_used_at": None,
        }
        return dict(self.tokens[token_id])

    async def get_token_by_hash(self, token_hash):
        return self.token if token_hash == _hash_token(self.raw_token) else None

    async def touch_token(self, token_id):
        self.touched.append(token_id)

    async def create_run(self, token, payload, *, strategy_run_id):
        run = {
            "strategy_run_id": strategy_run_id,
            "token_id": token.token_id,
            "template_id": payload.template_id,
            "account_scope": payload.account_scope,
            "execution_mode": payload.execution_mode,
            "status": "open",
            "summary_fields": payload.summary_fields,
            "risk_schema": payload.risk_schema,
            "allowed_actions": payload.allowed_actions,
            "runtime_state": payload.runtime_state,
            "metadata": payload.metadata,
        }
        self.runs[strategy_run_id] = run
        return dict(run)

    async def get_run(self, strategy_run_id):
        run = self.runs.get(strategy_run_id)
        return dict(run) if run else None

    async def update_run_risk(self, strategy_run_id, patch):
        run = self.runs[strategy_run_id]
        state = dict(run.get("runtime_state") or {})
        risk = dict(state.get("risk") or {})
        risk.update(patch)
        state["risk"] = risk
        run["runtime_state"] = state
        run["risk_schema"] = [
            {**field, "value": patch.get(field.get("key"), field.get("value"))}
            for field in run.get("risk_schema", [])
        ]
        return dict(run)

    async def update_run_status(self, strategy_run_id, status, *, state_patch=None):
        run = self.runs[strategy_run_id]
        state = dict(run.get("runtime_state") or {})
        if state_patch:
            state.update(state_patch)
        run["status"] = status
        run["runtime_state"] = state
        return dict(run)

    async def list_live_strategy_open_legs(self, *, strategy_run_id, account_id):
        return [dict(item) for item in self.live_open_legs.get(strategy_run_id, [])]

    async def get_intent_result(self, strategy_run_id, idempotency_key):
        return self.intent_results.get((strategy_run_id, idempotency_key))

    async def save_intent_result(self, *, token_id, strategy_run_id, request, status, result):
        self.intent_results[(strategy_run_id, request.idempotency_key)] = result
        return result


class AlgoWorkerApiTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, repo, *, paper_runtime=None, raw_token="secret-token"):
        return SimpleNamespace(
            headers={"authorization": f"Bearer {raw_token}"},
            app=SimpleNamespace(state=SimpleNamespace(algo_worker_repository=repo, paper_runtime_service=paper_runtime)),
            is_disconnected=AsyncMock(return_value=False),
        )

    async def test_admin_token_creation_allows_explicit_live_kite_scope(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        payload = WorkerTokenCreateRequest(name="ml-worker", account_scope="kite:AB1234", allowed_modes=["paper", "live"])

        with patch("api.routers.algo_workers.require_app_user", return_value=SimpleNamespace(username="admin")):
            response = await create_worker_token(request, payload)

        self.assertEqual(response.account_scope, "kite:AB1234")
        self.assertIn("live", response.allowed_modes)

    async def test_admin_token_creation_rejects_live_without_kite_account_scope(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        payload = WorkerTokenCreateRequest(name="ml-worker", account_scope="paper-a", allowed_modes=["live"])

        with patch("api.routers.algo_workers.require_app_user", return_value=SimpleNamespace(username="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await create_worker_token(request, payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("kite:<broker_user_id>", ctx.exception.detail)

    async def test_admin_token_creation_rejects_live_paper_account_scope(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        payload = WorkerTokenCreateRequest(name="ml-worker", account_scope="kite:paper-a", allowed_modes=["live"])

        with patch("api.routers.algo_workers.require_app_user", return_value=SimpleNamespace(username="admin")):
            with self.assertRaises(HTTPException) as ctx:
                await create_worker_token(request, payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("real broker", ctx.exception.detail)

    async def test_worker_can_create_paper_run_and_submit_idempotent_basket_intent(self):
        repo = _FakeWorkerRepository()
        paper_runtime = SimpleNamespace(place_basket=AsyncMock(return_value={"mode": "paper", "status": "success", "results": []}))
        request = self._request(repo, paper_runtime=paper_runtime)

        run = await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-worker-1",
                template_id="mean_reversion",
                account_scope="kite:paper-a",
                execution_mode="paper",
                risk_schema=[{"key": "stop_loss_pct", "label": "Stop loss", "type": "number", "value": 1.2}],
            ),
        )

        self.assertEqual(run["strategy_run_id"], "run-worker-1")

        payload = WorkerIntentRequest(
            intent_type="place_basket",
            idempotency_key="entry-0001",
            payload={"orders": [{"exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 1}]},
        )
        first = await submit_worker_intent(request, "run-worker-1", payload)
        second = await submit_worker_intent(request, "run-worker-1", payload)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "deduped")
        paper_runtime.place_basket.assert_awaited_once()
        call = paper_runtime.place_basket.await_args.kwargs
        self.assertEqual(call["account_scope"], "kite:paper-a")
        self.assertEqual(call["attribution"]["strategy_run_id"], "run-worker-1")
        self.assertEqual(call["attribution"]["source"], "algo_worker")

    async def test_worker_risk_patch_updates_runtime_state_and_schema_values(self):
        repo = _FakeWorkerRepository()
        request = self._request(repo)
        await create_worker_run(
            request,
            WorkerRunCreateRequest(
                strategy_run_id="run-risk",
                template_id="momentum",
                account_scope="kite:paper-a",
                risk_schema=[{"key": "trailing_distance", "label": "Trail", "type": "number", "value": 3.0}],
            ),
        )

        updated = await patch_worker_run_risk(request, "run-risk", WorkerRiskPatchRequest(patch={"trailing_distance": 2.0}))

        self.assertEqual(updated["runtime_state"]["risk"]["trailing_distance"], 2.0)
        self.assertEqual(updated["risk_schema"][0]["value"], 2.0)

    async def test_live_run_requires_strategy_metadata(self):
        token = WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-live",
                    template_id="mean_reversion",
                    account_scope="kite:AB1234",
                    execution_mode="live",
                    metadata={"strategy_family": "indicator_strategy"},
                ),
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("strategy_name", ctx.exception.detail)

    async def test_live_run_rejects_unknown_strategy_family(self):
        token = WorkerToken(
            token_id="worker-1",
            name="test-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        request = self._request(repo)

        with self.assertRaises(HTTPException) as ctx:
            await create_worker_run(
                request,
                WorkerRunCreateRequest(
                    strategy_run_id="run-live",
                    template_id="mean_reversion",
                    account_scope="kite:AB1234",
                    execution_mode="live",
                    metadata={"strategy_family": "unknown", "strategy_name": "Mean Reversion"},
                ),
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("strategy_family", ctx.exception.detail)

    async def test_live_worker_intent_routes_through_live_order_service_with_attribution(self):
        sys.modules.pop("broker_api.kite_orders", None)
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {
                "strategy_family": "indicator_strategy",
                "strategy_name": "Mean Reversion",
                "entry_surface": "external_algo_worker",
            },
        }
        live_orders = SimpleNamespace(
            place_order=AsyncMock(return_value=SimpleNamespace(order_id="OID-LIVE-1", model_dump=lambda mode="json": {"order_id": "OID-LIVE-1"}))
        )
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = live_orders

        with patch("api.routers.algo_workers._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "api.routers.algo_workers.asyncio.to_thread",
            _run_to_thread_inline,
        ):
            response = await submit_worker_intent(
                request,
                "run-live",
                WorkerIntentRequest(
                    intent_type="place_order",
                    idempotency_key="live-0001",
                    payload={
                        "order": {
                            "exchange": "NSE",
                            "tradingsymbol": "INFY",
                            "transaction_type": "BUY",
                            "variety": "regular",
                            "product": "CNC",
                            "order_type": "MARKET",
                            "quantity": 1,
                        }
                    },
                    metadata={"signal": "zscore-cross"},
                ),
            )

        self.assertEqual(response["status"], "accepted")
        live_orders.place_order.assert_awaited_once()
        call = live_orders.place_order.await_args
        req = call.args[1]
        self.assertEqual(req.attribution["strategy_run_id"], "run-live")
        self.assertEqual(req.attribution["strategy_family"], "indicator_strategy")
        self.assertEqual(req.attribution["strategy_name"], "Mean Reversion")
        self.assertEqual(req.attribution["execution_mode"], "live")
        self.assertEqual(req.attribution["account_ref"], "kite:AB1234")
        self.assertEqual(req.attribution["source"], "algo_worker")
        self.assertEqual(call.kwargs["idempotency_key"], "live-0001")

    async def test_worker_intent_rejects_non_open_run(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-closed"] = {
            "strategy_run_id": "run-closed",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "closed",
        }
        request = self._request(repo, paper_runtime=SimpleNamespace(place_order=AsyncMock()))

        with self.assertRaises(HTTPException) as ctx:
            await submit_worker_intent(
                request,
                "run-closed",
                WorkerIntentRequest(intent_type="place_order", idempotency_key="closed-0001", payload={"order": {}}),
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("open strategy runs", ctx.exception.detail)

    async def test_live_worker_exit_closes_when_reconciled_strategy_is_already_flat(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        paper_runtime = SimpleNamespace(exit_strategy=AsyncMock())
        request = self._request(repo, paper_runtime=paper_runtime)

        with patch("api.routers.algo_workers._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "api.routers.algo_workers._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 0}),
        ), patch("api.routers.algo_workers.asyncio.to_thread", _run_to_thread_inline):
            response = await exit_worker_run(request, "run-live", WorkerExitRequest(reason="target reached"))

        self.assertEqual(response["mode"], "live")
        self.assertEqual(response["status"], "closed")
        self.assertEqual(response["run"]["status"], "closed")
        self.assertEqual(repo.runs["run-live"]["runtime_state"]["exit_reason"], "target reached")
        paper_runtime.exit_strategy.assert_not_called()

    async def test_live_worker_exit_places_reducing_basket_and_keeps_run_exiting_until_flat(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "runtime_state": {},
        }
        repo.live_open_legs["run-live"] = [
            {
                "journal_run_id": "11111111-1111-4111-8111-111111111111",
                "account_id": "kite:AB1234",
                "instrument_token": 408065,
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": "CNC",
                "net_quantity": 1,
                "broker_net_quantity": 1,
            }
        ]
        live_orders = SimpleNamespace(
            place_basket=AsyncMock(
                return_value=SimpleNamespace(
                    model_dump=lambda mode="json": {
                        "status": "success",
                        "results": [{"index": 0, "tradingsymbol": "INFY", "order_id": "OID-EXIT", "status": "success"}],
                        "errors": [],
                    }
                )
            )
        )
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = live_orders

        with patch("api.routers.algo_workers._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "api.routers.algo_workers._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 1}),
        ), patch("api.routers.algo_workers.asyncio.to_thread", _run_to_thread_inline):
            response = await exit_worker_run(request, "run-live", WorkerExitRequest(reason="operator exit", idempotency_key="exit-0001"))

        self.assertEqual(response["mode"], "live")
        self.assertEqual(response["status"], "exiting")
        self.assertEqual(repo.runs["run-live"]["status"], "exiting")
        live_orders.place_basket.assert_awaited_once()
        planned_orders = repo.runs["run-live"]["runtime_state"]["live_exit"]["orders"]
        self.assertEqual(planned_orders[0]["transaction_type"], "SELL")
        self.assertEqual(planned_orders[0]["quantity"], 1)
        self.assertEqual(planned_orders[0]["attribution"]["strategy_run_id"], "run-live")
        self.assertEqual(live_orders.place_basket.await_args.kwargs["idempotency_key"], "exit-0001")

    async def test_live_worker_exit_rejects_when_broker_position_cannot_cover_attributed_leg(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live"] = {
            "strategy_run_id": "run-live",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
            "runtime_state": {},
        }
        repo.live_open_legs["run-live"] = [
            {
                "instrument_token": 408065,
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "product": "CNC",
                "net_quantity": 3,
                "broker_net_quantity": 1,
            }
        ]
        live_orders = SimpleNamespace(place_basket=AsyncMock())
        request = self._request(repo)
        request.app.state.algo_worker_orders_service = live_orders

        with patch("api.routers.algo_workers._load_live_kite_for_account", return_value=SimpleNamespace(access_token="token")), patch(
            "api.routers.algo_workers._refresh_live_account_state",
            AsyncMock(return_value={"account_id": "kite:AB1234", "reconciled_positions": 1}),
        ), patch("api.routers.algo_workers.asyncio.to_thread", _run_to_thread_inline):
            with self.assertRaises(HTTPException) as ctx:
                await exit_worker_run(request, "run-live", WorkerExitRequest(reason="operator exit"))

        self.assertEqual(ctx.exception.status_code, 409)
        live_orders.place_basket.assert_not_called()

    async def test_worker_run_pnl_snapshot_returns_zeroes_for_dry_run(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-dry"] = {
            "strategy_run_id": "run-dry",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "dry_run",
            "status": "open",
            "metadata": {},
        }
        request = self._request(repo)

        response = await get_worker_run_pnl(request, "run-dry")

        self.assertEqual(response["strategy_run_id"], "run-dry")
        self.assertEqual(response["execution_mode"], "dry_run")
        self.assertEqual(response["totals"]["net_pnl"], 0.0)
        self.assertFalse(response["is_realtime"])
        self.assertEqual(response["legs"], [])

    async def test_worker_run_pnl_snapshot_returns_paper_grouped_totals_and_legs(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-paper"] = {
            "strategy_run_id": "run-paper",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
            "status": "open",
            "metadata": {},
        }
        paper_runtime = SimpleNamespace(
            get_strategy_run_pnl=AsyncMock(
                return_value={
                    "currency": "INR",
                    "strategy": {
                        "status": "open",
                        "realized_pnl": 10.0,
                        "unrealized_pnl": 5.5,
                        "last_updated_at": "2026-04-25T12:00:00+00:00",
                        "positions": [
                            {
                                "instrument_token": 408065,
                                "exchange": "NSE",
                                "tradingsymbol": "INFY",
                                "product": "CNC",
                                "net_quantity": 1,
                                "side": "LONG",
                                "average_price": 100.0,
                                "last_price": 105.5,
                                "realized_pnl": 10.0,
                                "unrealized_pnl": 5.5,
                            }
                        ],
                    },
                }
            )
        )
        request = self._request(repo, paper_runtime=paper_runtime)

        response = await get_worker_run_pnl(request, "run-paper")

        self.assertEqual(response["totals"]["gross_pnl"], 15.5)
        self.assertEqual(response["totals"]["charges"], 0.0)
        self.assertEqual(response["legs"][0]["tradingsymbol"], "INFY")
        self.assertEqual(response["legs"][0]["net_pnl"], 15.5)

    async def test_worker_run_pnl_snapshot_returns_live_grouped_totals_and_legs(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-pnl"] = {
            "strategy_run_id": "run-live-pnl",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_journal_repository = SimpleNamespace(
            find_source_link=lambda **kwargs: SimpleNamespace(run_id="11111111-1111-4111-8111-111111111111"),
            list_execution_facts=lambda run_id: [
                SimpleNamespace(
                    id=1,
                    source_type="live_fill",
                    side="BUY",
                    quantity=1,
                    price=100.0,
                    fees_amount=0.8,
                    taxes_amount=0.2,
                    slippage_amount=0.0,
                    fill_timestamp=datetime.fromisoformat("2026-04-25T12:00:00+00:00"),
                    payload={"broker_fill": {"instrument_token": 408065, "exchange": "NSE", "tradingsymbol": "INFY", "product": "CNC"}},
                )
            ],
        )
        request.app.state.algo_worker_realtime_positions_service = SimpleNamespace(
            get_positions=AsyncMock(
                return_value={
                    "NSE:INFY:CNC": SimpleNamespace(
                        instrument_token=408065,
                        product="CNC",
                        quantity=1,
                        last_price=101.5,
                        last_reconciled_at="2026-04-25T12:00:05+00:00",
                    )
                }
            )
        )

        response = await get_worker_run_pnl(request, "run-live-pnl")

        self.assertEqual(response["totals"]["realized_pnl"], 0.0)
        self.assertEqual(response["totals"]["unrealized_pnl"], 1.5)
        self.assertEqual(response["totals"]["charges"], 1.0)
        self.assertEqual(response["totals"]["net_pnl"], 0.5)
        self.assertEqual(response["legs"][0]["broker_net_quantity"], 1)
        self.assertFalse(response["is_stale"])

    async def test_worker_run_pnl_snapshot_marks_live_leg_stale_when_broker_quantity_sign_is_opposite(self):
        token = WorkerToken(
            token_id="worker-live",
            name="live-worker",
            account_scope="kite:AB1234",
            allowed_modes=["live"],
            allowed_actions=sorted(DEFAULT_WORKER_ACTIONS),
            allowed_templates=[],
        )
        repo = _FakeWorkerRepository(token=token)
        repo.runs["run-live-stale"] = {
            "strategy_run_id": "run-live-stale",
            "token_id": "worker-live",
            "template_id": "mean_reversion",
            "account_scope": "kite:AB1234",
            "execution_mode": "live",
            "status": "open",
            "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
        }
        request = self._request(repo)
        request.app.state.algo_worker_journal_repository = SimpleNamespace(
            find_source_link=lambda **kwargs: SimpleNamespace(run_id="11111111-1111-4111-8111-111111111111"),
            list_execution_facts=lambda run_id: [
                SimpleNamespace(
                    id=1,
                    source_type="live_fill",
                    side="BUY",
                    quantity=1,
                    price=100.0,
                    fees_amount=0.0,
                    taxes_amount=0.0,
                    slippage_amount=0.0,
                    fill_timestamp=datetime.fromisoformat("2026-04-25T12:00:00+00:00"),
                    payload={"broker_fill": {"instrument_token": 408065, "exchange": "NSE", "tradingsymbol": "INFY", "product": "CNC"}},
                )
            ],
        )
        request.app.state.algo_worker_realtime_positions_service = SimpleNamespace(
            get_positions=AsyncMock(
                return_value={
                    "NSE:INFY:CNC": SimpleNamespace(
                        instrument_token=408065,
                        product="CNC",
                        quantity=-1,
                        last_price=101.5,
                        last_reconciled_at="2026-04-25T12:00:05+00:00",
                    )
                }
            )
        )

        response = await get_worker_run_pnl(request, "run-live-stale")

        self.assertTrue(response["is_stale"])
        self.assertTrue(response["legs"][0]["is_stale"])

    async def test_worker_run_pnl_stream_returns_sse_snapshot(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-dry-stream"] = {
            "strategy_run_id": "run-dry-stream",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "dry_run",
            "status": "open",
            "metadata": {},
        }
        request = self._request(repo)

        response = await stream_worker_run_pnl(request, "run-dry-stream", interval_seconds=0.25)
        chunk = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertIn("run-dry-stream", chunk)
        self.assertIn("data:", chunk)

    async def test_worker_run_pnl_stream_refreshes_run_status_between_events(self):
        repo = _FakeWorkerRepository()
        repo.runs["run-dry-stream-status"] = {
            "strategy_run_id": "run-dry-stream-status",
            "token_id": "worker-1",
            "template_id": "mean_reversion",
            "account_scope": "kite:paper-a",
            "execution_mode": "dry_run",
            "status": "open",
            "metadata": {},
        }
        request = self._request(repo)
        request.is_disconnected = AsyncMock(side_effect=[False, False, True])

        response = await stream_worker_run_pnl(request, "run-dry-stream-status", interval_seconds=0.25)
        first = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]
        repo.runs["run-dry-stream-status"]["status"] = "closed"
        second = await response.body_iterator.__anext__()  # pyright: ignore[reportAttributeAccessIssue]

        self.assertIn('"status": "open"', first)
        self.assertIn('"status": "closed"', second)


if __name__ == "__main__":
    unittest.main()
