import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import (  # noqa: E402
    AlgoWorkerConfig,
    BrokerValidationError,
    BackendProtection,
    BasketProtection,
    CostContract,
    WorkerCandle,
    WorkerHistoricalCandles,
    WorkerFundsSegment,
    WorkerFundsSnapshot,
    WorkerGttTrigger,
    WorkerGttWriteResult,
    OrderPreview,
    KiteAlgoWorkerClient,
    KiteAlgoWorkerError,
    PreviewPayload,
    ManagedRun,
    RunConfig,
    SafetyCheckResult,
    OperationalProtection,
    ProtectedPosition,
    WorkerOrderSnapshot,
    RunProtectionState,
    WorkerOrderResult,
    WorkerRunHealthSnapshot,
    WorkerRunPnlLeg,
    WorkerRunPnlSnapshot,
    WorkerTimelineResponse,
    WorkerTradeSnapshot,
    amo_limit_order,
    amo_market_order,
    equity_market_order,
    ensure_run,
    limit_order,
    market_order,
    live_equity_market_order,
    option_market_order,
    preview_then_place_order,
    sl_m_order,
    sl_order,
    wait_for_fresh_candle,
    wait_for_history,
    wait_for_quotes,
    wait_for_terminal_order_state,
    warmup_history,
)
import kite_algo_worker as kite_algo_worker_pkg  # noqa: E402
from broker_api.orders import PlaceOrderRequest  # noqa: E402
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


def test_run_config_builds_create_run_payload_without_mutating_original():
    base = RunConfig(
        template_id="mean-reversion",
        account_scope="kite:paper-a",
        execution_mode="paper",
    )
    updated = base.with_summary_field("symbol", "NSE:INFY").with_metadata(strategy_name="MR Demo").with_risk_patch(stop_loss_pct=1.2)

    assert base.summary_fields == []
    assert base.metadata == {}
    assert base.runtime_state == {}
    assert updated.summary_fields == [{"key": "symbol", "value": "NSE:INFY"}]
    assert updated.metadata["strategy_name"] == "MR Demo"
    assert updated.runtime_state["risk"] == {"stop_loss_pct": 1.2}


def test_run_config_with_allowed_actions_appends_without_dropping_defaults():
    config = RunConfig(template_id="mean-reversion", account_scope="kite:paper-a")

    updated = config.with_allowed_actions("runs:exit", "edit_risk")

    assert config.allowed_actions == ["edit_risk", "exit_strategy"]
    assert updated.allowed_actions == ["edit_risk", "exit_strategy", "runs:exit"]


def test_create_run_from_config_matches_raw_create_run_payload(monkeypatch):
    captured = {}

    def fake_request(_session, method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(payload={"status": "ok"})

    monkeypatch.setattr("requests.Session.request", fake_request)

    sdk_client = client()
    config = RunConfig(
        strategy_run_id="run-1",
        template_id="mean-reversion",
        account_scope="kite:paper-a",
        execution_mode="paper",
        metadata={"strategy_name": "MR Demo"},
    ).with_summary_field("symbol", "NSE:INFY")

    sdk_client.create_run_from_config(config)

    assert captured["json"] == {
        "strategy_run_id": "run-1",
        "template_id": "mean-reversion",
        "account_scope": "kite:paper-a",
        "execution_mode": "paper",
        "summary_fields": [{"key": "symbol", "value": "NSE:INFY"}],
        "risk_schema": [],
        "allowed_actions": ["edit_risk", "exit_strategy"],
        "runtime_state": {},
        "metadata": {"strategy_name": "MR Demo"},
    }


def test_client_run_claims_session_and_releases_on_exit():
    events = []

    class FakeClient(KiteAlgoWorkerClient):
        def get_run(self, strategy_run_id: str):
            raise KiteAlgoWorkerError("missing", status_code=404)

        def create_run_from_config(self, config: RunConfig):
            events.append(("create", config.strategy_run_id))
            return {
                "strategy_run_id": config.strategy_run_id,
                "template_id": config.template_id,
                "account_scope": config.account_scope,
                "execution_mode": config.execution_mode,
            }

        def claim_session(self, strategy_run_id: str):
            events.append(("claim", strategy_run_id))
            return {"strategy_run_id": strategy_run_id, "worker_session_nonce": "nonce-1"}

        def run_heartbeat(self, strategy_run_id: str, *, session_nonce: str, worker_id=None, status="healthy", metrics=None):
            events.append(("heartbeat", strategy_run_id, session_nonce))
            return {"status": "ok"}

        def release_session(self, strategy_run_id: str, *, session_nonce: str):
            events.append(("release", strategy_run_id, session_nonce))
            return {"status": "released"}

    fake = FakeClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
    config = RunConfig(strategy_run_id="run-ctx", template_id="demo", account_scope="kite:paper-a", execution_mode="paper")

    with fake.run(config) as run:
        assert run.run_id == "run-ctx"
        assert run.session_nonce == "nonce-1"

    assert events == [
        ("create", "run-ctx"),
        ("claim", "run-ctx"),
        ("heartbeat", "run-ctx", "nonce-1"),
        ("release", "run-ctx", "nonce-1"),
    ]


def test_client_run_rejects_heartbeat_on_enter_without_claim_session():
    config = RunConfig(strategy_run_id="run-ctx", template_id="demo", account_scope="kite:paper-a", execution_mode="paper")

    with pytest.raises(ValueError, match="heartbeat_on_enter"):
        with client().run(config, claim_session=False, heartbeat_on_enter=True):
            pass


def test_client_run_raises_on_existing_run_contract_mismatch():
    class FakeClientWithExistingRun(KiteAlgoWorkerClient):
        def __init__(self, existing_run):
            super().__init__(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
            self._existing_run = existing_run

        def get_run(self, strategy_run_id: str):
            assert strategy_run_id == self._existing_run["strategy_run_id"]
            return dict(self._existing_run)

    fake = FakeClientWithExistingRun(
        {
            "strategy_run_id": "run-ctx",
            "template_id": "old-template",
            "account_scope": "kite:paper-a",
            "execution_mode": "paper",
        }
    )
    config = RunConfig(strategy_run_id="run-ctx", template_id="new-template", account_scope="kite:paper-a", execution_mode="paper")

    with pytest.raises(KiteAlgoWorkerError, match="RunConfig mismatch"):
        with fake.run(config):
            pass


def test_managed_run_place_order_forwards_session_nonce_and_safety_token():
    captured = {}

    class FakeClient:
        def place_order(self, strategy_run_id, order, idempotency_key, metadata=None, safety_token=None, session_nonce=None):
            captured.update(
                strategy_run_id=strategy_run_id,
                order=dict(order),
                idempotency_key=idempotency_key,
                metadata=dict(metadata or {}),
                safety_token=safety_token,
                session_nonce=session_nonce,
            )
            return {"status": "accepted"}

    run = ManagedRun(
        client=FakeClient(),
        config=RunConfig(template_id="demo", account_scope="kite:paper-a"),
        run={"strategy_run_id": "run-1"},
        session_nonce="nonce-1",
    )
    run.place_order({"exchange": "NSE"}, idempotency_key="run-1:entry:1", safety_token="safe-1")

    assert captured["strategy_run_id"] == "run-1"
    assert captured["safety_token"] == "safe-1"
    assert captured["session_nonce"] == "nonce-1"


def test_managed_run_heartbeat_requires_claimed_session_nonce():
    run = ManagedRun(
        client=object(),
        config=RunConfig(template_id="demo", account_scope="kite:paper-a"),
        run={"strategy_run_id": "run-1"},
        session_nonce=None,
    )

    with pytest.raises(ValueError, match="claimed session nonce"):
        run.heartbeat()


def test_managed_run_refresh_updates_local_run_payload():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_run(self, strategy_run_id):
            self.calls.append(strategy_run_id)
            return {"strategy_run_id": strategy_run_id, "status": "open", "metadata": {"refreshed": True}}

    fake = FakeClient()
    run = ManagedRun(
        client=fake,
        config=RunConfig(template_id="demo", account_scope="kite:paper-a"),
        run={"strategy_run_id": "run-1", "status": "created"},
        session_nonce="nonce-1",
    )

    refreshed = run.refresh()

    assert fake.calls == ["run-1"]
    assert refreshed["metadata"]["refreshed"] is True
    assert run.run["metadata"]["refreshed"] is True


def test_package_version_matches_sdk_pyproject():
    pyproject_text = (SDK_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)

    assert match is not None
    assert kite_algo_worker_pkg.__version__ == match.group(1)


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


def test_safety_check_uses_new_endpoint(monkeypatch):
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(
            payload={
                "strategy_run_id": "run-1",
                "can_trade": True,
                "run_status": "open",
                "safety_token": "abc",
                "token_expires_at": "2026-05-06T10:00:10Z",
                "blocking_reasons": [],
                "generic_protection": {"status": "active"},
                "options_protection": {"applicable": False},
                "evaluated_at": "2026-05-06T10:00:00Z",
            }
        )

    monkeypatch.setattr("requests.Session.request", fake_request)
    sdk_client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
    result = sdk_client.safety_check("run-1")

    assert isinstance(result, SafetyCheckResult)
    assert result.can_trade is True
    assert calls[0][0] == "GET"
    assert calls[0][1] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/safety-check"


def test_claim_session_uses_expected_endpoint(captured_requests):
    client().claim_session("run-1")

    assert captured_requests[0]["method"] == "POST"
    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/claim-session"


def test_release_session_sends_nonce_header(captured_requests):
    client().release_session("run-1", session_nonce="nonce-1")

    assert captured_requests[0]["method"] == "DELETE"
    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/claim-session"
    assert captured_requests[0]["kwargs"]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}


def test_run_heartbeat_sends_nonce_header_and_payload(captured_requests):
    client().run_heartbeat("run-1", session_nonce="nonce-1", worker_id="w-1", status="healthy", metrics={"cpu": 10})

    assert captured_requests[0]["method"] == "POST"
    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/heartbeat"
    assert captured_requests[0]["kwargs"]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}
    assert captured_requests[0]["kwargs"]["json"] == {"worker_id": "w-1", "status": "healthy", "metrics": {"cpu": 10}}


def test_place_order_includes_safety_token_when_supplied(monkeypatch):
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append(kwargs["json"])
        return FakeResponse(payload={"status": "accepted", "result": {"status": "ok"}})

    monkeypatch.setattr("requests.Session.request", fake_request)
    sdk_client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
    sdk_client.place_order(
        "run-1",
        {"exchange": "NSE", "tradingsymbol": "INFY"},
        "run-1:entry:001",
        safety_token="signed-token",
    )

    assert calls[0]["safety_token"] == "signed-token"


def test_place_order_includes_session_nonce_when_supplied(monkeypatch):
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append(kwargs)
        return FakeResponse(payload={"status": "accepted", "result": {"status": "ok"}})

    monkeypatch.setattr("requests.Session.request", fake_request)
    sdk_client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
    sdk_client.place_order(
        "run-1",
        {"exchange": "NSE", "tradingsymbol": "INFY"},
        "run-1:entry:001",
        session_nonce="nonce-1",
    )

    assert calls[0]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}


def test_place_basket_includes_safety_token_when_supplied(monkeypatch):
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append(kwargs["json"])
        return FakeResponse(payload={"status": "accepted", "result": {"status": "ok"}})

    monkeypatch.setattr("requests.Session.request", fake_request)
    sdk_client = KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))
    sdk_client.place_basket(
        "run-1",
        [{"exchange": "NSE", "tradingsymbol": "INFY", "transaction_type": "BUY", "quantity": 1}],
        "run-1:basket:001",
        safety_token="signed-token",
    )

    assert calls[0]["safety_token"] == "signed-token"


def test_patch_risk_and_exit_include_session_nonce(captured_requests):
    client().patch_risk("run-1", {"stop_loss_pct": 1.2}, session_nonce="nonce-1")
    client().exit_run("run-1", session_nonce="nonce-1")

    assert captured_requests[0]["kwargs"]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}
    assert captured_requests[1]["kwargs"]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}


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
        get_candles_snapshot=lambda *args, **kwargs: WorkerHistoricalCandles(
            symbol="NSE:NIFTY 50",
            interval="5minute",
            source="runtime",
            current=WorkerCandle(
                ts="2026-04-28T09:20:00+05:30",
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=1200,
                is_complete=False,
            ),
            candles=[
                WorkerCandle(
                    ts="2026-04-28T09:15:00+05:30",
                    open=99,
                    high=100,
                    low=98.5,
                    close=99.8,
                    volume=1000,
                    is_complete=True,
                )
            ],
            is_stale=False,
        ),
        get_historical_candles=lambda *args, **kwargs: {"candles": [{"ts": "2026-04-28T09:15:00+05:30"}]},
        get_historical_candles_snapshot=lambda *args, **kwargs: WorkerHistoricalCandles(
            symbol="NSE:NIFTY 50",
            timeframe="day",
            candles=[
                WorkerCandle(
                    ts="2026-04-27T15:30:00+05:30",
                    open=99,
                    high=102,
                    low=98,
                    close=101,
                    volume=5000,
                    is_complete=True,
                )
            ],
        ),
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
    assert report["capabilities"]["async_client"] is True
    assert report["capabilities"]["websocket_client"] is True
    assert report["capabilities"]["preview_order"] is True
    assert report["capabilities"]["list_orders"] is True
    assert report["capabilities"]["wait_for_history"] is True
    assert report["capabilities"]["typed_marketdata"] == {
        "available": True,
        "symbol": "NSE:NIFTY 50",
        "typed_snapshot": True,
        "snapshot_type": "WorkerHistoricalCandles",
        "current_type": "WorkerCandle",
        "candle_count": 1,
        "is_stale": False,
        "source": "runtime",
    }
    assert report["capabilities"]["recovery_helpers"] == {
        "wait_for_history": True,
        "warmup_history": True,
        "symbol": "NSE:NIFTY 50",
        "history_ready": True,
        "history_candle_count": 1,
        "warmup_ready": True,
        "warmup_candle_count": 1,
        "warmup_snapshot_type": "WorkerHistoricalCandles",
    }
    websocket_health = report["capabilities"]["websocket_health"]
    assert websocket_health["available"] is True
    assert websocket_health["type"] == "StreamHealth"
    assert set(websocket_health["reconnect_metadata_fields"]) == {
        "reconnect_count",
        "last_reconnect_at",
        "subscription_replayed",
        "subscription_replayed_at",
        "is_stale",
        "last_error",
        "next_reconnect_delay_seconds",
    }
    assert websocket_health["sample"]["stream_name"] == "ticks"
    indicators = report["capabilities"]["indicators"]
    assert indicators["available"] is True
    assert set(indicators["representative"].keys()) == {"sma_last", "ema_last", "rsi_last", "atr_last"}
    assert indicators["representative"]["sma_last"] is not None


def test_marketdata_helpers_fallback_cleanly_without_optional_dependencies():
    if getattr(kite_algo_worker_pkg, "_MARKETDATA_AVAILABLE", True):
        pytest.skip("optional marketdata dependencies are installed in this environment")

    with pytest.raises(ModuleNotFoundError, match="pandas and numpy are required"):
        kite_algo_worker_pkg.candles_to_df([])


def test_indicator_helpers_fallback_cleanly_without_optional_dependencies():
    if getattr(kite_algo_worker_pkg, "NUMBA_AVAILABLE", False) or getattr(kite_algo_worker_pkg, "_MARKETDATA_AVAILABLE", False):
        # Environment-specific optional deps may be present in some runners.
        pass

    try:
        kite_algo_worker_pkg.ta.sma([1, 2, 3], 2)
    except ModuleNotFoundError as exc:
        assert "pandas and numpy are required" in str(exc)


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


def test_get_candles_snapshot_returns_typed_models(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        if url.endswith("/worker/market/candles"):
            return FakeResponse(
                payload={
                    "symbol": "NSE:SBIN",
                    "instrument_token": 123,
                    "interval": "5minute",
                    "current": {
                        "ts": "2026-04-28T09:20:00+05:30",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 1200,
                        "oi": None,
                        "is_complete": False,
                    },
                    "candles": [
                        {
                            "ts": "2026-04-28T09:15:00+05:30",
                            "open": 99,
                            "high": 100,
                            "low": 98.5,
                            "close": 99.8,
                            "volume": 1000,
                            "oi": None,
                            "is_complete": True,
                        }
                    ],
                    "is_stale": False,
                    "source": "runtime",
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr("requests.Session.request", fake_request)

    snapshot = client().get_candles_snapshot("NSE:SBIN")

    assert isinstance(snapshot, WorkerHistoricalCandles)
    assert isinstance(snapshot.current, WorkerCandle)
    assert isinstance(snapshot.candles[0], WorkerCandle)
    assert snapshot.current.close == 100.5
    assert snapshot.candles[0].is_complete is True
    assert snapshot.model_dump()["source"] == "runtime"


def test_get_historical_candles_snapshot_maps_from_and_to(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        if url.endswith("/worker/market/history"):
            return FakeResponse(
                payload={
                    "symbol": "NSE:SBIN",
                    "instrument_token": 123,
                    "timeframe": "day",
                    "interval": "day",
                    "from": "2026-01-01T09:15:00+05:30",
                    "to": "2026-01-02T15:30:00+05:30",
                    "count": 1,
                    "source": "historical_db",
                    "ingestion": {"status": "disabled"},
                    "candles": [
                        {
                            "ts": "2026-01-01T15:30:00+05:30",
                            "open": 99,
                            "high": 100,
                            "low": 98,
                            "close": 99.5,
                            "volume": 500,
                            "oi": 12,
                            "is_complete": True,
                        }
                    ],
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr("requests.Session.request", fake_request)

    snapshot = client().get_historical_candles_snapshot("NSE:SBIN", timeframe="day")

    assert isinstance(snapshot, WorkerHistoricalCandles)
    assert snapshot.from_ts == "2026-01-01T09:15:00+05:30"
    assert snapshot.to_ts == "2026-01-02T15:30:00+05:30"
    assert snapshot.candles[0].close == 99.5
    assert snapshot.model_dump()["from"] == "2026-01-01T09:15:00+05:30"
    assert snapshot.model_dump()["to"] == "2026-01-02T15:30:00+05:30"


def test_get_orders_and_order_snapshot_return_typed_models(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        if url.endswith("/worker/orders"):
            return FakeResponse(
                payload={
                    "strategy_run_id": "run-1",
                    "orders": [
                        {
                            "order_id": "o1",
                            "status": "COMPLETE",
                            "tradingsymbol": "INFY",
                            "quantity": 2,
                            "price": 10.5,
                            "meta": {"source": "worker"},
                        }
                    ],
                }
            )
        if url.endswith("/worker/orders/o1"):
            return FakeResponse(
                payload={
                    "strategy_run_id": "run-1",
                    "order": {
                        "order_id": "o1",
                        "status": "COMPLETE",
                        "tradingsymbol": "INFY",
                        "quantity": 2,
                        "price": 10.5,
                        "meta": {"source": "worker"},
                    },
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr("requests.Session.request", fake_request)

    orders_snapshot = client().get_orders_snapshot("run-1")
    order_snapshot = client().get_order_snapshot("run-1", "o1")

    assert isinstance(orders_snapshot.orders[0], WorkerOrderSnapshot)
    assert orders_snapshot.orders[0].order_id == "o1"
    assert orders_snapshot.orders[0].model_dump()["meta"] == {"source": "worker"}
    assert isinstance(order_snapshot, WorkerOrderSnapshot)
    assert order_snapshot.price == 10.5


def test_raw_model_dump_preserves_unknown_none_fields():
    snapshot = WorkerOrderSnapshot.model_validate(
        {
            "order_id": "o1",
            "status": "COMPLETE",
            "meta": None,
        }
    )

    assert "meta" in snapshot.model_dump()
    assert snapshot.model_dump()["meta"] is None


def test_get_trades_snapshot_returns_typed_models(monkeypatch):
    def fake_request(self, method, url, **kwargs):
        if url.endswith("/worker/trades"):
            return FakeResponse(
                payload={
                    "strategy_run_id": "run-1",
                    "trades": [
                        {
                            "trade_id": "abc",
                            "order_id": "o1",
                            "quantity": 2,
                            "average_price": 10.5,
                            "meta": {"fill": "worker"},
                        }
                    ],
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr("requests.Session.request", fake_request)

    trades_snapshot = client().get_trades_snapshot("run-1")

    assert isinstance(trades_snapshot.trades[0], WorkerTradeSnapshot)
    assert trades_snapshot.trades[0].trade_id == "abc"
    assert trades_snapshot.trades[0].model_dump()["meta"] == {"fill": "worker"}


def test_wait_for_quotes_polls_until_quotes_arrive(monkeypatch):
    responses = [{"quotes": []}, {"quotes": [{"symbol": "NSE:INFY"}]}]
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_quotes", lambda *args, **kwargs: responses.pop(0))

    result = wait_for_quotes(client(), ["NSE:INFY"], attempts=2, sleep_seconds=0)

    assert result["quotes"][0]["symbol"] == "NSE:INFY"


def test_wait_for_terminal_order_state_polls_until_terminal(monkeypatch):
    responses = [
        WorkerOrderSnapshot(order_id="o1", status="OPEN"),
        WorkerOrderSnapshot(order_id="o1", status="COMPLETE", filled_quantity=1),
    ]
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_order_snapshot", lambda *args, **kwargs: responses.pop(0))

    result = wait_for_terminal_order_state(client(), "run-1", "o1", attempts=2, sleep_seconds=0)

    assert isinstance(result, WorkerOrderSnapshot)
    assert result.status == "COMPLETE"


def test_wait_for_fresh_candle_returns_complete_candle(monkeypatch):
    responses = [
        WorkerHistoricalCandles(current=WorkerCandle(ts="2026-04-28T09:15:00+05:30", open=100, high=101, low=99, close=100, volume=10, is_complete=False)),
        WorkerHistoricalCandles(current=WorkerCandle(ts="2026-04-28T09:20:00+05:30", open=101, high=102, low=100, close=101, volume=11, is_complete=True)),
    ]
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_candles_snapshot", lambda *args, **kwargs: responses.pop(0))

    result = wait_for_fresh_candle(client(), "NSE:INFY", attempts=2, sleep_seconds=0)

    assert isinstance(result, WorkerCandle)
    assert result.is_complete is True
    assert result.close == 101.0


def test_warmup_history_returns_typed_snapshot_after_min_candles(monkeypatch):
    responses = [
        WorkerHistoricalCandles(candles=[WorkerCandle(ts="2026-04-28T09:00:00+05:30", open=100, high=101, low=99, close=100, volume=10)]),
        WorkerHistoricalCandles(
            candles=[
                WorkerCandle(ts="2026-04-28T09:00:00+05:30", open=100, high=101, low=99, close=100, volume=10),
                WorkerCandle(ts="2026-04-28T09:05:00+05:30", open=100, high=102, low=99, close=101, volume=12),
            ]
        ),
    ]
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_historical_candles_snapshot", lambda *args, **kwargs: responses.pop(0))

    result = warmup_history(client(), "NSE:INFY", timeframe="5minute", min_candles=2, attempts=2, sleep_seconds=0)

    assert isinstance(result, WorkerHistoricalCandles)
    assert len(result.candles) >= 2


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


def test_sdk_log_decision_event_posts_normalized_payload(captured_requests):
    client().log_decision_event(
        "run-1",
        decision_type="entry",
        action="enter",
        summary="Entered on breakout",
        related_resource_type="basket_execution",
        related_resource_id="basket-1",
        metadata={"signal": "breakout"},
    )

    assert captured_requests[0]["method"] == "POST"
    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/decision-events"
    assert captured_requests[0]["kwargs"]["json"] == {
        "decision_type": "entry",
        "action": "enter",
        "summary": "Entered on breakout",
        "related_resource_type": "basket_execution",
        "related_resource_id": "basket-1",
        "metadata": {"signal": "breakout"},
    }


def test_sdk_timeline_helpers_use_expected_routes(captured_requests):
    client().list_timeline("run-1", after_cursor=10, event_kind="decision")
    client().stream_timeline("run-1", after_cursor=11)

    assert captured_requests[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/timeline"
    assert captured_requests[0]["kwargs"]["params"] == {"after_cursor": 10, "event_kind": "decision"}
    assert captured_requests[1]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/timeline/stream"
    assert captured_requests[1]["kwargs"]["params"] == {"after_cursor": 11}


def test_sdk_timeline_snapshot_is_typed(monkeypatch):
    monkeypatch.setattr(
        KiteAlgoWorkerClient,
        "list_timeline",
        lambda self, run_id, **params: {
            "strategy_run_id": run_id,
            "after_cursor": params.get("after_cursor", 0),
            "last_cursor": 21,
            "events": [
                {
                    "cursor": 21,
                    "strategy_run_id": run_id,
                    "account_id": "kite:AB1234",
                    "basket_execution_id": None,
                    "event_kind": "decision",
                    "event_source": "worker",
                    "event_type": "decision.entry",
                    "related_resource_type": "basket_execution",
                    "related_resource_id": "basket-1",
                    "summary": "Entered on breakout",
                    "payload": {"decision_type": "entry", "action": "enter"},
                    "created_at": "2026-05-07T10:00:00+00:00",
                }
            ],
        },
    )

    snapshot = client().list_timeline_snapshot("run-1", after_cursor=20)

    assert isinstance(snapshot, WorkerTimelineResponse)
    assert snapshot.last_cursor == 21
    assert snapshot.events[0].event_kind == "decision"


def test_get_run_protection_state_returns_runtime_state_fragment(monkeypatch):
    monkeypatch.setattr(KiteAlgoWorkerClient, "get_run", lambda self, run_id: {"runtime_state": {"backend_protection_state": {"status": "active", "generation": 2}}})
    assert client().get_run_protection_state("run-1") == {"status": "active", "generation": 2}


def test_get_run_health_snapshot_is_typed(monkeypatch):
    monkeypatch.setattr(
        KiteAlgoWorkerClient,
        "get_run",
        lambda self, run_id: {
            "strategy_run_id": run_id,
            "status": "open",
            "execution_mode": "paper",
            "account_scope": "kite:paper-a",
            "heartbeat_age_sec": 42,
            "health_status": "healthy",
            "session_status": "claimed",
            "recovery_status": "idle",
            "recovery_action_required": False,
            "worker_session_claimed_at": "2026-05-07T09:15:00Z",
            "last_heartbeat_at": "2026-05-07T09:16:00Z",
        },
    )

    snapshot = client().get_run_health_snapshot("run-1")

    assert isinstance(snapshot, WorkerRunHealthSnapshot)
    assert snapshot.heartbeat_age_sec == 42
    assert snapshot.health_status == "healthy"


def test_gtt_helpers_use_worker_routes(monkeypatch):
    responses = [
        {"trigger_id": 91},
        [{"id": 91, "type": "single", "status": "active", "condition": {"tradingsymbol": "INFY"}, "orders": []}],
        {"id": 91, "type": "single", "status": "active", "condition": {"tradingsymbol": "INFY"}, "orders": []},
        {"trigger_id": 91},
        {"trigger_id": 91},
    ]

    def fake_request(self, method, url, **kwargs):
        payload = responses.pop(0)
        return FakeResponse(payload=payload, text=json.dumps(payload))

    monkeypatch.setattr("requests.Session.request", fake_request)
    sdk = client()

    placed = sdk.place_gtt_snapshot({"type": "single"})
    listed = sdk.list_gtts_snapshot()
    current = sdk.get_gtt_snapshot(91)
    modified = sdk.modify_gtt_snapshot(91, {"type": "single"})
    deleted = sdk.delete_gtt_snapshot(91)

    assert isinstance(placed, WorkerGttWriteResult)
    assert isinstance(listed[0], WorkerGttTrigger)
    assert isinstance(current, WorkerGttTrigger)
    assert modified.trigger_id == 91
    assert deleted.trigger_id == 91


def test_helper_layer_builds_safe_orders_and_recovers_runs(monkeypatch):
    order = live_equity_market_order("IDEA", "BUY", 1, product="MIS")
    amo = amo_limit_order("NSE", "IDEA", "BUY", "MIS", 1, price=9.8)
    amo_market = amo_market_order("NSE", "IDEA", "BUY", "MIS", 1)
    assert order["market_protection"] == -1
    assert order["exchange"] == "NSE"
    assert amo["variety"] == "amo"
    assert amo["price"] == 9.8
    assert amo_market["variety"] == "amo"
    assert amo_market["order_type"] == "MARKET"

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
