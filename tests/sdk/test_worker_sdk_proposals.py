"""SDK transport for proposal submission (G5).

A thin round-trip test: the SDK must carry the envelope to
``POST /worker/proposals`` and hand back exactly what the server said, with no
client-side reinterpretation. Whether a plan is created is the server's decision
— the transport's job is to not lose the rejection reason when it is refused.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import (  # noqa: E402
    AlgoWorkerConfig,
    AsyncKiteAlgoWorkerClient,
    KiteAlgoWorkerClient,
)


class Response:
    def __init__(self, payload=None, status_code: int = 201):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class SyncTransport:
    """Stands in for ``client.session``: a fake with a ``request`` method."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class AsyncTransport:
    """Stands in for ``client.client`` (an httpx.AsyncClient)."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


PAYLOAD = {
    "evaluation_id": "eval-1",
    "evaluation_kind": "run_now",
    "strategy_run_id": "run-1",
    "strategy_id": "stg-A",
    "account_scope": "kite:paper",
    "target_kind": "single_instrument",
    "payload": {
        "instrument_token": 100,
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "product": "CNC",
        "target_quantity": 10,
    },
}

VALIDATED = {
    "proposal_id": "prop-1",
    "status": "validated",
    "plan": {
        "plan_id": "plan-1",
        "proposal_id": "prop-1",
        "strategy_id": "stg-A",
        "account_id": "kite:paper",
        "plan_kind": "single_instrument",
        "plan_hash": "a" * 64,
        "logical_plan": {},
        "resolved_plan": {"legs": []},
        "pinned_catalog_generation": "gen-2",
        "invalidation_state": {"state": "valid"},
    },
    "idempotent": False,
}

REFUSED = {"proposal_id": "prop-2", "status": "refused", "plan": None, "idempotent": False}


def _config() -> AlgoWorkerConfig:
    return AlgoWorkerConfig(base_url="http://worker.test", token="t")


def test_sync_proposals_submit_round_trips():
    transport = SyncTransport(Response(VALIDATED))
    client = KiteAlgoWorkerClient(_config())
    client.session = transport

    result = client.proposals.submit(PAYLOAD)

    method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/worker/proposals")
    assert kwargs["json"] == PAYLOAD
    # The plan is handed back verbatim — the transport never reshapes it.
    assert result["status"] == "validated"
    assert result["plan"]["plan_hash"] == "a" * 64
    assert result["idempotent"] is False
    assert client.submit_proposal(PAYLOAD)["proposal_id"] == "prop-1"


def test_sync_proposals_submit_preserves_a_refusal():
    transport = SyncTransport(Response(REFUSED))
    client = KiteAlgoWorkerClient(_config())
    client.session = transport

    result = client.proposals.submit(PAYLOAD)
    assert result["status"] == "refused"
    assert result["plan"] is None


def test_async_proposals_submit_round_trips():
    transport = AsyncTransport(Response(VALIDATED))
    client = AsyncKiteAlgoWorkerClient(_config())
    object.__setattr__(client, "client", transport)

    result = asyncio.run(client.proposals.submit(PAYLOAD))

    method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/worker/proposals")
    assert kwargs["json"] == PAYLOAD
    assert result["proposal_id"] == "prop-1"
