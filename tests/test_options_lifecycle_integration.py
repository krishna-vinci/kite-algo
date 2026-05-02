from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from options.api.execution_router import router as execution_router
from options.api.market_router import get_options_session_manager, router as market_router
from options.api.protection_router import router as protection_router
from options.api.strategy_router import router as strategy_router
from options.execution.store import OptionRunStore, get_option_run_store


class _FakeInstrumentRepo:
    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper(), None


class _FakeOptionsSessionManager:
    def __init__(self, snapshot: dict):
        self.instrument_repo = _FakeInstrumentRepo()
        self._snapshot = snapshot

    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper()

    def get_snapshot(self, _underlying: str):
        return self._snapshot


def _build_snapshot() -> dict:
    return {
        "underlying": "NIFTY",
        "spot_ltp": 22520.0,
        "updated_at": "2030-04-29T10:00:00Z",
        "expiries": ["2030-05-09", "2030-05-16"],
        "per_expiry": {
            "2030-05-09": {
                "forward": 22540.25,
                "sigma_expiry": 0.162,
                "atm_strike": 22500,
                "rows": [
                    {
                        "strike": 22450,
                        "CE": {
                            "token": 1001,
                            "tsym": "NIFTY30MAY22450CE",
                            "ltp": 121.0,
                            "oi": 100,
                            "iv": 0.15,
                            "delta": 0.58,
                            "gamma": 0.0012,
                            "theta": -12.1,
                            "vega": 8.2,
                            "rho": 1.2,
                        },
                        "PE": {
                            "token": 2001,
                            "tsym": "NIFTY30MAY22450PE",
                            "ltp": 84.0,
                            "oi": 140,
                            "iv": 0.16,
                            "delta": -0.42,
                            "gamma": 0.0011,
                            "theta": -10.4,
                            "vega": 7.9,
                            "rho": -1.1,
                        },
                    },
                    {
                        "strike": 22500,
                        "CE": {
                            "token": 1002,
                            "tsym": "NIFTY30MAY22500CE",
                            "ltp": 101.0,
                            "oi": 130,
                            "iv": 0.17,
                            "delta": 0.51,
                            "gamma": 0.0014,
                            "theta": -11.3,
                            "vega": 8.6,
                            "rho": 1.0,
                        },
                        "PE": {
                            "token": 2002,
                            "tsym": "NIFTY30MAY22500PE",
                            "ltp": 96.0,
                            "oi": 160,
                            "iv": 0.17,
                            "delta": -0.49,
                            "gamma": 0.0013,
                            "theta": -11.0,
                            "vega": 8.4,
                            "rho": -1.0,
                        },
                    },
                    {
                        "strike": 22550,
                        "CE": {
                            "token": 1003,
                            "tsym": "NIFTY30MAY22550CE",
                            "ltp": 86.0,
                            "oi": 90,
                            "iv": 0.18,
                            "delta": 0.44,
                            "gamma": 0.0010,
                            "theta": -9.8,
                            "vega": 7.2,
                            "rho": 0.9,
                        },
                        "PE": {
                            "token": 2003,
                            "tsym": "NIFTY30MAY22550PE",
                            "ltp": 115.0,
                            "oi": 170,
                            "iv": 0.18,
                            "delta": -0.56,
                            "gamma": 0.0010,
                            "theta": -9.9,
                            "vega": 7.1,
                            "rho": -0.9,
                        },
                    },
                ],
            },
            "2030-05-16": {"atm_strike": 22500, "rows": []},
        },
    }


def _client() -> tuple[TestClient, OptionRunStore]:
    app = FastAPI()
    app.include_router(market_router)
    app.include_router(strategy_router)
    app.include_router(execution_router)
    app.include_router(protection_router)

    store = OptionRunStore()
    app.dependency_overrides[get_option_run_store] = lambda: store
    app.dependency_overrides[get_options_session_manager] = lambda: _FakeOptionsSessionManager(_build_snapshot())
    return TestClient(app), store


def _create_run(client: TestClient, *, product: str = "MIS", protection: dict | None = None) -> str:
    payload = {
        "strategy_name": "bull_call_spread",
        "product": product,
        "legs": [
            {
                "leg_id": "sell_1",
                "transaction_type": "SELL",
                "tradingsymbol": "NIFTY30MAY22550CE",
                "quantity": 75,
            },
            {
                "leg_id": "buy_1",
                "transaction_type": "BUY",
                "tradingsymbol": "NIFTY30MAY22500CE",
                "quantity": 75,
            },
        ],
    }
    if protection is not None:
        payload["protection"] = protection
    response = client.post("/api/options/runs", json=payload)
    assert response.status_code == 200
    return response.json()["strategy_run_id"]


def test_market_to_strategy_to_run_to_preview_entry_integration():
    client, _ = _client()

    mini_chain = client.get(
        "/api/options/underlyings/NIFTY/mini-chain",
        params={"expiry": "nearest", "window": 1},
    )
    assert mini_chain.status_code == 200
    mini_body = mini_chain.json()
    assert mini_body["underlying"] == "NIFTY"
    assert mini_body["expiry"] == "2030-05-09"
    assert len(mini_body["contracts"]) == 3
    ce_atm = mini_body["contracts"][1]["ce"]

    greeks = client.get("/api/options/underlyings/NIFTY/greeks", params={"expiry": "nearest"})
    assert greeks.status_code == 200
    greeks_body = greeks.json()
    assert greeks_body["underlying"] == "NIFTY"
    assert greeks_body["contracts"][1]["ce"]["delta"] == 0.51
    assert greeks_body["contracts"][1]["ce"]["gamma"] == 0.0014
    assert greeks_body["greeks_source"] == "synthetic_forward_black76"

    preview = client.post(
        "/api/options/strategies/preview",
        json={
            "underlying": "NIFTY",
            "template_id": "bull_call_spread",
            "strategy_type": "bull_call_spread",
            "current_spot": 22520,
            "legs": [
                {
                    "instrument_token": ce_atm["token"],
                    "tradingsymbol": ce_atm["tsym"],
                    "strike": mini_body["contracts"][1]["strike"],
                    "option_type": "CE",
                    "transaction_type": "BUY",
                    "ltp": ce_atm["ltp"],
                    "lot_size": 75,
                    "lots": 1,
                }
            ],
        },
    )
    assert preview.status_code == 200
    assert preview.json()["strategy"]["inferred_family"] == "directional"

    missing_product = client.post(
        "/api/options/runs",
        json={"strategy_name": "bull_call_spread", "legs": []},
    )
    assert missing_product.status_code == 422

    strategy_run_id = _create_run(client, product="MIS")
    run = client.get(f"/api/options/runs/{strategy_run_id}")
    assert run.status_code == 200
    assert run.json()["product"] == "MIS"

    entry_preview = client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    assert entry_preview.status_code == 200
    entry_body = entry_preview.json()
    assert entry_body["product"] == "MIS"
    assert [item["transaction_type"] for item in entry_body["order_plan"]] == ["BUY", "SELL"]
    assert {item["product"] for item in entry_body["order_plan"]} == {"MIS"}


def test_entry_partial_cleanup_integration_reflects_state_facts():
    client, _ = _client()
    strategy_run_id = _create_run(client)

    preview = client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={})
    assert preview.status_code == 200

    enter = client.post(
        f"/api/options/runs/{strategy_run_id}/enter",
        json={
            "order_results": [
                {"leg_id": "buy_1", "status": "filled"},
                {"leg_id": "sell_1", "status": "rejected"},
            ],
            "trade_results": [
                {
                    "leg_id": "buy_1",
                    "trade_id": "entry_t_buy_1",
                    "tradingsymbol": "NIFTY30MAY22500CE",
                    "quantity": 75,
                    "transaction_type": "BUY",
                }
            ],
        },
    )
    assert enter.status_code == 200
    enter_body = enter.json()
    assert enter_body["status"] in {"partial_entry", "cleanup_required"}
    assert enter_body["status"] == "cleanup_required"
    assert enter_body["completed_legs"] == ["buy_1"]
    assert enter_body["failed_legs"] == ["sell_1"]

    state = client.get(f"/api/options/runs/{strategy_run_id}/state")
    assert state.status_code == 200
    state_body = state.json()["state"]
    assert state_body["status"] == "cleanup_required"
    assert state_body["completed_legs"] == ["buy_1"]
    assert state_body["failed_legs"] == ["sell_1"]


def test_preview_exit_and_exit_integration_full_fill_to_exited():
    client, _ = _client()
    strategy_run_id = _create_run(client, product="NRML")

    assert client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={}).status_code == 200
    enter = client.post(f"/api/options/runs/{strategy_run_id}/enter", json={})
    assert enter.status_code == 200
    assert enter.json()["status"] == "entered"

    exit_preview = client.post(f"/api/options/runs/{strategy_run_id}/preview-exit", json={})
    assert exit_preview.status_code == 200
    assert exit_preview.json()["strategy_run_id"] == strategy_run_id

    exit_response = client.post(f"/api/options/runs/{strategy_run_id}/exit", json={})
    assert exit_response.status_code == 200
    assert exit_response.json()["status"] == "exited"

    state = client.get(f"/api/options/runs/{strategy_run_id}/state")
    orders = client.get(f"/api/options/runs/{strategy_run_id}/orders")
    trades = client.get(f"/api/options/runs/{strategy_run_id}/trades")
    assert state.status_code == 200
    assert state.json()["state"]["status"] == "exited"
    assert orders.status_code == 200 and len(orders.json()["orders"]) >= 4
    assert trades.status_code == 200 and len(trades.json()["trades"]) >= 4


def test_protection_triggered_grouped_exit_uses_run_product_and_open_legs_only():
    client, _ = _client()
    strategy_run_id = _create_run(
        client,
        product="MIS",
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

    assert client.post(f"/api/options/runs/{strategy_run_id}/preview-entry", json={}).status_code == 200
    enter = client.post(
        f"/api/options/runs/{strategy_run_id}/enter",
        json={
            "order_results": [
                {"leg_id": "buy_1", "status": "filled"},
                {"leg_id": "sell_1", "status": "pending"},
            ],
            "trade_results": [
                {
                    "leg_id": "buy_1",
                    "trade_id": "entry_partial_buy",
                    "tradingsymbol": "NIFTY30MAY22500CE",
                    "quantity": 75,
                    "transaction_type": "BUY",
                }
            ],
        },
    )
    assert enter.status_code == 200
    assert enter.json()["status"] == "partial_entry"

    protection_state = client.get(f"/api/options/runs/{strategy_run_id}/protection/state")
    assert protection_state.status_code == 200
    state_body = protection_state.json()
    assert state_body["triggered"] is True
    assert state_body["matched_rule"]["metric"] == "open_quantity"
    assert len(state_body["recommended_exit_orders"]) == 1
    assert state_body["recommended_exit_orders"][0]["tradingsymbol"] == "NIFTY30MAY22500CE"
    assert state_body["recommended_exit_orders"][0]["product"] == "MIS"
    assert state_body["recommended_exit_orders"][0]["market_protection"] == -1

    replay = client.post(
        f"/api/options/runs/{strategy_run_id}/protection/replay",
        json={"metric_snapshots": [{"open_quantity": 0}, {"open_quantity": 2}]},
    )
    assert replay.status_code == 200
    events = replay.json()["events"]
    assert events[0]["triggered"] is False
    assert events[1]["triggered"] is True
    assert len(events[1]["recommended_exit_orders"]) == 1
    assert events[1]["recommended_exit_orders"][0]["product"] == "MIS"
    assert events[1]["recommended_exit_orders"][0]["market_protection"] == -1
