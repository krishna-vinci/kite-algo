from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from options.api.execution_router import router as execution_router
from options.api.protection_router import router as protection_router
from options.execution.store import OptionRunStore, get_option_run_store


def _client() -> tuple[TestClient, OptionRunStore]:
    app = FastAPI()
    app.include_router(execution_router)
    app.include_router(protection_router)
    store = OptionRunStore()
    app.dependency_overrides[get_option_run_store] = lambda: store
    return TestClient(app), store


def _create_run(client: TestClient, *, protection: dict | None = None) -> str:
    response = client.post(
        "/api/options/runs",
        json={
            "strategy_name": "iron_condor",
            "product": "NRML",
            "legs": [
                {
                    "leg_id": "sell_ce",
                    "transaction_type": "SELL",
                    "tradingsymbol": "NIFTY26MAY25100CE",
                    "quantity": 75,
                },
                {
                    "leg_id": "sell_pe",
                    "transaction_type": "SELL",
                    "tradingsymbol": "NIFTY26MAY24900PE",
                    "quantity": 75,
                },
            ],
            "protection": protection,
        },
    )
    assert response.status_code == 200
    return response.json()["strategy_run_id"]


def test_get_protection_returns_initial_run_protection():
    client, _store = _client()
    initial = {
        "rules": [
            {
                "metric": "combined_premium",
                "operator": "gte",
                "threshold": 120.0,
                "action": "exit",
            }
        ]
    }
    strategy_run_id = _create_run(client, protection=initial)

    response = client.get(f"/api/options/runs/{strategy_run_id}/protection")
    assert response.status_code == 200
    assert response.json()["protection"] == initial


def test_put_protection_updates_only_config_and_preserves_orders_trades():
    client, _store = _client()
    strategy_run_id = _create_run(client)

    # Persist execution facts first.
    client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    entered = client.post(f"/api/options/runs/{strategy_run_id}/enter", json={})
    assert entered.status_code == 200
    before_orders = client.get(f"/api/options/runs/{strategy_run_id}/orders").json()["orders"]
    before_trades = client.get(f"/api/options/runs/{strategy_run_id}/trades").json()["trades"]

    payload = {
        "rules": [
            {
                "metric": "open_quantity",
                "operator": "gte",
                "threshold": 1,
                "action": "exit",
            }
        ],
        "precedence": ["exit"],
    }
    update = client.put(f"/api/options/runs/{strategy_run_id}/protection", json=payload)
    assert update.status_code == 200
    assert update.json()["protection"] == {
        "rules": [
            {
                "key": "rule_1",
                "metric": "open_quantity",
                "operator": "gte",
                "threshold": 1.0,
                "role": "exit",
                "action": "exit",
            }
        ],
        "precedence": ["exit"],
    }

    after_orders = client.get(f"/api/options/runs/{strategy_run_id}/orders").json()["orders"]
    after_trades = client.get(f"/api/options/runs/{strategy_run_id}/trades").json()["trades"]
    assert after_orders == before_orders
    assert after_trades == before_trades


def test_get_protection_state_non_triggering_returns_false():
    client, store = _client()
    strategy_run_id = _create_run(
        client,
        protection={
            "rules": [
                {
                    "metric": "combined_premium",
                    "operator": "gte",
                    "threshold": 9999,
                    "action": "exit",
                }
            ]
        },
    )
    run = store.get_run(strategy_run_id)
    run.metadata["protection_metrics"] = {"combined_premium": 120.0}
    store.save_run(run)

    response = client.get(f"/api/options/runs/{strategy_run_id}/protection/state")
    assert response.status_code == 200
    body = response.json()
    assert body["triggered"] is False
    assert body["matched_rule"] is None
    assert body["recommended_exit_orders"] == []


def test_state_and_replay_triggered_return_rule_and_exit_orders_with_run_product():
    client, store = _client()
    strategy_run_id = _create_run(
        client,
        protection={
            "rules": [
                {
                    "metric": "open_quantity",
                    "operator": "gte",
                    "threshold": 1,
                    "action": "exit",
                }
            ]
        },
    )
    run = store.get_run(strategy_run_id)
    run.completed_legs = ["sell_ce", "sell_pe"]
    store.save_run(run)

    state = client.get(f"/api/options/runs/{strategy_run_id}/protection/state")
    assert state.status_code == 200
    state_body = state.json()
    assert state_body["triggered"] is True
    assert state_body["matched_rule"]["metric"] == "open_quantity"
    assert len(state_body["recommended_exit_orders"]) == 2
    assert {order["product"] for order in state_body["recommended_exit_orders"]} == {"NRML"}
    assert {order["market_protection"] for order in state_body["recommended_exit_orders"]} == {-1}

    replay = client.post(
        f"/api/options/runs/{strategy_run_id}/protection/replay",
        json={
            "metric_snapshots": [
                {"open_quantity": 0},
                {"open_quantity": 2},
            ]
        },
    )
    assert replay.status_code == 200
    events = replay.json()["events"]
    assert len(events) == 2
    assert events[0]["index"] == 0 and events[0]["triggered"] is False
    assert events[1]["index"] == 1 and events[1]["triggered"] is True
    assert events[1]["matched_rule"]["metric"] == "open_quantity"
    assert {order["product"] for order in events[1]["recommended_exit_orders"]} == {"NRML"}


def test_protection_exit_recommendations_only_use_open_completed_legs():
    client, store = _client()
    strategy_run_id = _create_run(
        client,
        protection={
            "rules": [
                {
                    "metric": "open_quantity",
                    "operator": "gte",
                    "threshold": 1,
                    "action": "exit",
                }
            ]
        },
    )
    run = store.get_run(strategy_run_id)
    run.completed_legs = ["sell_ce"]
    store.save_run(run)

    state = client.get(f"/api/options/runs/{strategy_run_id}/protection/state")
    assert state.status_code == 200
    orders = state.json()["recommended_exit_orders"]
    assert len(orders) == 1
    assert orders[0]["tradingsymbol"] == "NIFTY26MAY25100CE"
    assert orders[0]["transaction_type"] == "BUY"


def test_missing_run_returns_structured_404_for_all_protection_routes():
    client, _ = _client()
    missing = "opt_run_missing"
    for method, path in [
        ("GET", f"/api/options/runs/{missing}/protection"),
        ("PUT", f"/api/options/runs/{missing}/protection"),
        ("GET", f"/api/options/runs/{missing}/protection/state"),
        ("POST", f"/api/options/runs/{missing}/protection/replay"),
    ]:
        if method == "PUT":
            response = client.put(path, json={"rules": []})
        elif method == "POST":
            response = client.post(path, json={"metric_snapshots": [{"open_quantity": 1}]})
        else:
            response = client.get(path)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "OPTION_RUN_NOT_FOUND"


def test_invalid_protection_config_or_snapshot_returns_4xx_not_500():
    client, _ = _client()
    strategy_run_id = _create_run(client)

    invalid_config = client.put(
        f"/api/options/runs/{strategy_run_id}/protection",
        json={"rules": [{"metric": "unknown_metric", "operator": "gte", "threshold": 1}]},
    )
    assert invalid_config.status_code == 422
    assert invalid_config.json()["detail"]["code"] == "OPTION_PROTECTION_INVALID_CONFIG"

    invalid_snapshot = client.post(
        f"/api/options/runs/{strategy_run_id}/protection/replay",
        json={"metric_snapshots": [{"open_quantity": "abc"}]},
    )
    assert invalid_snapshot.status_code == 422

    missing_snapshots = client.post(
        f"/api/options/runs/{strategy_run_id}/protection/replay",
        json={"metric_snapshots": []},
    )
    assert missing_snapshots.status_code == 400
    assert missing_snapshots.json()["detail"]["code"] == "OPTION_PROTECTION_REPLAY_SNAPSHOTS_REQUIRED"
