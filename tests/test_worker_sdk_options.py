import sys
from pathlib import Path

import pytest

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, KiteAlgoWorkerError, option_leg  # noqa: E402
from kite_algo_worker.options import SpreadSpec, resolve_delta_leg, resolve_offset_leg, resolve_option_contracts, resolve_option_leg, resolve_spread  # noqa: E402
from kite_algo_worker.options.models import OptionExecutionLeg, OptionRunCreateRequest  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}"

    def json(self):
        return self._payload


def _client() -> KiteAlgoWorkerClient:
    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def test_worker_client_exposes_options_namespace():
    assert _client().options is not None


def test_options_preview_entry_uses_generic_preview_basket(monkeypatch):
    client = _client()
    captured = {}

    def fake_preview_basket(strategy_run_id, orders, metadata=None, all_or_none=False):
        captured["strategy_run_id"] = strategy_run_id
        captured["orders"] = list(orders)
        captured["metadata"] = dict(metadata or {})
        captured["all_or_none"] = all_or_none
        return {"preview": {"cost_contract": {"margin_required": 1000}}}

    monkeypatch.setattr(client, "preview_basket", fake_preview_basket)

    result = client.options.preview_entry(
        "run-1",
        [{"tradingsymbol": "NIFTY26MAY25000CE", "transaction_type": "BUY", "quantity": 75}],
        metadata={"source": "options-test"},
        all_or_none=True,
    )

    assert captured["strategy_run_id"] == "run-1"
    assert captured["orders"][0]["tradingsymbol"] == "NIFTY26MAY25000CE"
    assert captured["metadata"] == {"source": "options-test"}
    assert captured["all_or_none"] is True
    assert result["preview"]["cost_contract"]["margin_required"] == 1000


def test_options_list_expiries_uses_worker_safe_options_route(monkeypatch):
    captured = {}

    def fake_request(_session, method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["timeout"] = kwargs.get("timeout")
        return FakeResponse(
            payload={
                "underlying": "NIFTY",
                "expiries": ["2026-05-28"],
                "spot_ltp": 25012.4,
                "updated_at": "2026-04-29T09:15:00Z",
            }
        )

    monkeypatch.setattr("requests.Session.request", fake_request)

    payload = _client().options.list_expiries("nifty")

    assert captured["method"] == "GET"
    assert captured["url"] == "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/expiries"
    assert captured["timeout"] == 3
    assert payload["underlying"] == "NIFTY"
    assert payload["expiries"] == ["2026-05-28"]


def test_option_leg_structure_helper_builds_preview_ready_payload():
    payload = option_leg("NIFTY26MAY25000CE", "BUY", 75, tag="entry")

    assert payload["leg_id"].startswith("leg_")
    assert payload == {
        "leg_id": payload["leg_id"],
        "exchange": "NFO",
        "tradingsymbol": "NIFTY26MAY25000CE",
        "transaction_type": "BUY",
        "order_type": "MARKET",
        "quantity": 75,
        "variety": "regular",
        "tag": "entry",
    }


def test_option_leg_structure_helper_includes_product_when_explicitly_provided():
    payload = option_leg("NIFTY26MAY25000CE", "BUY", 75, product="MIS", tag="entry")

    assert payload["product"] == "MIS"


def test_options_run_and_protection_methods_use_worker_safe_paths(monkeypatch):
    captured = []

    def fake_request(_session, method, url, **kwargs):
        captured.append(
            {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "params": kwargs.get("params"),
                "timeout": kwargs.get("timeout"),
            }
        )
        return FakeResponse(payload={"ok": True})

    monkeypatch.setattr("requests.Session.request", fake_request)
    client = _client()

    client.options.preview_strategy({"strategy_name": "bull_call_spread", "legs": []})
    client.options.create_run(
        strategy_name="bull_call_spread",
        product="MIS",
        legs=[{"tradingsymbol": "NIFTY26MAY25000CE", "transaction_type": "BUY", "quantity": 75}],
        protection={"enabled": True},
        metadata={"source": "sdk-test"},
    )
    client.options.preview_entry("run-42")
    client.options.preview_run_entry("run-42", {"mode": "dry_run"})
    client.options.enter("run-42")
    client.options.preview_exit("run-42")
    client.options.exit("run-42")
    client.options.get_run_state("run-42")
    client.options.update_protection("run-42", {"rules": [{"metric": "combined_premium"}]})
    client.options.get_protection_state("run-42")
    client.options.replay_protection(
        "run-42",
        metric_snapshots=[{"combined_premium": 100.0}, {"combined_premium": 120.0}],
        protection={"rules": [{"metric": "combined_premium", "operator": "gte", "threshold": 120.0}]},
    )

    assert [entry["url"] for entry in captured] == [
        "http://localhost:8000/api/algo-workers/worker/options/strategies/preview",
        "http://localhost:8000/api/algo-workers/worker/options/runs",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/preview-entry",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/preview-entry",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/enter",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/preview-exit",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/exit",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/state",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/protection",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/protection/state",
        "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/protection/replay",
    ]
    assert all("/api/options/" not in entry["url"] for entry in captured)
    assert captured[0]["method"] == "POST"
    assert captured[1]["method"] == "POST"
    assert captured[2]["method"] == "POST"
    assert captured[3]["method"] == "POST"
    assert captured[4]["method"] == "POST"
    assert captured[5]["method"] == "POST"
    assert captured[6]["method"] == "POST"
    assert captured[7]["method"] == "GET"
    assert captured[8]["method"] == "PUT"
    assert captured[8]["json"] == {"rules": [{"metric": "combined_premium"}]}
    assert captured[9]["method"] == "GET"
    assert captured[10]["method"] == "POST"
    assert all(entry["timeout"] == 3 for entry in captured)


def test_options_create_run_payload_includes_product_protection_and_metadata(monkeypatch):
    captured = {}

    def fake_request(_session, method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(payload={"ok": True})

    monkeypatch.setattr("requests.Session.request", fake_request)

    _client().options.create_run(
        strategy_name="iron_condor",
        product="NRML",
        legs=[{"tradingsymbol": "NIFTY26MAY24800PE", "transaction_type": "SELL", "quantity": 50}],
        protection={"rules": [{"metric": "strategy_mtm", "operator": "lte", "threshold": -1000}]},
        metadata={"note": "nightly"},
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://localhost:8000/api/algo-workers/worker/options/runs"
    assert captured["json"] == {
        "strategy_name": "iron_condor",
        "product": "NRML",
        "legs": [
            {
                "leg_id": captured["json"]["legs"][0]["leg_id"],
                "tradingsymbol": "NIFTY26MAY24800PE",
                "transaction_type": "SELL",
                "quantity": 50,
                "exchange": "NFO",
                "product": "NRML",
                "order_type": "MARKET",
                "metadata": {},
            }
        ],
        "protection": {"rules": [{"metric": "strategy_mtm", "operator": "lte", "threshold": -1000}]},
        "metadata": {"note": "nightly"},
    }


def test_options_preview_entry_compatibility_orders_still_use_preview_basket(monkeypatch):
    client = _client()
    captured = {}

    def fake_preview_basket(strategy_run_id, orders, metadata=None, all_or_none=False):
        captured["strategy_run_id"] = strategy_run_id
        captured["orders"] = list(orders)
        captured["metadata"] = dict(metadata or {})
        captured["all_or_none"] = all_or_none
        return {"ok": True}

    monkeypatch.setattr(client, "preview_basket", fake_preview_basket)

    result = client.options.preview_entry(
        "run-compat",
        [{"tradingsymbol": "NIFTY26MAY25000CE", "transaction_type": "BUY", "quantity": 75}],
        metadata={"flow": "compat"},
        all_or_none=True,
    )

    assert captured == {
        "strategy_run_id": "run-compat",
        "orders": [{"tradingsymbol": "NIFTY26MAY25000CE", "transaction_type": "BUY", "quantity": 75}],
        "metadata": {"flow": "compat"},
        "all_or_none": True,
    }
    assert result == {"ok": True}


def test_sdk_option_leg_builder_emits_default_leg_id():
    payload = option_leg("NIFTY26MAY25000CE", "BUY", 75)

    assert payload["leg_id"].startswith("leg_")
    assert payload["transaction_type"] == "BUY"


def test_sdk_option_run_create_request_coerces_typed_legs():
    request = OptionRunCreateRequest(
        strategy_name="bull_call_spread",
        product="MIS",
        legs=[{"tradingsymbol": "NIFTY26MAY25000CE", "transaction_type": "BUY", "quantity": 75}],
    )

    assert isinstance(request.legs[0], OptionExecutionLeg)
    assert request.legs[0].product == "MIS"


def test_option_client_enter_includes_safety_token(monkeypatch):
    captured = {}

    def fake_request(_session, method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(payload={"ok": True})

    monkeypatch.setattr("requests.Session.request", fake_request)

    _client().options.enter("run-42", safety_token="signed-token")

    assert captured["url"] == "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/enter"
    assert captured["json"]["safety_token"] == "signed-token"


def test_option_client_exit_includes_safety_token(monkeypatch):
    captured = {}

    def fake_request(_session, method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(payload={"ok": True})

    monkeypatch.setattr("requests.Session.request", fake_request)

    _client().options.exit("run-42", safety_token="signed-token")

    assert captured["url"] == "http://localhost:8000/api/algo-workers/worker/options/runs/run-42/exit"
    assert captured["json"]["safety_token"] == "signed-token"


def test_option_client_mutations_include_session_nonce(monkeypatch):
    calls = []

    def fake_request(_session, method, url, **kwargs):
        calls.append({"method": method, "url": url, "headers": kwargs.get("headers")})
        return FakeResponse(payload={"ok": True})

    monkeypatch.setattr("requests.Session.request", fake_request)
    sdk = _client()
    sdk.options.enter("run-42", session_nonce="nonce-1")
    sdk.options.exit("run-42", session_nonce="nonce-1")
    sdk.options.update_protection("run-42", {"rules": []}, session_nonce="nonce-1")

    assert calls[0]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}
    assert calls[1]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}
    assert calls[2]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}


def test_resolve_option_contracts_returns_normalized_contract_rows():
    fake_options = type(
        "FakeOptions",
        (),
        {
            "resolve_contracts": staticmethod(
                lambda underlying, payload: {
                    "underlying": underlying,
                    "contracts": [
                        {
                            "tradingsymbol": "NIFTY26MAY25000CE",
                            "instrument_token": 123,
                            "strike": 25000,
                            "option_type": "CE",
                            "expiry_key": "2026-05-28",
                            "lot_size": 75,
                            "ltp": 110.5,
                        }
                    ],
                }
            )
        },
    )()

    resolved = resolve_option_contracts(
        fake_options,
        underlying="NIFTY",
        selection_payload={"legs": [{"option_type": "CE", "moneyness": "ATM"}]},
    )

    assert resolved[0]["tradingsymbol"] == "NIFTY26MAY25000CE"
    assert resolved[0]["lot_size"] == 75


def test_resolve_option_contracts_accepts_worker_route_resolved_key():
    fake_options = type(
        "FakeOptions",
        (),
        {
            "resolve_contracts": staticmethod(
                lambda underlying, payload: {
                    "underlying": underlying,
                    "resolved": [
                        {
                            "tradingsymbol": "NIFTY26MAY25000CE",
                            "instrument_token": 123,
                            "strike": 25000,
                            "option_type": "CE",
                            "expiry_key": "2026-05-28",
                            "lot_size": 75,
                            "ltp": 110.5,
                        }
                    ],
                }
            )
        },
    )()

    resolved = resolve_option_contracts(fake_options, underlying="NIFTY", selection_payload={"legs": [{"option_type": "CE", "offset": "ATM"}]})

    assert resolved[0]["tradingsymbol"] == "NIFTY26MAY25000CE"


def test_resolve_option_leg_builds_single_typed_leg_with_resolution_metadata():
    class FakeOptions:
        @staticmethod
        def resolve_contracts(underlying, payload):
            assert underlying == "NIFTY"
            assert payload == {"expiry": "current_week", "legs": [{"option_type": "CE", "offset": "OTM1"}]}
            return {
                "resolved": [
                    {
                        "tradingsymbol": "NIFTY26MAY25100CE",
                        "instrument_token": 2,
                        "strike": 25100,
                        "option_type": "CE",
                        "expiry_key": "2026-05-28",
                        "lot_size": 75,
                        "ltp": 60.0,
                        "resolver": "offset",
                        "resolution_meta": {"offset": "OTM1"},
                    }
                ]
            }

    leg = resolve_option_leg(
        FakeOptions(),
        underlying="NIFTY",
        product="MIS",
        expiry="current_week",
        selection={"option_type": "CE", "offset": "OTM1"},
        transaction_type="BUY",
        lots=2,
        metadata={"tag": "entry"},
    )

    assert isinstance(leg, OptionExecutionLeg)
    assert leg.quantity == 150
    assert leg.metadata["tag"] == "entry"
    assert leg.metadata["resolver"] == "offset"
    assert leg.metadata["resolution_meta"]["offset"] == "OTM1"


def test_resolve_offset_and_delta_leg_helpers_build_selection_requests():
    calls = []

    class FakeOptions:
        @staticmethod
        def resolve_contracts(underlying, payload):
            calls.append({"underlying": underlying, "payload": payload})
            return {
                "resolved": [
                    {
                        "tradingsymbol": "NIFTY26MAY25000CE",
                        "instrument_token": 1,
                        "strike": 25000,
                        "option_type": "CE",
                        "expiry_key": "2026-05-28",
                        "lot_size": 75,
                        "ltp": 100.0,
                    }
                ]
            }

    resolve_offset_leg(
        FakeOptions(),
        underlying="NIFTY",
        product="MIS",
        expiry="current_week",
        option_type="CE",
        offset="ATM",
        transaction_type="BUY",
    )
    resolve_delta_leg(
        FakeOptions(),
        underlying="NIFTY",
        product="MIS",
        expiry="current_week",
        option_type="PE",
        delta_target=0.3,
        transaction_type="SELL",
    )

    assert calls[0]["payload"] == {"expiry": "current_week", "legs": [{"option_type": "CE", "offset": "ATM"}]}
    assert calls[1]["payload"] == {"expiry": "current_week", "legs": [{"option_type": "PE", "delta_target": 0.3}]}


def test_resolve_spread_builds_option_execution_legs_with_explicit_product():
    calls = []

    class FakeOptions:
        @staticmethod
        def resolve_contracts(underlying, payload):
            calls.append({"underlying": underlying, "payload": payload})
            return {
                "contracts": [
                    {
                        "tradingsymbol": "NIFTY26MAY25000CE",
                        "instrument_token": 1,
                        "strike": 25000,
                        "option_type": "CE",
                        "expiry_key": "2026-05-28",
                        "lot_size": 75,
                        "ltp": 100.0,
                    },
                    {
                        "tradingsymbol": "NIFTY26MAY25100CE",
                        "instrument_token": 2,
                        "strike": 25100,
                        "option_type": "CE",
                        "expiry_key": "2026-05-28",
                        "lot_size": 75,
                        "ltp": 60.0,
                    },
                ]
            }

    spec = SpreadSpec(
        spread_type="vertical_call_spread",
        expiry="current_week",
        legs=[
            {"selection": {"option_type": "CE", "moneyness": "ATM"}, "transaction_type": "BUY", "lots": 1},
            {"selection": {"option_type": "CE", "moneyness": "+1_strike"}, "transaction_type": "SELL", "lots": 1},
        ],
    )

    legs = resolve_spread(FakeOptions(), underlying="NIFTY", product="MIS", spec=spec)

    assert len(calls) == 1
    assert calls[0]["payload"] == {
        "expiry": "current_week",
        "legs": [
            {"option_type": "CE", "moneyness": "ATM"},
            {"option_type": "CE", "moneyness": "+1_strike"},
        ],
    }
    assert [leg.transaction_type for leg in legs] == ["BUY", "SELL"]
    assert all(leg.product == "MIS" for leg in legs)
    assert all(leg.quantity == 75 for leg in legs)


def test_resolve_spread_raises_worker_error_for_missing_contracts():
    class FakeOptions:
        @staticmethod
        def resolve_contracts(underlying, payload):
            _ = (underlying, payload)
            return {"contracts": []}

    spec = SpreadSpec(
        spread_type="vertical_call_spread",
        expiry="current_week",
        legs=[{"selection": {"option_type": "CE", "moneyness": "ATM"}, "transaction_type": "BUY", "lots": 1}],
    )

    with pytest.raises(KiteAlgoWorkerError, match="Expected 1 resolved contracts"):
        resolve_spread(FakeOptions(), underlying="NIFTY", product="MIS", spec=spec)
