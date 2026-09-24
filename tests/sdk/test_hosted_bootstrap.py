"""Hosted child bootstrap — loading the strategy and building ``ctx``.

No network: ``build_context`` is monkeypatched so only the local import and the
``ctx`` shape are exercised.
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

from kite_algo_worker import hosted as hosted_bootstrap  # noqa: E402
from kite_algo_worker.hosted import ChildContext, load_strategy_main  # noqa: E402


def test_load_strategy_main_returns_callable(tmp_path):
    source = tmp_path / "strat.py"
    source.write_text("def main(ctx):\n    return 7\n")
    main = load_strategy_main(str(source))
    assert main(None) == 7


def test_load_strategy_main_requires_main(tmp_path):
    source = tmp_path / "strat.py"
    source.write_text("value = 1\n")
    with pytest.raises(RuntimeError):
        load_strategy_main(str(source))


#: A real strategy shape: future annotations plus ``@dataclass``. Before the
#: loader registered the module in ``sys.modules`` this raised
#: ``AttributeError: 'NoneType' object has no attribute '__dict__'`` from
#: ``dataclasses`` -- the supplied Nifty-500 momentum source failed on exactly
#: this line, inside the real runner, before any strategy code ran.
_DATACLASS_STRATEGY = '''\
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence


class RegimeState(StrEnum):
    RISK_ON = "RISK_ON"


@dataclass(frozen=True)
class Candle:
    session: str
    close: float
    final: bool = True


@dataclass(frozen=True)
class Decision:
    previous_state: RegimeState
    state: RegimeState
    candles: Sequence[Candle] = ()


def main(ctx):
    decision = Decision(RegimeState.RISK_ON, RegimeState.RISK_ON, (Candle("2026-09-23", 10.0),))
    ctx.progress(decision.state.value)
    return 0
'''


def test_dataclass_source_with_string_annotations_loads(tmp_path):
    """The loader registers its module, so ``@dataclass`` can resolve annotations."""
    source = tmp_path / "momentum_like.py"
    source.write_text(_DATACLASS_STRATEGY)
    main = load_strategy_main(str(source))

    import dataclasses

    notes: list = []
    assert main(type("Ctx", (), {"progress": lambda self, note=None: notes.append(note)})()) == 0
    assert notes == ["RISK_ON"]

    module = sys.modules["hosted_strategy"]
    fields = [field.name for field in dataclasses.fields(module.Decision)]
    assert fields == ["previous_state", "state", "candles"]


def test_failed_load_does_not_leave_a_module_behind(tmp_path):
    """A source that dies mid-import must not leave a half-built module registered."""
    sys.modules.pop("hosted_strategy", None)
    source = tmp_path / "explodes.py"
    source.write_text(_DATACLASS_STRATEGY + "\nraise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError):
        load_strategy_main(str(source))
    assert "hosted_strategy" not in sys.modules


def test_failed_load_restores_a_previous_module(tmp_path):
    """The failure path puts back whatever occupied the name before."""
    sentinel = object()
    sys.modules["hosted_strategy"] = sentinel
    try:
        source = tmp_path / "explodes.py"
        source.write_text("raise RuntimeError('boom')\n")
        with pytest.raises(RuntimeError):
            load_strategy_main(str(source))
        assert sys.modules["hosted_strategy"] is sentinel
    finally:
        sys.modules.pop("hosted_strategy", None)


def test_child_context_progress_delegates_to_managed_run(tmp_path):
    class FakeRun:
        def __init__(self):
            self.notes = []

        def progress(self, note=None):
            self.notes.append(note)
            return {"recorded": True}

    run = FakeRun()
    ctx = ChildContext(
        params={"lots": 1},
        client=None,
        run=run,
        scratch=tmp_path,
        template_id="hosted:hs_1",
        execution_mode="paper",
        run_id="run_1",
    )
    assert ctx.progress("tick") == {"recorded": True}
    assert run.notes == ["tick"]


def test_run_child_invokes_strategy_main(tmp_path, monkeypatch):
    source = tmp_path / "strat.py"
    source.write_text("def main(ctx):\n    return ctx.params['answer']\n")
    ctx = ChildContext(
        params={"answer": 42},
        client=None,
        run=None,
        scratch=tmp_path,
        template_id="hosted:hs_1",
        execution_mode="paper",
        run_id="run_1",
    )
    monkeypatch.setattr(hosted_bootstrap, "build_context", lambda: ctx)
    assert hosted_bootstrap.run_child(str(source)) == 42


def test_build_context_pins_live_mode_from_the_child_environment(tmp_path, monkeypatch):
    """``KITE_ALGO_MODE=live`` reaches both ``ctx`` and the attach config.

    The SDK has no allowlist that would reject live; the child's attach config
    must carry the same mode the supervisor launched it with, otherwise the
    attach consistency check would refuse a live run.
    """
    captured: dict[str, object] = {}

    def fake_attach_run(self, run_id, *, session_nonce, config):
        captured["run_id"] = run_id
        captured["session_nonce"] = session_nonce
        captured["config"] = config
        return object()

    monkeypatch.setattr("kite_algo_worker.client.KiteAlgoWorkerClient.attach_run", fake_attach_run)
    monkeypatch.setenv("KITE_ALGO_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("KITE_ALGO_WORKER_TOKEN", "kwa_test")
    monkeypatch.setenv("KITE_ALGO_RUN_ID", "run-1")
    monkeypatch.setenv("KITE_ALGO_SESSION_NONCE", "wsn_1")
    monkeypatch.setenv("KITE_ALGO_TEMPLATE_ID", "hosted:hs_1")
    monkeypatch.setenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:live")
    monkeypatch.setenv("KITE_ALGO_MODE", "live")
    monkeypatch.setenv("KITE_ALGO_PARAMS", '{"lots": 1}')
    monkeypatch.setenv("KITE_ALGO_SCRATCH", str(tmp_path))

    ctx = hosted_bootstrap.build_context()

    assert ctx.execution_mode == "live"
    assert ctx.params == {"lots": 1}
    assert captured["run_id"] == "run-1"
    assert captured["session_nonce"] == "wsn_1"
    assert captured["config"].execution_mode == "live"
    assert captured["config"].account_scope == "kite:live"
