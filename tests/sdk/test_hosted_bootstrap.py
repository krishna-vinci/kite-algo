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
