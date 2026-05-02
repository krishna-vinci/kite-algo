from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from options.api.execution_router import router as execution_router
from options.execution.store import OptionRunStore, get_option_run_store


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
                },
                {
                    "leg_id": "buy_1",
                    "transaction_type": "BUY",
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "quantity": 75,
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
    assert body["margin"] == {"required": 0.0, "source": "deterministic_stub"}
    assert body["charges"] == {"estimated": 0.0, "source": "deterministic_stub"}
    assert [item["transaction_type"] for item in body["order_plan"]] == ["BUY", "SELL"]
    assert {item["product"] for item in body["order_plan"]} == {"MIS"}


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


def test_exit_default_all_filled_path_marks_exited():
    client, _ = _client()
    strategy_run_id = _create_run(client)
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    client.post(f"/api/options/runs/{strategy_run_id}/enter", json={})
    client.post(f"/api/options/runs/{strategy_run_id}/preview-exit", json={})

    exited = client.post(f"/api/options/runs/{strategy_run_id}/exit", json={})
    assert exited.status_code == 200
    assert exited.json()["status"] == "exited"


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
