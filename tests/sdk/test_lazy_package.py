"""Guard: importing the SDK package root must stay pandas-free.

Embedding hosts (MCP adapter, CLIs) import ``kite_algo_worker`` for the HTTP
client only; the numerical stack may only load when indicators/marketdata are
actually touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"


def test_package_root_import_does_not_load_numerical_stack() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SDK_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    probe = (
        "import sys\n"
        "import kite_algo_worker\n"
        "heavy = {'pandas', 'numpy', 'numba'} & set(sys.modules)\n"
        "assert not heavy, f'eager numerical imports: {heavy}'\n"
        "assert kite_algo_worker.AlgoWorkerConfig and kite_algo_worker.AsyncKiteAlgoWorkerClient\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, env=env)


def test_lazy_indicator_access_still_works() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SDK_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    probe = (
        "import kite_algo_worker\n"
        "try:\n"
        "    from kite_algo_worker.indicators import TechnicalAnalysis as direct\n"
        "except ModuleNotFoundError:\n"
        "    raise SystemExit(0)  # numerical extra not installed here\n"
        "assert kite_algo_worker.TechnicalAnalysis is direct\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, env=env)
