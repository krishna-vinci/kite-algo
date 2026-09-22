"""SDK ``attach_run`` — additive, attach-only behavior.

Pins: it fetches and validates the existing run, preserves the supplied session
nonce, and performs **no** lifecycle side effects (no create/claim/heartbeat/
release). ``client.run`` is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import (  # noqa: E402
    AlgoWorkerConfig,
    KiteAlgoWorkerClient,
    KiteAlgoWorkerError,
    ManagedRun,
    RunConfig,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = ""
        self.content = b"{}"

    def json(self):
        return self._payload


def client():
    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


RUN = {
    "strategy_run_id": "run-1",
    "token_id": "worker_child",
    "template_id": "hosted:hs_1",
    "account_scope": "kite:paper",
    "execution_mode": "paper",
    "status": "open",
    "worker_session_nonce": "wsn_server_side",
}


def _capture(monkeypatch, calls, payload=RUN):
    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return FakeResponse(payload=payload)

    monkeypatch.setattr("requests.Session.request", fake_request)


def test_attach_run_fetches_and_does_no_lifecycle_calls(monkeypatch):
    calls = []
    _capture(monkeypatch, calls)

    config = RunConfig(template_id="hosted:hs_1", account_scope="kite:paper", execution_mode="paper")
    managed = client().attach_run("run-1", session_nonce="wsn_sup", config=config)

    assert isinstance(managed, ManagedRun)
    assert managed.session_nonce == "wsn_sup"  # supplied nonce preserved, not the run's
    assert managed.run_id == "run-1"
    assert [c["method"] for c in calls] == ["GET"]
    assert calls[0]["url"].endswith("/worker/runs/run-1")
    # No claim / heartbeat / release / create were issued.
    assert not any(
        "claim-session" in c["url"] or "heartbeat" in c["url"] or c["method"] in ("POST", "DELETE")
        for c in calls
    )


def test_attach_run_rejects_config_mismatch(monkeypatch):
    calls = []
    _capture(monkeypatch, calls)
    config = RunConfig(template_id="hosted:other", account_scope="kite:paper", execution_mode="paper")
    with pytest.raises(KiteAlgoWorkerError) as exc:
        client().attach_run("run-1", session_nonce="n", config=config)
    assert exc.value.status_code == 409


def test_attach_run_accepts_a_live_run_and_a_live_config(monkeypatch):
    """Live is an ordinary mode value: the attach consistency check forwards it.

    A hosted child launched in live mode must be able to attach to its own live
    run; nothing in the SDK narrows the mode vocabulary to paper/dry-run.
    """
    calls = []
    live_run = {**RUN, "execution_mode": "live", "account_scope": "kite:live"}
    _capture(monkeypatch, calls, payload=live_run)

    config = RunConfig(template_id="hosted:hs_1", account_scope="kite:live", execution_mode="live")
    managed = client().attach_run("run-1", session_nonce="wsn_sup", config=config)

    assert managed.config.execution_mode == "live"
    assert managed.run["execution_mode"] == "live"
