from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import (  # noqa: E402
    WorkerBasketExecutionsResponse,
    WorkerBracketActionResult,
    WorkerBracketIntent,
    WorkerExecutionEventsResponse,
    WorkerOrderHistoryResponse,
    KiteAlgoWorkerClient,
    AlgoWorkerConfig,
)
from kite_algo_worker._shared import (  # noqa: E402
    build_heartbeat_payload,
    build_intent_payload,
    session_headers,
    split_instruments,
)


class FakeResponse:
    def __init__(self, payload=None, *, text: str = "", lines=None, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text
        self.content = text.encode("utf-8") if text else b"{}"
        self._lines = list(lines or [])
        self.closed = False

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line if decode_unicode else line.encode("utf-8")

    def close(self):
        self.closed = True


def _client() -> KiteAlgoWorkerClient:
    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test"))


def test_shared_builders_preserve_worker_contracts():
    assert split_instruments(["NSE:INFY", 408065, "256265"]) == (["NSE:INFY"], [408065, 256265])
    assert session_headers(None) is None
    assert session_headers("nonce-1") == {"X-Worker-Session-Nonce": "nonce-1"}
    assert build_heartbeat_payload(worker_id=None, status="healthy", metrics=None) == {
        "status": "healthy",
        "metrics": {},
    }
    assert build_intent_payload(
        intent_type="place_order",
        body_key="order",
        body={"tradingsymbol": "INFY"},
        idempotency_key="run-1:entry:1",
        metadata=None,
        safety_token="safe-1",
    ) == {
        "intent_type": "place_order",
        "payload": {"order": {"tradingsymbol": "INFY"}},
        "idempotency_key": "run-1:entry:1",
        "metadata": {},
        "safety_token": "safe-1",
    }


def test_execution_models_parse_nested_payloads_and_preserve_unknown_fields():
    history = WorkerOrderHistoryResponse.model_validate(
        {
            "strategy_run_id": "run-1",
            "order_id": "ord-1",
            "history": [{"order_id": "ord-1", "status": "COMPLETE", "future_key": "kept"}],
            "future_envelope": "kept",
        }
    )
    baskets = WorkerBasketExecutionsResponse.model_validate(
        {
            "strategy_run_id": "run-1",
            "baskets": [
                {
                    "basket_execution_id": "bex-1",
                    "strategy_run_id": "run-1",
                    "status": "completed",
                    "legs": [{"basket_execution_id": "bex-1", "leg_index": 0, "status": "filled"}],
                }
            ],
        }
    )
    bracket = WorkerBracketIntent.model_validate(
        {"bracket_intent_id": "brk-1", "strategy_run_id": "run-1", "status": "armed", "config": {}}
    )
    bracket_action = WorkerBracketActionResult.model_validate(
        {
            "bracket_intent_id": "brk-1",
            "strategy_run_id": "run-1",
            "status": "entry_working",
            "action_required": False,
            "entry_result": {"order_id": "ord-1"},
        }
    )
    events = WorkerExecutionEventsResponse.model_validate(
        {
            "strategy_run_id": "run-1",
            "after_cursor": 0,
            "last_cursor": 7,
            "events": [
                {
                    "cursor": 7,
                    "strategy_run_id": "run-1",
                    "account_id": "kite:a",
                    "event_type": "order.updated",
                    "future_event_key": True,
                }
            ],
        }
    )

    assert history.history[0].raw["future_key"] == "kept"
    assert history.raw["future_envelope"] == "kept"
    assert baskets.baskets[0].legs[0].leg_index == 0
    assert bracket.bracket_intent_id == "brk-1"
    assert bracket_action.entry_result == {"order_id": "ord-1"}
    assert events.events[0].cursor == 7
    assert events.events[0].raw["future_event_key"] is True


def test_sync_observability_methods_use_worker_paths(monkeypatch):
    calls = []
    responses = [
        {"strategy_run_id": "run-1", "order_id": "ord-1", "history": []},
        {"strategy_run_id": "run-1", "baskets": []},
        {"basket_execution_id": "bex-1", "strategy_run_id": "run-1", "status": "active"},
        {"strategy_run_id": "run-1", "bracket_intent_id": "brk-1", "status": "entry_working"},
        {"strategy_run_id": "run-1", "brackets": []},
        {"bracket_intent_id": "brk-1", "strategy_run_id": "run-1", "status": "armed"},
        {"strategy_run_id": "run-1", "bracket_intent_id": "brk-1", "status": "cancelling"},
        {"strategy_run_id": "run-1", "after_cursor": 0, "last_cursor": 0, "events": []},
    ]

    def fake_request(_session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(payload=responses.pop(0), text="payload")

    monkeypatch.setattr("requests.Session.request", fake_request)
    sdk = _client()
    sdk.get_order_history("run-1", "ord-1")
    sdk.list_baskets("run-1", limit=20)
    sdk.get_basket("run-1", "bex-1")
    sdk.create_bracket(
        "run-1",
        entry_order={"quantity": 1},
        stoploss={"trigger_price": 90},
        session_nonce="nonce-1",
    )
    sdk.list_brackets("run-1", limit=20)
    sdk.get_bracket("run-1", "brk-1")
    sdk.cancel_bracket("run-1", "brk-1", session_nonce="nonce-1")
    sdk.list_execution_events("run-1", after_cursor=0, limit=20, event_type="order.updated")

    assert [item[0] for item in calls] == ["GET", "GET", "GET", "POST", "GET", "GET", "POST", "GET"]
    assert calls[0][2]["params"] == {"strategy_run_id": "run-1"}
    assert calls[3][2]["headers"] == {"X-Worker-Session-Nonce": "nonce-1"}
    assert calls[7][2]["params"] == {
        "after_cursor": 0,
        "limit": 20,
        "basket_execution_id": None,
        "event_type": "order.updated",
    }


def test_sync_execution_sse_closes_response(monkeypatch):
    response = FakeResponse(
        lines=[
            "event: execution",
            'data: {"cursor": 4, "event_type": "order.updated"}',
            "",
            "event: end",
            'data: {"done": true}',
        ]
    )
    monkeypatch.setattr("requests.Session.request", lambda *_args, **_kwargs: response)

    events = list(_client().stream_execution_events("run-1", after_cursor=3))

    assert events == [{"cursor": 4, "event_type": "order.updated"}]
    assert response.closed is True


def test_sync_fundamentals_csv_uses_text_transport(monkeypatch):
    response = FakeResponse(text="symbol,metric\nINFY,revenue\n")
    calls = []

    def fake_request(_session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)
    result = _client().export_fundamentals_csv(symbols=["INFY"])

    assert result == "symbol,metric\nINFY,revenue\n"
    assert calls[0][0:2] == ("GET", "http://localhost:8000/api/algo-workers/worker/fundamentals/export.csv")
    assert calls[0][2]["params"] == {
        "symbols": ["INFY"],
        "dataset": "fundamentals_features",
        "schema_version": 1,
    }


@pytest.mark.parametrize("scope", [(None, None), ([], None), (["INFY"], "NIFTY")])
def test_csv_scope_validation(scope):
    with pytest.raises(ValueError):
        _client().export_fundamentals_csv(symbols=scope[0], index=scope[1])
