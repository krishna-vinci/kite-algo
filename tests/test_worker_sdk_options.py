import sys
from pathlib import Path

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, option_leg  # noqa: E402


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

    assert payload == {
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
        legs=[{"symbol": "NIFTY", "option_type": "CE", "strike": 25000, "expiry": "2026-05-28"}],
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
        legs=[{"symbol": "NIFTY", "option_type": "PE", "strike": 24800, "expiry": "2026-05-28"}],
        protection={"rules": [{"metric": "strategy_mtm", "operator": "lte", "threshold": -1000}]},
        metadata={"note": "nightly"},
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://localhost:8000/api/algo-workers/worker/options/runs"
    assert captured["json"] == {
        "strategy_name": "iron_condor",
        "product": "NRML",
        "legs": [{"symbol": "NIFTY", "option_type": "PE", "strike": 24800, "expiry": "2026-05-28"}],
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
