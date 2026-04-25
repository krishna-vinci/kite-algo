import sys
from pathlib import Path

import pytest

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.kite_orders", None)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import (  # noqa: E402
    AlgoWorkerConfig,
    KiteAlgoWorkerClient,
    KiteAlgoWorkerError,
    equity_market_order,
    limit_order,
    market_order,
    option_market_order,
    sl_m_order,
    sl_order,
)
from broker_api.kite_orders import PlaceOrderRequest  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", lines=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text
        self.content = text.encode("utf-8") if text else b"{}"
        self._lines = lines or []
        self.closed = False

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line if decode_unicode else line.encode("utf-8")

    def close(self):
        self.closed = True


@pytest.fixture
def captured_requests(monkeypatch):
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs, "headers": dict(self.headers)})
        return FakeResponse(payload={"status": "ok"})

    monkeypatch.setattr("requests.Session.request", fake_request)
    return calls


def client():
    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def test_authorization_header_is_sent(captured_requests):
    client().health()

    assert captured_requests[0]["headers"]["Authorization"] == "Bearer kwa_test"
    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/health"
    assert captured_requests[0]["kwargs"]["timeout"] == 3


def test_create_run_payload_shape(captured_requests):
    client().create_run(
        strategy_run_id="run-1",
        template_id="mean-reversion",
        account_scope="kite:paper-a",
        execution_mode="paper",
        summary_fields=[{"key": "symbol", "value": "INFY"}],
        risk_schema=[{"key": "stop_loss_pct", "value": 1.2}],
        runtime_state={"risk": {"stop_loss_pct": 1.2}},
        metadata={"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
    )

    payload = captured_requests[0]["kwargs"]["json"]
    assert payload == {
        "strategy_run_id": "run-1",
        "template_id": "mean-reversion",
        "account_scope": "kite:paper-a",
        "execution_mode": "paper",
        "summary_fields": [{"key": "symbol", "value": "INFY"}],
        "risk_schema": [{"key": "stop_loss_pct", "value": 1.2}],
        "allowed_actions": ["edit_risk", "exit_strategy"],
        "runtime_state": {"risk": {"stop_loss_pct": 1.2}},
        "metadata": {"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
    }


def test_place_order_requires_idempotency_key(captured_requests):
    for bad_key in ["", "   ", "short", "x" * 161]:
        with pytest.raises(ValueError, match="idempotency_key"):
            client().place_order("run-1", {"exchange": "NSE"}, bad_key)

    assert captured_requests == []


def test_place_basket_wraps_orders_correctly(captured_requests):
    orders = [market_order("NSE", "INFY", "BUY", "CNC", 1)]

    client().place_basket("run-1", orders, "run-1:entry-basket:1", metadata={"signal": "demo"}, all_or_none=True, dry_run=True)

    payload = captured_requests[0]["kwargs"]["json"]
    assert payload["intent_type"] == "place_basket"
    assert payload["idempotency_key"] == "run-1:entry-basket:1"
    assert payload["metadata"] == {"signal": "demo"}
    assert payload["payload"] == {"basket": {"orders": orders, "all_or_none": True, "dry_run": True}}


def test_exit_run_supports_dry_run(captured_requests):
    client().exit_run("run-1", reason="operator preview", idempotency_key="run-1:exit-preview:1", dry_run=True)

    payload = captured_requests[0]["kwargs"]["json"]
    assert payload == {"reason": "operator preview", "idempotency_key": "run-1:exit-preview:1", "dry_run": True}


def test_get_run_pnl_uses_worker_pnl_endpoint(captured_requests):
    client().get_run_pnl("run-1")

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/pnl"


def test_stream_run_pnl_parses_sse_events(monkeypatch):
    response = FakeResponse(
        payload={"ignored": True},
        lines=[": heartbeat", "data: {\"strategy_run_id\": \"run-1\", \"totals\": {\"net_pnl\": 12.5}}"],
    )

    def fake_request(self, method, url, **kwargs):
        assert method == "GET"
        assert url == "http://localhost:8000/api/algo-workers/worker/runs/run-1/pnl/stream"
        assert kwargs["stream"] is True
        assert kwargs["params"] == {"interval_seconds": 0.5}
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    events = list(client().stream_run_pnl("run-1", interval_seconds=0.5))

    assert events == [{"strategy_run_id": "run-1", "totals": {"net_pnl": 12.5}}]
    assert response.closed is True


def test_stream_run_pnl_closes_response_on_non_2xx(monkeypatch):
    response = FakeResponse(status_code=503, payload={"detail": "stream unavailable"})

    def fake_request(self, method, url, **kwargs):
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    with pytest.raises(KiteAlgoWorkerError, match="stream unavailable"):
        list(client().stream_run_pnl("run-1"))

    assert response.closed is True


def test_stream_run_pnl_raises_on_sse_error_event(monkeypatch):
    response = FakeResponse(
        payload={"ignored": True},
        lines=["event: error", "data: {\"detail\": \"temporary backend issue\"}"],
    )

    def fake_request(self, method, url, **kwargs):
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    with pytest.raises(KiteAlgoWorkerError, match="temporary backend issue"):
        list(client().stream_run_pnl("run-1"))

    assert response.closed is True


def test_non_2xx_response_raises_custom_exception(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        return FakeResponse(status_code=409, payload={"detail": "closed strategy runs cannot be edited"})

    monkeypatch.setattr("requests.Session.request", fake_request)

    with pytest.raises(KiteAlgoWorkerError) as ctx:
        client().patch_risk("run-1", {"stop_loss_pct": 0.8})

    assert ctx.value.status_code == 409
    assert ctx.value.response_body == {"detail": "closed strategy runs cannot be edited"}
    assert "closed strategy runs" in str(ctx.value)


def test_order_builders_produce_valid_broker_order_payloads():
    equity = equity_market_order("INFY", "BUY", 1)
    option = option_market_order("NIFTY24APR22500CE", "SELL", 50)

    assert equity == {
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "variety": "regular",
        "product": "CNC",
        "order_type": "MARKET",
        "quantity": 1,
        "validity": "DAY",
    }
    assert option == {
        "exchange": "NFO",
        "tradingsymbol": "NIFTY24APR22500CE",
        "transaction_type": "SELL",
        "variety": "regular",
        "product": "NRML",
        "order_type": "MARKET",
        "quantity": 50,
        "validity": "DAY",
    }
    limit = limit_order("NSE", "INFY", "SELL", "CNC", 1, 1510.5)
    stop_limit = sl_order("NSE", "INFY", "SELL", "CNC", 1, 1489.5, 1490.0)
    slm = sl_m_order("NSE", "INFY", "SELL", "CNC", 1, 1490.0, market_protection=-1)
    assert limit["price"] == 1510.5
    assert stop_limit["order_type"] == "SL"
    assert slm["order_type"] == "SL-M"
    assert slm["trigger_price"] == 1490.0
    assert slm["market_protection"] == -1
    assert "tag" not in slm
    assert "attribution" not in slm

    for payload in [equity, option, limit, stop_limit, slm]:
        validated = PlaceOrderRequest.model_validate(payload)
        assert validated.tradingsymbol == payload["tradingsymbol"]
