import sys
from pathlib import Path

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._payload


def _client() -> KiteAlgoWorkerClient:
    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def test_options_market_methods_use_worker_safe_paths(monkeypatch):
    captured = []

    def fake_request(_session, method, url, **kwargs):
        if url.endswith("/expiries"):
            payload = {
                "underlying": "NIFTY",
                "expiries": ["2026-05-28"],
                "spot_ltp": 25012.4,
                "updated_at": "2026-04-29T09:15:00Z",
            }
        else:
            payload = {"ok": True}
        captured.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
                "timeout": kwargs.get("timeout"),
            }
        )
        return FakeResponse(payload=payload)

    monkeypatch.setattr("requests.Session.request", fake_request)
    client = _client()

    client.options.ensure_session("NIFTY")
    client.options.list_expiries("NIFTY")
    client.options.get_chain("NIFTY", expiry="nearest")
    client.options.get_mini_chain("NIFTY", expiry="nearest", window=2)
    client.options.get_greeks("NIFTY", expiry="nearest")
    client.options.resolve_contracts("NIFTY", {"legs": []})
    client.options.get_pcr("NIFTY", expiry="nearest")
    client.options.get_max_pain("NIFTY", expiry="nearest")

    assert [entry["url"] for entry in captured] == [
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/session",
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/expiries",
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/chain",
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/mini-chain",
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/greeks",
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/selection/resolve",
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/analytics/pcr",
        "http://localhost:8000/api/algo-workers/worker/options/underlyings/NIFTY/analytics/max-pain",
    ]
    assert all("/api/options/" not in entry["url"] for entry in captured)
    assert all(entry["timeout"] == 3 for entry in captured)


def test_options_market_params_and_payload_are_forwarded(monkeypatch):
    captured = []

    def fake_request(_session, method, url, **kwargs):
        captured.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
            }
        )
        return FakeResponse(payload={"ok": True})

    monkeypatch.setattr("requests.Session.request", fake_request)
    client = _client()

    client.options.get_chain("NIFTY", expiry="2026-05-28")
    client.options.get_mini_chain("NIFTY", expiry="nearest", window=7)
    client.options.get_greeks("NIFTY", expiry="next_week")
    client.options.get_pcr("NIFTY", expiry="current_month")
    client.options.get_max_pain("NIFTY", expiry="nearest")
    client.options.resolve_contracts("NIFTY", {"expiry": "nearest", "legs": [{"option_type": "CE", "strike": 22500}]})

    assert captured[0]["params"] == {"expiry": "2026-05-28"}
    assert captured[1]["params"] == {"expiry": "nearest", "window": 7}
    assert captured[2]["params"] == {"expiry": "next_week"}
    assert captured[3]["params"] == {"expiry": "current_month"}
    assert captured[4]["params"] == {"expiry": "nearest"}
    assert captured[5]["method"] == "POST"
    assert captured[5]["json"] == {
        "expiry": "nearest",
        "legs": [{"option_type": "CE", "strike": 22500}],
    }
