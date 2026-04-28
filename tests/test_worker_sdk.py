import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.kite_orders", None)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import (  # noqa: E402
    AlgoWorkerConfig,
    BrokerValidationError,
    BackendProtection,
    BasketProtection,
    CostContract,
    WorkerFundsSegment,
    WorkerFundsSnapshot,
    OrderPreview,
    KiteAlgoWorkerClient,
    KiteAlgoWorkerError,
    PreviewPayload,
    OperationalProtection,
    ProtectedPosition,
    RunProtectionState,
    WorkerOrderResult,
    WorkerRunPnlLeg,
    WorkerRunPnlSnapshot,
    amo_limit_order,
    equity_market_order,
    ensure_run,
    limit_order,
    market_order,
    live_equity_market_order,
    option_market_order,
    preview_then_place_order,
    sl_m_order,
    sl_order,
    wait_for_history,
    wait_for_quotes,
)
from broker_api.kite_orders import PlaceOrderRequest  # noqa: E402
from scripts.sdk_worker_certification import main as sdk_worker_certification_main  # noqa: E402


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


def test_create_run_serializes_backend_protection(captured_requests):
    protection = BackendProtection(
        positions=[
            ProtectedPosition(
                symbol="nse:infy",
                product="cnc",
                side="buy",
                quantity=1,
                entry_price=1500,
                stoploss_pct=2.5,
            )
        ],
        basket=BasketProtection(stoploss_pct=5, trailing_activate_pct=3, trailing_drawdown_pct=1.5),
        operations=OperationalProtection(exit_on_worker_stale=True, worker_stale_sec=300, mis_squareoff_buffer_sec=60),
    )

    client().create_run(
        strategy_run_id="run-1",
        template_id="mean-reversion",
        account_scope="kite:paper-a",
        execution_mode="paper",
        runtime_state={"risk": {"stop_loss_pct": 1.2}},
        backend_protection=protection,
    )

    payload = captured_requests[0]["kwargs"]["json"]
    assert payload["runtime_state"]["risk"] == {"stop_loss_pct": 1.2}
    assert payload["runtime_state"]["backend_protection"] == {
        "enabled": True,
        "mode": "exposure",
        "version": 1,
        "positions": [
            {
                "symbol": "NSE:INFY",
                "product": "CNC",
                "side": "BUY",
                "quantity": 1,
                "entry_price": 1500.0,
                "stoploss_pct": 2.5,
            }
        ],
        "basket": {
            "stoploss_pct": 5.0,
            "trailing_activate_pct": 3.0,
            "trailing_drawdown_pct": 1.5,
        },
        "operations": {
            "exit_on_worker_stale": True,
            "worker_stale_sec": 300,
            "mis_squareoff_buffer_sec": 60,
        },
    }


def test_update_backend_protection_calls_patch_endpoint(captured_requests):
    protection = BackendProtection(basket=BasketProtection(stoploss_pct=5))

    client().update_backend_protection("run-1", protection, reason="rebalance", reset_trailing=False)

    assert captured_requests[0]["method"] == "PATCH"
    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/protection"
    assert captured_requests[0]["kwargs"]["json"] == {
        "backend_protection": {
            "enabled": True,
            "mode": "exposure",
            "version": 1,
            "basket": {"stoploss_pct": 5.0},
        },
        "reason": "rebalance",
        "reset_trailing": False,
    }


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


def test_get_funds_uses_worker_funds_endpoint(captured_requests):
    client().get_funds(mode="live", account_scope="kite:AB1234")

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/funds"
    assert captured_requests[0]["kwargs"]["params"] == {"mode": "live", "account_scope": "kite:AB1234"}


def test_get_run_funds_uses_worker_run_funds_endpoint(captured_requests):
    client().get_run_funds("run-1")

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/funds"


def test_get_quotes_splits_string_symbols_and_int_tokens(captured_requests):
    client().get_quotes(["NSE:INFY", 408065], mode="full")

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/quotes"
    assert captured_requests[0]["kwargs"]["json"] == {
        "symbols": ["NSE:INFY"],
        "instrument_tokens": [408065],
        "mode": "full",
    }


def test_get_quotes_treats_digit_strings_as_tokens(captured_requests):
    client().get_quotes(["408065", "NSE:TCS"])

    assert captured_requests[0]["kwargs"]["json"] == {
        "symbols": ["NSE:TCS"],
        "instrument_tokens": [408065],
        "mode": "quote",
    }


def test_resolve_ticker_uses_expected_endpoint(captured_requests):
    client().resolve_ticker("NSE:INFY")

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/instruments/resolve"
    assert captured_requests[0]["kwargs"]["params"] == {"symbol": "NSE:INFY"}


def test_search_tickers_uses_expected_endpoint(captured_requests):
    client().search_tickers("inf", exchange="NSE", limit=5)

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/instruments/search"
    assert captured_requests[0]["kwargs"]["params"] == {"query": "inf", "limit": 5, "exchange": "NSE"}


def test_get_candles_uses_symbol_endpoint(captured_requests):
    client().get_candles("NSE:INFY", interval="5minute", lookback=25)

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/candles"
    assert captured_requests[0]["kwargs"]["params"] == {
        "interval": "5minute",
        "lookback": 25,
        "symbol": "NSE:INFY",
    }


def test_get_historical_candles_uses_worker_history_endpoint(captured_requests):
    client().get_historical_candles(
        "NSE:INFY",
        timeframe="day",
        from_date="2024-01-01T00:00:00Z",
        to_date="2024-12-31T00:00:00Z",
        ingest=True,
        passthrough=True,
    )

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/history"
    assert captured_requests[0]["kwargs"]["params"] == {
        "timeframe": "day",
        "ingest": True,
        "passthrough": True,
        "symbol": "NSE:INFY",
        "from": "2024-01-01T00:00:00Z",
        "to": "2024-12-31T00:00:00Z",
    }


def test_get_historical_candles_accepts_token(captured_requests):
    client().get_historical_candles(408065, timeframe="5minute", ingest=False)

    assert captured_requests[0]["kwargs"]["params"] == {
        "timeframe": "5minute",
        "ingest": False,
        "passthrough": False,
        "instrument_token": 408065,
    }


def test_get_market_snapshot_uses_expected_endpoint(captured_requests):
    client().get_market_snapshot(
        symbols=["NSE:INFY"],
        instrument_tokens=[408065],
        candles=[{"symbol": "NSE:INFY", "interval": "5minute"}],
        mode="full",
    )

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/snapshot"
    assert captured_requests[0]["kwargs"]["json"] == {
        "symbols": ["NSE:INFY"],
        "instrument_tokens": [408065],
        "candles": [{"symbol": "NSE:INFY", "interval": "5minute"}],
        "mode": "full",
    }


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


def test_stream_ticks_parses_sse_events_and_closes_response(monkeypatch):
    response = FakeResponse(
        payload={"ignored": True},
        lines=[
            ": heartbeat",
            "data: {\"ticks\": [], \"missing\": []}",
            "data: {\"ticks\": [{\"instrument_token\": 408065}]}",
            "event: end",
            "data: {\"done\": true}",
        ],
    )

    def fake_request(self, method, url, **kwargs):
        assert method == "GET"
        assert url == "http://localhost:8000/api/algo-workers/worker/market/ticks/stream"
        assert kwargs["stream"] is True
        assert kwargs["params"] == {"symbols": "NSE:INFY", "tokens": "408065", "mode": "quote"}
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    events = list(client().stream_ticks(["NSE:INFY", 408065], mode="quote"))

    assert events[0] == {"ticks": [], "missing": []}
    assert events[1] == {"ticks": [{"instrument_token": 408065}]}
    assert response.closed is True


def test_stream_candles_parses_sse_events_and_closes_response(monkeypatch):
    response = FakeResponse(
        payload={"ignored": True},
        lines=["data: {\"current\": {\"interval\": \"5minute\", \"close\": 123.4}}"],
    )

    def fake_request(self, method, url, **kwargs):
        assert method == "GET"
        assert url == "http://localhost:8000/api/algo-workers/worker/market/candles/stream"
        assert kwargs["stream"] is True
        assert kwargs["params"] == {"interval": "5minute", "symbol": "NSE:INFY"}
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    events = list(client().stream_candles("NSE:INFY", interval="5minute"))

    assert events == [{"current": {"interval": "5minute", "close": 123.4}}]
    assert response.closed is True


def test_sdk_certification_reports_preview_and_stream_capabilities(monkeypatch, capsys):
    fake_client = SimpleNamespace(
        health=lambda: {"status": "ok"},
        get_funds=lambda **kwargs: {"account_scope": "kite:paper-a"},
        get_quotes=lambda *args, **kwargs: {"quotes": [{"symbol": "NSE:NIFTY 50"}]},
        preview_order=lambda *args, **kwargs: {"preview": {"intent_type": "place_order"}},
    )

    monkeypatch.setattr("scripts.sdk_worker_certification.KiteAlgoWorkerClient", lambda config: fake_client)
    monkeypatch.setenv("KITE_ALGO_WORKER_TOKEN", "kwa_test")
    monkeypatch.setenv("KITE_ALGO_RUN_ID", "run-1")
    monkeypatch.setenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a")
    monkeypatch.setenv("KITE_ALGO_SYMBOL", "NSE:NIFTY 50")
    monkeypatch.setattr(sys, "argv", ["sdk_worker_certification.py", "--mode", "live"])

    assert sdk_worker_certification_main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["preview"] == {"preview": {"intent_type": "place_order"}}
    assert report["capabilities"] == {
        "async_client": True,
        "websocket_client": True,
        "preview_order": True,
        "list_orders": True,
        "wait_for_history": True,
    }


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


def test_stream_ticks_raises_on_sse_error_event(monkeypatch):
    response = FakeResponse(
        payload={"ignored": True},
        lines=["event: error", "data: {\"detail\": \"temporary market issue\"}"],
    )

    def fake_request(self, method, url, **kwargs):
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    with pytest.raises(KiteAlgoWorkerError, match="temporary market issue"):
        list(client().stream_ticks(["NSE:INFY"]))

    assert response.closed is True


def test_stream_ticks_wraps_malformed_sse_json(monkeypatch):
    response = FakeResponse(payload={"ignored": True}, lines=["data: not-json"])

    def fake_request(self, method, url, **kwargs):
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    with pytest.raises(KiteAlgoWorkerError, match="invalid JSON"):
        list(client().stream_ticks(["NSE:INFY"]))

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


def test_typed_models_and_exception_hierarchy():
    preview = OrderPreview.model_validate(
        {
            "strategy_run_id": "run-1",
            "mode": "live",
            "preview": {
                "intent_type": "place_order",
                "cost_contract": {"margin_required": "10.00", "charges_estimate": "2.50"},
            },
        }
    )
    assert preview.preview.cost_contract.margin_required == "10.00"
    assert preview.preview.cost_contract.charges_estimate == "2.50"

    order_result = WorkerOrderResult.model_validate({"mode": "paper", "result": {"status": "filled", "order": {"order_id": "PAPER-1"}}})
    assert order_result.result["order"]["order_id"].startswith("PAPER-")

    state = RunProtectionState.model_validate({"status": "active", "generation": 2})
    assert state.status == "active"
    assert state.generation == 2

    err = BrokerValidationError("bad order", status_code=422, response_body={"detail": "bad order"})
    assert isinstance(err, KiteAlgoWorkerError)
    assert err.status_code == 422


def test_funds_and_pnl_models_validate_nested_payloads():
    funds = WorkerFundsSnapshot.model_validate(
        {
            "account_scope": "kite:paper-a",
            "mode": "paper",
            "source": "paper_runtime",
            "segments": {"equity": {"available_cash": 82000, "net": 100000}},
            "allocation": {"usable_equity_cash": 82000},
            "updated_at": "2026-04-28T09:15:00Z",
        }
    )
    pnl = WorkerRunPnlSnapshot.model_validate(
        {
            "strategy_run_id": "run-1",
            "execution_mode": "paper",
            "status": "open",
            "totals": {"net_pnl": 12.5},
            "legs": [{"tradingsymbol": "INFY", "net_pnl": 12.5}],
            "updated_at": "2026-04-28T09:15:00Z",
        }
    )

    assert isinstance(funds.segments["equity"], WorkerFundsSegment)
    assert funds.segments["equity"].available_cash == 82000.0
    assert isinstance(pnl.legs[0], WorkerRunPnlLeg)
    assert pnl.legs[0].tradingsymbol == "INFY"


def test_get_typed_snapshots_validate_payloads(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        if url.endswith("/worker/funds"):
            return FakeResponse(
                payload={
                    "account_scope": "kite:paper-a",
                    "mode": "paper",
                    "source": "paper_runtime",
                    "segments": {"equity": {"available_cash": "82000", "net": "100000"}},
                    "allocation": {"usable_equity_cash": 82000},
                    "updated_at": "2026-04-28T09:15:00Z",
                }
            )
        if url.endswith("/worker/runs/run-1/pnl"):
            return FakeResponse(
                payload={
                    "strategy_run_id": "run-1",
                    "execution_mode": "paper",
                    "status": "open",
                    "totals": {"net_pnl": "12.5"},
                    "legs": [{"tradingsymbol": "INFY", "net_pnl": "12.5"}],
                    "updated_at": "2026-04-28T09:15:00Z",
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr("requests.Session.request", fake_request)

    funds = client().get_funds_snapshot(mode="paper", account_scope="kite:paper-a")
    pnl = client().get_run_pnl_snapshot("run-1")

    assert funds.segments["equity"].available_cash == 82000.0
    assert pnl.totals.net_pnl == 12.5


def test_wait_for_quotes_polls_until_quotes_arrive(monkeypatch):
    responses = [{"quotes": []}, {"quotes": [{"symbol": "NSE:INFY"}]}]
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_quotes", lambda *args, **kwargs: responses.pop(0))

    result = wait_for_quotes(client(), ["NSE:INFY"], attempts=2, sleep_seconds=0)

    assert result["quotes"][0]["symbol"] == "NSE:INFY"


def test_preview_then_place_order_uses_preview_before_place(monkeypatch):
    calls = []

    monkeypatch.setattr(
        KiteAlgoWorkerClient,
        "preview_order",
        lambda self, run_id, order, metadata=None: calls.append("preview") or {"preview": {"intent_type": "place_order"}},
    )
    monkeypatch.setattr(
        KiteAlgoWorkerClient,
        "place_order",
        lambda self, run_id, order, key, metadata=None: calls.append("place") or {"status": "accepted"},
    )

    result = preview_then_place_order(client(), "run-1", {"exchange": "NSE", "tradingsymbol": "INFY"}, idempotency_key="run-1:entry:1")

    assert result["status"] == "accepted"
    assert calls == ["preview", "place"]


def test_sync_client_order_lifecycle_and_preview_endpoints(captured_requests):
    client().list_orders("run-1")
    client().list_trades("run-1")
    client().cancel_order("run-1", "order-1")
    client().modify_order("run-1", "order-1", {"quantity": 2}, variety="amo")
    client().preview_order("run-1", market_order("NSE", "INFY", "BUY", "CNC", 1))
    client().preview_basket("run-1", [market_order("NSE", "INFY", "BUY", "CNC", 1)], metadata={"source": "test"}, all_or_none=True)

    assert captured_requests[0]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
    assert captured_requests[1]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
    assert captured_requests[2]["kwargs"]["json"] == {"strategy_run_id": "run-1", "variety": "regular"}
    assert captured_requests[3]["kwargs"]["json"] == {"strategy_run_id": "run-1", "variety": "amo", "quantity": 2}
    assert captured_requests[4]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/preview/order"
    assert captured_requests[5]["kwargs"]["json"] == {
        "orders": [market_order("NSE", "INFY", "BUY", "CNC", 1)],
        "metadata": {"source": "test"},
        "all_or_none": True,
    }


def test_get_run_protection_state_returns_runtime_state_fragment(monkeypatch):
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_run", lambda self, run_id: {"runtime_state": {"backend_protection_state": {"status": "active", "generation": 2}}})
    assert client().get_run_protection_state("run-1") == {"status": "active", "generation": 2}


def test_helper_layer_builds_safe_orders_and_recovers_runs(monkeypatch):
    order = live_equity_market_order("IDEA", "BUY", 1, product="MIS")
    amo = amo_limit_order("NSE", "IDEA", "BUY", "MIS", 1, price=9.8)
    assert order["market_protection"] == -1
    assert order["exchange"] == "NSE"
    assert amo["variety"] == "amo"
    assert amo["price"] == 9.8

    responses = [
        {"candles": []},
        {"candles": [{"ts": "2026-04-28T00:00:00Z"}]},
    ]
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_historical_candles", lambda *args, **kwargs: responses.pop(0))
    assert wait_for_history(client(), "NSE:IDEA", timeframe="day", attempts=2, sleep_seconds=0)["candles"]

    def _missing_run(self, run_id):
        raise KiteAlgoWorkerError("missing", status_code=404, response_body={"detail": "missing"})

    monkeypatch.setattr(KiteAlgoWorkerClient, "get_run", _missing_run)
    monkeypatch.setattr(KiteAlgoWorkerClient, "create_run", lambda self, **kwargs: {"created": kwargs})
    created = ensure_run(client(), strategy_run_id="run-2", template_id="mean-reversion", account_scope="kite:paper-a", execution_mode="paper", metadata={"x": 1})
    assert created["created"]["strategy_run_id"] == "run-2"
