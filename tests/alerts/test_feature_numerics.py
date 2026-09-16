"""SDK-parity and hand-computed tests for ``backend.alerts.features``.

The backend cannot import the pandas-based SDK at runtime (separate
deployment), so ``backend.alerts.features`` re-implements the SDK numerics in
pure Python.  These tests pin that agreement:

* SDK parity: both implementations run on identical deterministic inputs
  (``random.Random(42)``, 200 points) and must agree within 1e-9 absolute;
  SDK NaN / pandas-NaN warmup positions must be ``None`` on the backend side.
* Hand-computed small cases pin each formula independently of the SDK.
* Edge cases: warmup ``None`` counts, empty inputs, non-finite/None inputs,
  zero-volume denominators, VWAP session resets and ``volume_ratio`` offsets.
"""

import math
import random
import sys
from pathlib import Path

import pandas as pd
import pytest

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from backend.alerts import features  # noqa: E402
from kite_algo_worker.indicators import (  # noqa: E402
    atr as sdk_atr,
    bbands as sdk_bbands,
    ema as sdk_ema,
    macd as sdk_macd,
    rsi as sdk_rsi,
    sma as sdk_sma,
    supertrend as sdk_supertrend,
    vwap as sdk_vwap,
    wma as sdk_wma,
)

TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _finite_or_none(value):
    number = float(value)
    return number if math.isfinite(number) else None


def _series_to_list(series):
    """pandas Series (with NaN warmup) -> list of float | None."""
    return [_finite_or_none(value) for value in series.tolist()]


def _assert_matches(backend, sdk, label="", tolerance=TOLERANCE):
    """Backend list must match the SDK list within ``tolerance`` absolute.

    SDK NaN / pandas-NaN positions (warmup) must be ``None`` on the backend
    side, and vice versa: the backend never emits a float where the SDK has
    NaN.
    """
    assert len(backend) == len(sdk), f"{label}: length {len(backend)} != {len(sdk)}"
    for idx, (got, raw_expected) in enumerate(zip(backend, sdk)):
        expected = _finite_or_none(raw_expected) if raw_expected is not None else None
        if got is None:
            assert expected is None, f"{label}[{idx}]: backend None but SDK produced {expected}"
        else:
            assert expected is not None, f"{label}[{idx}]: SDK NaN warmup but backend produced {got}"
            assert abs(got - expected) <= tolerance, f"{label}[{idx}]: {got} != {expected}"
        if got is not None and not isinstance(got, int):
            assert math.isfinite(got), f"{label}[{idx}]: backend produced non-finite {got}"


def _sample_inputs(n=200):
    """Deterministic inputs: closes in [50, 150], volumes in [1000, 5000]."""
    rng = random.Random(42)
    closes = [50.0 + 100.0 * rng.random() for _ in range(n)]
    volumes = [1000.0 + 4000.0 * rng.random() for _ in range(n)]
    highs = [close + 1.0 + 2.0 * rng.random() for close in closes]
    lows = [close - 1.0 - 2.0 * rng.random() for close in closes]
    opens = [(high + low) / 2.0 for high, low in zip(highs, lows)]
    bars = [
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
        for open_, high, low, close, volume in zip(opens, highs, lows, closes, volumes)
    ]
    return closes, volumes, bars


# ---------------------------------------------------------------------------
# SDK parity
# ---------------------------------------------------------------------------


def test_parity_sma():
    values, _, _ = _sample_inputs()
    series = pd.Series(values, name="close")
    for period in (12, 60):
        _assert_matches(features.sma(values, period), sdk_sma(series, period).tolist(), label=f"sma({period})")


def test_parity_ema():
    values, _, _ = _sample_inputs()
    series = pd.Series(values, name="close")
    for period in (12, 26):
        _assert_matches(features.ema(values, period), sdk_ema(series, period).tolist(), label=f"ema({period})")


def test_parity_wma():
    values, _, _ = _sample_inputs()
    series = pd.Series(values, name="close")
    for period in (10, 30):
        _assert_matches(
            features.wma(values, period),
            _series_to_list(sdk_wma(series, period)),
            label=f"wma({period})",
        )


def test_parity_rsi():
    values, _, _ = _sample_inputs()
    series = pd.Series(values, name="close")
    for period in (14, 7):
        _assert_matches(features.rsi(values, period=period), sdk_rsi(series, period=period).tolist(), label=f"rsi({period})")


def test_parity_macd():
    values, _, _ = _sample_inputs()
    series = pd.Series(values, name="close")
    for fast, slow, signal in ((12, 26, 9), (5, 10, 3)):
        backend = features.macd(values, fast=fast, slow=slow, signal=signal)
        sdk = sdk_macd(series, fast_period=fast, slow_period=slow, signal_period=signal)
        _assert_matches(backend["macd"], sdk["macd"].tolist(), label=f"macd({fast},{slow},{signal})")
        _assert_matches(backend["signal"], sdk["signal"].tolist(), label=f"signal({fast},{slow},{signal})")
        _assert_matches(backend["hist"], sdk["histogram"].tolist(), label=f"hist({fast},{slow},{signal})")


def test_parity_atr():
    _, _, bars = _sample_inputs()
    frame = pd.DataFrame(
        {
            "high": [bar["high"] for bar in bars],
            "low": [bar["low"] for bar in bars],
            "close": [bar["close"] for bar in bars],
        }
    )
    for period in (14, 5):
        _assert_matches(features.atr(bars, period=period), sdk_atr(frame, period=period).tolist(), label=f"atr({period})")


def test_parity_bollinger():
    values, _, _ = _sample_inputs()
    series = pd.Series(values, name="close")
    backend = features.bollinger(values, period=20, num_std=2.0)
    sdk = sdk_bbands(series, period=20, multiplier=2.0)
    # SDK column names are upper/middle/lower; backend keys are mid/upper/lower.
    _assert_matches(backend["mid"], sdk["middle"].tolist(), label="bollinger.mid")
    _assert_matches(backend["upper"], sdk["upper"].tolist(), label="bollinger.upper")
    _assert_matches(backend["lower"], sdk["lower"].tolist(), label="bollinger.lower")


def test_parity_supertrend():
    _, _, bars = _sample_inputs()
    frame = pd.DataFrame(
        {
            "high": [bar["high"] for bar in bars],
            "low": [bar["low"] for bar in bars],
            "close": [bar["close"] for bar in bars],
        }
    )
    backend = features.supertrend(bars, period=10, multiplier=3.0)
    sdk = sdk_supertrend(frame, period=10, multiplier=3.0)
    _assert_matches(backend["value"], sdk["supertrend"].tolist(), label="supertrend.value")
    sdk_direction = [
        None if not math.isfinite(float(value)) else int(float(value))
        for value in sdk["direction"].tolist()
    ]
    assert backend["direction"] == sdk_direction
    assert all(direction in (1, -1) for direction in backend["direction"] if direction is not None)


def test_parity_vwap_session_single_session_matches_sdk_vwap():
    _, _, bars = _sample_inputs()
    frame = pd.DataFrame(
        {
            "high": [bar["high"] for bar in bars],
            "low": [bar["low"] for bar in bars],
            "close": [bar["close"] for bar in bars],
            "volume": [bar["volume"] for bar in bars],
        }
    )
    backend = features.vwap_session(bars, ["session"] * len(bars))
    # SDK vwap over a frame accumulates the typical price (h+l+c)/3 volume
    # weighted per finite run -- identical to one uninterrupted session.
    _assert_matches(backend, sdk_vwap(frame).tolist(), label="vwap_session")


def test_parity_volume_sma():
    _, volumes, _ = _sample_inputs()
    series = pd.Series(volumes, name="volume")
    for period in (20, 50):
        _assert_matches(
            features.volume_sma(volumes, period),
            sdk_sma(series, period).tolist(),
            label=f"volume_sma({period})",
        )


def test_volume_ratio_anchored_on_sdk_sma():
    """volume_ratio is volumes[i] / sdk_sma(volumes, period)[i - offset]."""
    _, volumes, _ = _sample_inputs()
    base = _series_to_list(sdk_sma(pd.Series(volumes, name="volume"), 20))
    for offset in (0, 3):
        expected = []
        for idx in range(len(volumes)):
            anchor = idx - offset
            if anchor < 0 or base[anchor] is None or base[anchor] <= 0:
                expected.append(None)
            else:
                expected.append(volumes[idx] / base[anchor])
        _assert_matches(
            features.volume_ratio(volumes, period=20, offset=offset),
            expected,
            label=f"volume_ratio(offset={offset})",
        )


# ---------------------------------------------------------------------------
# hand-computed small cases
# ---------------------------------------------------------------------------


def test_sma_hand_computed():
    assert features.sma([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [None, None, 2.0, 3.0, 4.0]
    assert features.sma([1.0, 2.0, 3.0], 1) == [1.0, 2.0, 3.0]


def test_ema_matches_sdk_seed_fixture():
    # SDK fixture: seed = SMA of first `period`, then recursive EMA.
    assert features.ema([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [None, None, 2.0, 3.0, 4.0]
    # alpha = 1.0 when period == 1, so EMA tracks the input exactly.
    assert features.ema([1.0, 2.0, 3.0], 1) == [1.0, 2.0, 3.0]


def test_wma_hand_computed():
    assert features.wma([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [
        None,
        None,
        14.0 / 6.0,
        20.0 / 6.0,
        26.0 / 6.0,
    ]


def test_rsi_hand_computed():
    # deltas [2, -1, 3, -1]: seeds avg_gain=1.0/avg_loss=0.5, then Wilder.
    result = features.rsi([1.0, 3.0, 2.0, 5.0, 4.0], period=2)
    assert result[0] is None and result[1] is None
    assert result[2] == pytest.approx(200.0 / 3.0)  # rs = 1.0 / 0.5
    assert result[3] == pytest.approx(800.0 / 9.0)  # rs = 2.0 / 0.25
    assert result[4] == pytest.approx(800.0 / 13.0)  # rs = 1.0 / 0.625
    # Pure up-move: zero average loss with gains -> 100.
    assert features.rsi([1.0, 2.0, 3.0, 4.0, 5.0], period=2) == [None, None, 100.0, 100.0, 100.0]
    # Flat series: zero gain and zero loss -> 50 (SDK convention).
    assert features.rsi([5.0, 5.0, 5.0, 5.0, 5.0], period=2) == [None, None, 50.0, 50.0, 50.0]


def test_macd_hand_computed():
    result = features.macd([1.0, 2.0, 3.0, 4.0, 5.0], fast=2, slow=3, signal=2)
    assert result["macd"] == [None, None, 0.5, 0.5, 0.5]
    # Signal EMA restarts on the macd line (first finite at index slow-1 = 2),
    # so its seed lands at slow + signal - 2 = 3.
    assert result["signal"] == [None, None, None, 0.5, 0.5]
    assert result["hist"] == [None, None, None, 0.0, 0.0]


def test_atr_hand_computed():
    bars = [
        {"high": 2.0, "low": 1.0, "close": 1.5},
        {"high": 3.0, "low": 2.0, "close": 2.5},
    ]
    # tr = [1.0, max(1, |3-1.5|, |1.5-2|) = 1.5]; seed = mean = 1.25.
    assert features.atr(bars, period=2) == [None, pytest.approx(1.25)]


def test_bollinger_hand_computed():
    result = features.bollinger([1.0, 2.0, 3.0, 4.0, 5.0], period=3, num_std=2.0)
    spread = 2.0 * math.sqrt(2.0 / 3.0)  # population stddev of any 3-run here
    assert result["mid"] == [None, None, 2.0, 3.0, 4.0]
    assert result["upper"][2] == pytest.approx(2.0 + spread)
    assert result["lower"][2] == pytest.approx(2.0 - spread)
    assert result["upper"][4] == pytest.approx(4.0 + spread)
    assert result["lower"][4] == pytest.approx(4.0 - spread)


def test_supertrend_hand_computed_trending_up():
    # Same fixture as the SDK trend test: steady uptrend, bands never flip.
    bars = [
        {"high": 11.0, "low": 9.0, "close": 10.0},
        {"high": 12.0, "low": 10.0, "close": 11.0},
        {"high": 13.0, "low": 11.0, "close": 12.0},
        {"high": 14.0, "low": 12.0, "close": 13.0},
        {"high": 15.0, "low": 13.0, "close": 14.0},
    ]
    result = features.supertrend(bars, period=3, multiplier=2.0)
    assert result["value"] == [None, None, 8.0, 9.0, 10.0]
    assert result["direction"] == [None, None, 1, 1, 1]


def test_supertrend_hand_computed_flip_to_bearish():
    # period=2, multiplier=1: atr = [?, 2, 2, 7]; lower = [?, 9, 10, -6.5];
    # upper = [?, 13, 14, 7.5]. Close 0.5 breaks below the previous final
    # lower band (10) at index 3 -> direction flips, value = final upper 7.5.
    bars = [
        {"high": 11.0, "low": 9.0, "close": 10.0},
        {"high": 12.0, "low": 10.0, "close": 11.0},
        {"high": 13.0, "low": 11.0, "close": 12.0},
        {"high": 1.0, "low": 0.0, "close": 0.5},
    ]
    result = features.supertrend(bars, period=2, multiplier=1.0)
    assert result["value"] == [None, 9.0, 10.0, 7.5]
    assert result["direction"] == [None, 1, 1, -1]


def test_supertrend_carries_state_across_nan_close():
    # Mirrors the SDK test: tr never reads the *current* close, so ATR keeps
    # running; the NaN close carries bands/direction/value forward.
    bars = [
        {"high": 11.0, "low": 9.0, "close": 10.0},
        {"high": 12.0, "low": 10.0, "close": 11.0},
        {"high": 13.0, "low": 11.0, "close": 12.0},
        {"high": 14.0, "low": 12.0, "close": None},
        {"high": 15.0, "low": 13.0, "close": 14.0},
    ]
    result = features.supertrend(bars, period=3, multiplier=2.0)
    assert result["direction"][2] == 1
    assert result["direction"][3] == 1
    assert result["value"][3] == result["value"][2] == 8.0
    # tr[i + 1] reads close[i], so the NaN close also breaks the true range
    # run one bar later: ATR/bands/value go unknown at index 4 (SDK parity).
    assert result["value"][4] is None
    assert result["direction"][4] == 1


def test_vwap_session_resets_between_sessions():
    bars = [
        # session "a": tp=(3+1+2)/3=2.0 v=1 -> 2.0; tp=3.0 v=3 -> 11/4 = 2.75
        {"high": 3.0, "low": 1.0, "close": 2.0, "volume": 1.0},
        {"high": 4.0, "low": 2.0, "close": 3.0, "volume": 3.0},
        # session "b": cumulative state resets; tp=8.0 v=2 -> 8.0;
        # tp=5.0 v=2 -> (16 + 10) / 4 = 6.5
        {"high": 10.0, "low": 6.0, "close": 8.0, "volume": 2.0},
        {"high": 6.0, "low": 4.0, "close": 5.0, "volume": 2.0},
    ]
    assert features.vwap_session(bars, ["a", "a", "b", "b"]) == [2.0, 2.75, 8.0, 6.5]


def test_vwap_session_rejects_mismatched_lengths():
    bars = [{"high": 3.0, "low": 1.0, "close": 2.0, "volume": 1.0}]
    with pytest.raises(ValueError):
        features.vwap_session(bars, ["a", "b"])


def test_volume_ratio_offset_one_uses_previous_bar_sma():
    # sma(volumes, 2) = [None, 3, 5, 7]; offset=1 anchors on the prior bar.
    assert features.volume_ratio([2.0, 4.0, 6.0, 8.0], period=2, offset=1) == [
        None,
        None,
        2.0,
        1.6,
    ]


def test_volume_ratio_zero_denominator_yields_none():
    # sma([0, 0, 0, 5], 2) = [None, 0, 0, 2.5]; denominators <= 0 -> None.
    assert features.volume_ratio([0.0, 0.0, 0.0, 5.0], period=2, offset=0) == [
        None,
        None,
        None,
        2.0,
    ]


# ---------------------------------------------------------------------------
# validation and edge cases
# ---------------------------------------------------------------------------


def test_period_validation_bounds():
    values = [1.0, 2.0, 3.0]
    for period in (0, -3, 501):
        with pytest.raises(ValueError):
            features.sma(values, period)
        with pytest.raises(ValueError):
            features.ema(values, period)
        with pytest.raises(ValueError):
            features.wma(values, period)
        with pytest.raises(ValueError):
            features.rsi(values, period=period)
        with pytest.raises(ValueError):
            features.volume_sma(values, period)
        with pytest.raises(ValueError):
            features.volume_ratio(values, period)
    # Boundary periods are valid.
    assert features.sma(values, 1) == values
    assert features.sma(values, 500) == [None, None, None]
    with pytest.raises(ValueError):
        features.macd(values, fast=0)
    with pytest.raises(ValueError):
        features.macd(values, slow=501)
    with pytest.raises(ValueError):
        features.macd(values, signal=-1)
    with pytest.raises(ValueError):
        features.atr([], period=0)
    with pytest.raises(ValueError):
        features.bollinger(values, period=501)
    for num_std in (0, -2.0, float("inf")):
        with pytest.raises(ValueError):
            features.bollinger(values, num_std=num_std)
    for multiplier in (0, -2.0, float("inf")):
        with pytest.raises(ValueError):
            features.supertrend([], period=2, multiplier=multiplier)
    with pytest.raises(ValueError):
        features.volume_ratio(values, 2, offset=-1)


def test_empty_inputs_return_empty():
    assert features.sma([], 5) == []
    assert features.ema([], 5) == []
    assert features.wma([], 5) == []
    assert features.rsi([], period=14) == []
    assert features.macd([]) == {"macd": [], "signal": [], "hist": []}
    assert features.atr([], period=14) == []
    assert features.bollinger([]) == {"mid": [], "upper": [], "lower": []}
    assert features.supertrend([]) == {"value": [], "direction": []}
    assert features.vwap_session([], []) == []
    assert features.volume_sma([], 5) == []
    assert features.volume_ratio([], 5) == []


def test_warmup_none_counts_and_length_alignment():
    n, period = 10, 4
    values = [float(i + 1) for i in range(n)]
    bars = [
        {"open": v, "high": v + 1.0, "low": v - 1.0, "close": v, "volume": 100.0}
        for v in values
    ]

    # SMA/EMA/WMA/ATR/Bollinger/Supertrend first emit at index period - 1.
    for result in (
        features.sma(values, period),
        features.ema(values, period),
        features.wma(values, period),
        features.atr(bars, period=period),
        features.bollinger(values, period=period)["mid"],
        features.supertrend(bars, period=period)["value"],
        features.volume_sma(values, period),
    ):
        assert len(result) == n
        assert result[: period - 1] == [None] * (period - 1)
        assert result[period - 1] is not None

    # RSI needs period + 1 points (delta over the seed window).
    rsi_result = features.rsi(values, period=period)
    assert len(rsi_result) == n
    assert rsi_result[:period] == [None] * period
    assert rsi_result[period] is not None

    bollinger = features.bollinger(values, period=period)
    assert all(len(bollinger[key]) == n for key in ("mid", "upper", "lower"))
    assert bollinger["upper"][period - 1] is not None
    supertrend = features.supertrend(bars, period=period)
    assert len(supertrend["direction"]) == n
    assert supertrend["direction"][period - 1] == 1

    # MACD: line starts at slow - 1; signal seed at slow + signal - 2.
    macd = features.macd(values, fast=2, slow=4, signal=2)
    assert len(macd["macd"]) == n and macd["macd"][:3] == [None] * 3
    assert len(macd["signal"]) == n and macd["signal"][:4] == [None] * 4
    assert len(macd["hist"]) == n and macd["hist"][:4] == [None] * 4
    assert macd["signal"][4] is not None


def test_non_finite_and_none_inputs_yield_none_without_raising():
    values = [1.0, None, float("inf"), float("nan"), 2.0]
    for result in (
        features.sma(values, 2),
        features.ema(values, 2),
        features.wma(values, 2),
        features.rsi(values, period=2),
    ):
        assert result == [None] * 5
    macd = features.macd(values, fast=2, slow=3, signal=2)
    assert macd["macd"] == [None] * 5
    assert macd["signal"] == [None] * 5
    assert macd["hist"] == [None] * 5
    bollinger = features.bollinger(values, period=2)
    assert bollinger["mid"] == [None] * 5
    assert bollinger["upper"] == [None] * 5
    assert bollinger["lower"] == [None] * 5

    # ATR: tr[3] is None because close[2] is missing, breaking the Wilder
    # segment; the trailing segment is too short to seed again.
    bars = [
        {"high": 2.0, "low": 1.0, "close": 1.5},
        {"high": 3.0, "low": 2.0, "close": 2.5},
        {"high": 4.0, "low": 3.0, "close": None},
        {"high": 5.0, "low": 4.0, "close": 4.5},
        {"high": 6.0, "low": 5.0, "close": 5.5},
    ]
    atr = features.atr(bars, period=2)
    assert atr == [None, pytest.approx(1.25), pytest.approx(1.375), None, None]

    # VWAP: the bad bar is None and the cumulative state restarts after it.
    session_bars = [
        {"high": 3.0, "low": 1.0, "close": 2.0, "volume": 1.0},
        {"high": 4.0, "low": 2.0, "close": 3.0, "volume": 3.0},
        {"high": float("nan"), "low": 6.0, "close": 8.0, "volume": 2.0},
        {"high": 6.0, "low": 4.0, "close": 5.0, "volume": 2.0},
    ]
    assert features.vwap_session(session_bars, ["s"] * 4) == [2.0, 2.75, None, 5.0]

    # volume_ratio: non-finite numerator -> None.
    assert features.volume_ratio([2.0, 4.0, float("nan"), 8.0], period=2, offset=0) == [
        None,
        pytest.approx(4.0 / 3.0),
        None,
        None,
    ]


def test_supertrend_never_raises_on_malformed_bars():
    bars = [
        {"high": 11.0, "low": 9.0, "close": 10.0},
        {"high": 12.0, "low": 10.0},  # missing close
        "not-a-bar",
        {"high": 14.0, "low": 12.0, "close": 13.0, "volume": 5.0},
    ]
    result = features.supertrend(bars, period=2, multiplier=1.0)
    assert len(result["value"]) == len(result["direction"]) == 4
