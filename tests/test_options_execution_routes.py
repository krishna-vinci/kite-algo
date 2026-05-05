from __future__ import annotations

from decimal import Decimal
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from options.api.execution_router import router as execution_router
from options.execution.store import OptionRunStore, get_option_run_store
from execution_accounting.contracts import ChargesStatus, ExecutionCostContract

execution_router_module = importlib.import_module("options.api.execution_router")


def _client() -> tuple[TestClient, OptionRunStore]:
    app = FastAPI()
    app.include_router(execution_router)
    store = OptionRunStore()
    app.dependency_overrides[get_option_run_store] = lambda: store
    return TestClient(app), store


def _create_run(client: TestClient) -> str:
    response = client.post(
        "/api/options/runs",
        json={
            "strategy_name": "bull_call_spread",
            "product": "MIS",
            "legs": [
                {
                    "leg_id": "sell_1",
                    "transaction_type": "SELL",
                    "tradingsymbol": "NIFTY26MAY25100CE",
                    "quantity": 75,
                    "product": "NRML",
                    "ltp": 82.5,
                },
                {
                    "leg_id": "buy_1",
                    "transaction_type": "BUY",
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "quantity": 75,
                    "ltp": 118.0,
                },
            ],
            "protection": {"stoploss_pct": 20},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "created"
    assert payload["product"] == "MIS"
    return payload["strategy_run_id"]


def test_create_run_requires_product_and_persists_it():
    client, _ = _client()

    missing = client.post("/api/options/runs", json={"strategy_name": "x", "legs": []})
    assert missing.status_code == 422

    strategy_run_id = _create_run(client)
    fetched = client.get(f"/api/options/runs/{strategy_run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["product"] == "MIS"


def test_preview_entry_applies_run_product_and_buy_first_sorting():
    client, _ = _client()
    strategy_run_id = _create_run(client)

    response = client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["strategy_run_id"] == strategy_run_id
    assert body["product"] == "MIS"
    assert body["margin"]["source"] == "paper_estimators"
    assert body["margin"]["required"] > 0
    assert body["charges"]["source"] == "paper_estimators"
    assert body["charges"]["estimated"] > 0
    assert [item["transaction_type"] for item in body["order_plan"]] == ["BUY", "SELL"]
    assert {item["product"] for item in body["order_plan"]} == {"MIS"}


def test_preview_entry_prefers_explicit_order_price_over_ltp():
    client, _ = _client()
    response = client.post(
        "/api/options/runs",
        json={
            "strategy_name": "single_leg_limit",
            "product": "MIS",
            "legs": [
                {
                    "leg_id": "buy_limit_1",
                    "transaction_type": "BUY",
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "quantity": 75,
                    "order_type": "LIMIT",
                    "price": 91.25,
                    "ltp": 118.0,
                }
            ],
        },
    )
    strategy_run_id = response.json()["strategy_run_id"]

    preview = client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    assert preview.status_code == 200
    per_leg = preview.json()["charges"]["per_leg"][0]
    assert per_leg["reference_price"] == 91.25
    assert per_leg["reference_price_source"] == "order_price"


def test_preview_entry_prefers_broker_quoted_contract_when_available(monkeypatch):
    client, _ = _client()
    strategy_run_id = _create_run(client)

    monkeypatch.setattr(
        execution_router_module,
        "_build_broker_preview_contract",
        lambda request, order_plan: ExecutionCostContract(
            margin_required=Decimal("12345"),
            charges_estimate=Decimal("67"),
            total_charges=Decimal("67"),
            charges_status=ChargesStatus.BROKER_QUOTED,
            raw={"source": "test"},
        ),
    )

    response = client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["margin"]["required"] == 12345.0
    assert body["margin"]["source"] == "broker_quoted"
    assert body["charges"]["estimated"] == 67.0
    assert body["charges"]["source"] == "broker_quoted"
    assert body["cost_contract"]["margin_required"] == "12345"


def test_enter_default_all_filled_path_marks_entered_and_persists_facts():
    client, _ = _client()
    strategy_run_id = _create_run(client)
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})

    entered = client.post(f"/api/options/runs/{strategy_run_id}/enter", json={})
    assert entered.status_code == 200
    assert entered.json()["status"] == "entered"

    orders = client.get(f"/api/options/runs/{strategy_run_id}/orders")
    trades = client.get(f"/api/options/runs/{strategy_run_id}/trades")
    state = client.get(f"/api/options/runs/{strategy_run_id}/state")
    assert orders.status_code == 200 and len(orders.json()["orders"]) == 2
    assert trades.status_code == 200 and len(trades.json()["trades"]) == 2
    assert state.status_code == 200 and state.json()["state"]["status"] == "entered"


def test_enter_with_paper_account_scope_routes_through_paper_runtime_service():
    client, _ = _client()
    strategy_run_id = _create_run(client)
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    paper_runtime = SimpleNamespace(
        place_basket=AsyncMock(
            return_value={
                "mode": "paper",
                "status": "success",
                "results": [
                    {
                        "mode": "paper",
                        "status": "filled",
                        "order": {
                            "order_id": "PAPER-1",
                            "tradingsymbol": "NIFTY26MAY25000CE",
                            "transaction_type": "buy",
                            "quantity": 75,
                            "product": "MIS",
                            "metadata": {"leg_id": "buy_1", "strategy_run_id": strategy_run_id},
                        },
                        "trade": {
                            "trade_id": "PTRD-1",
                            "order_id": "PAPER-1",
                            "tradingsymbol": "NIFTY26MAY25000CE",
                            "transaction_type": "buy",
                            "quantity": 75,
                            "product": "MIS",
                            "metadata": {"leg_id": "buy_1", "strategy_run_id": strategy_run_id},
                        },
                    },
                    {
                        "mode": "paper",
                        "status": "filled",
                        "order": {
                            "order_id": "PAPER-2",
                            "tradingsymbol": "NIFTY26MAY25100CE",
                            "transaction_type": "sell",
                            "quantity": 75,
                            "product": "MIS",
                            "metadata": {"leg_id": "sell_1", "strategy_run_id": strategy_run_id},
                        },
                        "trade": {
                            "trade_id": "PTRD-2",
                            "order_id": "PAPER-2",
                            "tradingsymbol": "NIFTY26MAY25100CE",
                            "transaction_type": "sell",
                            "quantity": 75,
                            "product": "MIS",
                            "metadata": {"leg_id": "sell_1", "strategy_run_id": strategy_run_id},
                        },
                    },
                ],
                "errors": [],
            }
        )
    )
    client.app.state.paper_runtime_service = paper_runtime  # type: ignore[attr-defined]

    entered = client.post(
        f"/api/options/runs/{strategy_run_id}/enter",
        json={"account_scope": "kite:paper-e2e"},
    )
    assert entered.status_code == 200
    body = entered.json()
    assert body["mode"] == "paper"
    assert body["status"] == "entered"
    assert len(body["orders"]) == 2
    assert len(body["trades"]) == 2
    assert body["paper_result"]["mode"] == "paper"

    paper_runtime.place_basket.assert_awaited_once()
    call = paper_runtime.place_basket.await_args.kwargs
    assert call["account_scope"] == "kite:paper-e2e"
    assert call["attribution"]["strategy_run_id"] == strategy_run_id
    assert call["attribution"]["execution_mode"] == "paper"
    assert {order["metadata"]["leg_id"] for order in call["basket_payload"]["orders"]} == {"buy_1", "sell_1"}


def test_enter_partial_and_rejected_paths_map_to_partial_entry_and_cleanup_required():
    client, _ = _client()

    run_partial = _create_run(client)
    client.post(f"/api/options/runs/{run_partial}/preview-entry", json={})
    partial = client.post(
        f"/api/options/runs/{run_partial}/enter",
        json={
            "order_results": [
                {"leg_id": "buy_1", "status": "filled"},
                {"leg_id": "sell_1", "status": "pending"},
            ],
            "trade_results": [{"leg_id": "buy_1", "trade_id": "t1", "quantity": 75, "transaction_type": "BUY"}],
        },
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "partial_entry"

    run_cleanup = _create_run(client)
    client.post(f"/api/options/runs/{run_cleanup}/preview-entry", json={})
    cleanup = client.post(
        f"/api/options/runs/{run_cleanup}/enter",
        json={
            "order_results": [
                {"leg_id": "buy_1", "status": "filled"},
                {"leg_id": "sell_1", "status": "rejected"},
            ]
        },
    )
    assert cleanup.status_code == 200
    assert cleanup.json()["status"] == "cleanup_required"


def test_preview_exit_only_considers_open_completed_legs_fallback_when_no_trades():
    client, store = _client()
    strategy_run_id = _create_run(client)
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})

    run = store.get_run(strategy_run_id)
    run.status = "entered"
    run.completed_legs = ["buy_1"]
    store.save_run(run)

    response = client.post(f"/api/options/runs/{strategy_run_id}/preview-exit", json={})
    assert response.status_code == 200
    body = response.json()
    assert [leg["leg_id"] for leg in body["open_legs"]] == ["buy_1"]


def test_preview_exit_applies_universal_buy_first_sorting_and_margin_metadata():
    client, _ = _client()
    strategy_run_id = _create_run(client)
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    client.post(f"/api/options/runs/{strategy_run_id}/enter", json={})

    response = client.post(f"/api/options/runs/{strategy_run_id}/preview-exit", json={})
    assert response.status_code == 200
    body = response.json()
    assert [item["transaction_type"] for item in body["order_plan"]] == ["BUY", "SELL"]
    assert body["margin"]["required"] == 0
    assert body["margin"]["starting_required"] > 0
    assert body["margin"]["final_required"] == 0
    assert body["charges"]["per_leg"][0]["sequence_index"] == 1


def test_exit_default_all_filled_path_marks_exited():
    client, _ = _client()
    strategy_run_id = _create_run(client)
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    client.post(f"/api/options/runs/{strategy_run_id}/enter", json={})
    client.post(f"/api/options/runs/{strategy_run_id}/preview-exit", json={})

    exited = client.post(f"/api/options/runs/{strategy_run_id}/exit", json={})
    assert exited.status_code == 200
    assert exited.json()["status"] == "exited"
    assert [item["transaction_type"] for item in exited.json()["orders"]] == ["BUY", "SELL"]


def test_exit_partial_rejected_path_marks_partial_exit():
    client, _ = _client()
    strategy_run_id = _create_run(client)
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    client.post(f"/api/options/runs/{strategy_run_id}/enter", json={})
    client.post(f"/api/options/runs/{strategy_run_id}/preview-exit", json={})

    partial = client.post(
        f"/api/options/runs/{strategy_run_id}/exit",
        json={
            "order_results": [
                {"leg_id": "buy_1", "status": "filled"},
                {"leg_id": "sell_1", "status": "rejected"},
            ]
        },
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "partial_exit"


def test_missing_run_returns_404_with_structured_code():
    client, _ = _client()
    response = client.get("/api/options/runs/opt_run_missing")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "OPTION_RUN_NOT_FOUND"


def test_invalid_transition_returns_structured_409_not_500():
    client, _ = _client()
    strategy_run_id = _create_run(client)

    invalid = client.post(f"/api/options/runs/{strategy_run_id}/exit", json={})
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "OPTION_RUN_INVALID_TRANSITION"
