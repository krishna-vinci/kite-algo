import sys
from pathlib import Path
from time import perf_counter

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

import kite_algo_worker as kite_algo_worker_pkg  # noqa: E402


def _sample_ohlc(length: int = 256):
    base = [100.0 + (idx * 0.35) + ((idx % 7) * 0.1) for idx in range(length)]
    high = [value + 1.25 for value in base]
    low = [value - 1.15 for value in base]
    volume = [1000.0 + (idx * 15.0) for idx in range(length)]
    return base, high, low, volume


def run_indicator_benchmark_harness(iterations: int = 5, length: int = 256):
    close, high, low, volume = _sample_ohlc(length=length)
    benchmarks = {}

    indicator_calls = {
        "sma": lambda: kite_algo_worker_pkg.ta.sma(close, 20),
        "ema": lambda: kite_algo_worker_pkg.ta.ema(close, 20),
        "rsi": lambda: kite_algo_worker_pkg.ta.rsi(close, 14),
        "atr": lambda: kite_algo_worker_pkg.ta.atr(high, low, close, 14),
        "vwma": lambda: kite_algo_worker_pkg.ta.vwma(close, volume, 20),
    }

    for name, fn in indicator_calls.items():
        started = perf_counter()
        last_output = None
        for _ in range(iterations):
            last_output = fn()
        elapsed = perf_counter() - started
        benchmarks[name] = {
            "iterations": iterations,
            "series_length": length,
            "elapsed_seconds": elapsed,
            "avg_seconds": elapsed / iterations,
            "last_value": _extract_last_numeric(last_output),
        }

    return benchmarks


def _extract_last_numeric(value):
    if value is None:
        return None
    if hasattr(value, "dropna"):
        non_null = value.dropna()
        if getattr(non_null, "empty", False):
            return None
        value = non_null.iloc[-1]
    elif hasattr(value, "iloc"):
        value = value.iloc[-1]
    else:
        value = value[-1]
    if hasattr(value, "item"):
        value = value.item()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@pytest.mark.skipif(not getattr(kite_algo_worker_pkg, "_MARKETDATA_AVAILABLE", False), reason="optional indicator dependencies unavailable")
def test_indicator_benchmark_harness_returns_core_metrics():
    report = run_indicator_benchmark_harness(iterations=2, length=128)

    assert set(report.keys()) == {"sma", "ema", "rsi", "atr", "vwma"}
    for metrics in report.values():
        assert metrics["iterations"] == 2
        assert metrics["series_length"] == 128
        assert metrics["elapsed_seconds"] >= 0.0
        assert metrics["avg_seconds"] >= 0.0
        assert metrics["last_value"] is not None


@pytest.mark.skipif(not getattr(kite_algo_worker_pkg, "_MARKETDATA_AVAILABLE", False), reason="optional indicator dependencies unavailable")
def test_indicator_benchmark_harness_stays_lightweight():
    started = perf_counter()
    report = run_indicator_benchmark_harness(iterations=3, length=192)
    elapsed = perf_counter() - started

    assert report["ema"]["avg_seconds"] < 0.25
    assert report["rsi"]["avg_seconds"] < 0.25
    assert elapsed < 2.0
