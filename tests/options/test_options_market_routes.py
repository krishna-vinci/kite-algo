from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.options.api.market_router import get_options_session_manager, router as market_router


class _FakeInstrumentRepo:
    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper(), None


class _FakeManager:
    def __init__(self, snapshot: dict | None):
        self.instrument_repo = _FakeInstrumentRepo()
        self._snapshot = snapshot
        self.start_sessions_calls: list[tuple[list[dict], bool]] = []

    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper()

    def get_snapshot(self, _underlying: str):
        return self._snapshot

    async def start_sessions(self, items: list[dict], replace: bool = False):
        self.start_sessions_calls.append((items, replace))

    def get_watchlist(self):
        return [{"underlying": self._snapshot.get("underlying", "NIFTY"), "is_running": True}] if self._snapshot else []


def _build_snapshot() -> dict:
    return {
        "underlying": "NIFTY",
        "spot_ltp": 22520.0,
        "updated_at": "2026-04-29T10:00:00Z",
        "expiries": ["2026-05-07", "2026-05-14", "2026-05-28"],
        "per_expiry": {
            "2026-05-07": {
                "atm_strike": 22500,
                "rows": [
                    {
                        "strike": 22450,
                        "CE": {"token": 1001, "tsym": "NIFTY26MAY22450CE", "ltp": 121.0, "oi": 100, "delta": 0.58},
                        "PE": {"token": 2001, "tsym": "NIFTY26MAY22450PE", "ltp": 84.0, "oi": 140, "delta": -0.42},
                    },
                    {
                        "strike": 22500,
                        "CE": {"token": 1002, "tsym": "NIFTY26MAY22500CE", "ltp": 101.0, "oi": 130, "delta": 0.51},
                        "PE": {"token": 2002, "tsym": "NIFTY26MAY22500PE", "ltp": 96.0, "oi": 160, "delta": -0.49},
                    },
                    {
                        "strike": 22550,
                        "CE": {"token": 1003, "tsym": "NIFTY26MAY22550CE", "ltp": 86.0, "oi": 90, "delta": 0.44},
                        "PE": {"token": 2003, "tsym": "NIFTY26MAY22550PE", "ltp": 115.0, "oi": 170, "delta": -0.56},
                    },
                ],
            },
            "2026-05-14": {"atm_strike": 22500, "rows": []},
            "2026-05-28": {"atm_strike": 22500, "rows": []},
        },
    }


def _client(snapshot: dict | None):
    app = FastAPI()
    app.include_router(market_router)
    app.dependency_overrides[get_options_session_manager] = lambda: _FakeManager(snapshot)
    return TestClient(app)


def test_market_session_route_returns_snapshot_packet():
    response = _client(_build_snapshot()).get("/api/options/underlyings/NIFTY/session")
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "NIFTY"
    assert "expiries" in body
    assert body["snapshot"]["updated_at"] == "2026-04-29T10:00:00Z"


def test_legacy_market_session_route_returns_raw_snapshot_packet():
    response = _client(_build_snapshot()).get("/api/options/session/NIFTY")
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "NIFTY"
    assert body["spot_ltp"] == 22520.0
    assert "per_expiry" in body


def test_start_sessions_route_delegates_to_manager():
    snapshot = _build_snapshot()
    manager = _FakeManager(snapshot)
    app = FastAPI()
    app.include_router(market_router)
    app.dependency_overrides[get_options_session_manager] = lambda: manager
    client = TestClient(app)

    response = client.post(
        "/api/options/sessions",
        json={
            "replace": False,
            "items": [
                {"underlying": "NIFTY", "window": 12, "cadence_sec": 5},
                {"underlying": "BANKNIFTY", "window": 12, "cadence_sec": 5},
            ],
        },
    )

    assert response.status_code == 200
    assert manager.start_sessions_calls == [([
        {"underlying": "NIFTY", "window": 12, "cadence_sec": 5},
        {"underlying": "BANKNIFTY", "window": 12, "cadence_sec": 5},
    ], False)]


def test_market_routes_return_option_session_not_found_when_missing():
    response = _client(None).get("/api/options/underlyings/NIFTY/session")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "OPTION_SESSION_NOT_FOUND"


def test_market_chain_route_uses_selector_consistently():
    response = _client(_build_snapshot()).get("/api/options/underlyings/NIFTY/chain", params={"expiry": "nearest"})
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "NIFTY"
    assert body["expiry"] == "2026-05-07"
    assert isinstance(body["chain"], list)


def test_market_mini_chain_route_with_window_2():
    response = _client(_build_snapshot()).get(
        "/api/options/underlyings/NIFTY/mini-chain",
        params={"expiry": "nearest", "window": 2},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "NIFTY"
    assert body["expiry"] == "2026-05-07"
    assert "contracts" in body


def test_market_mini_chain_route_rejects_invalid_window():
    response = _client(_build_snapshot()).get(
        "/api/options/underlyings/NIFTY/mini-chain",
        params={"expiry": "nearest", "window": 0},
    )
    assert response.status_code == 422


def test_market_greeks_route_returns_contracts():
    response = _client(_build_snapshot()).get("/api/options/underlyings/NIFTY/greeks", params={"expiry": "nearest"})
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "NIFTY"
    assert body["expiry"] == "2026-05-07"
    assert "contracts" in body


def test_market_selection_resolve_exact_strike():
    response = _client(_build_snapshot()).post(
        "/api/options/underlyings/NIFTY/selection/resolve",
        json={
            "expiry": "nearest",
            "legs": [{"option_type": "CE", "strike": 22500}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "NIFTY"
    assert body["expiry"] == "2026-05-07"
    assert body["resolved"][0]["resolver"] == "exact"
    assert body["resolved"][0]["strike"] == 22500.0


def test_market_selection_resolves_delta_target_from_snapshot_greeks():
    response = _client(_build_snapshot()).post(
        "/api/options/underlyings/NIFTY/selection/resolve",
        json={
            "expiry": "nearest",
            "legs": [{"option_type": "CE", "delta_target": 0.3}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    resolved = body["resolved"][0]
    assert resolved["resolver"] == "delta"
    assert resolved["strike"] == 22550.0
    assert resolved["resolution_meta"]["resolved_delta"] == 0.44


def test_market_selection_resolves_target_delta_alias():
    response = _client(_build_snapshot()).post(
        "/api/options/underlyings/NIFTY/selection/resolve",
        json={
            "expiry": "nearest",
            "legs": [{"option_type": "PE", "target_delta": 0.42}],
        },
    )
    assert response.status_code == 200
    resolved = response.json()["resolved"][0]
    assert resolved["resolver"] == "delta"
    assert resolved["strike"] == 22450.0
    assert resolved["resolution_meta"]["delta_comparison"] == "magnitude"


def test_market_selection_returns_delta_unresolvable_when_snapshot_lacks_deltas():
    snapshot = _build_snapshot()
    for row in snapshot["per_expiry"]["2026-05-07"]["rows"]:
        row["CE"].pop("delta", None)
    response = _client(snapshot).post(
        "/api/options/underlyings/NIFTY/selection/resolve",
        json={
            "expiry": "nearest",
            "legs": [{"option_type": "CE", "delta_target": 0.3}],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "OPTION_DELTA_SELECTION_UNRESOLVABLE"


def test_market_selection_rejects_malformed_offset_contract_token():
    snapshot = _build_snapshot()
    snapshot["per_expiry"]["2026-05-07"]["rows"][2]["CE"]["token"] = "bad-token"
    response = _client(snapshot).post(
        "/api/options/underlyings/NIFTY/selection/resolve",
        json={
            "expiry": "nearest",
            "legs": [{"option_type": "CE", "offset": "OTM1"}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "OPTION_SELECTION_UNRESOLVABLE"
    assert "Invalid instrument token" in detail["message"]


def test_market_analytics_routes_return_value_packets():
    client = _client(_build_snapshot())
    pcr = client.get("/api/options/underlyings/NIFTY/analytics/pcr", params={"expiry": "nearest"})
    max_pain = client.get("/api/options/underlyings/NIFTY/analytics/max-pain", params={"expiry": "nearest"})

    assert pcr.status_code == 200
    assert max_pain.status_code == 200
    assert pcr.json()["underlying"] == "NIFTY"
    assert max_pain.json()["underlying"] == "NIFTY"
    assert pcr.json()["expiry"] == "2026-05-07"
    assert max_pain.json()["expiry"] == "2026-05-07"


def test_market_routes_reject_unavailable_expiry_selector():
    response = _client(_build_snapshot()).get(
        "/api/options/underlyings/NIFTY/chain",
        params={"expiry": date(2026, 6, 4).isoformat()},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "OPTION_INVALID_EXPIRY_SELECTOR"
