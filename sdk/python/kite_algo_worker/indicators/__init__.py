from __future__ import annotations

from typing import Any

try:
    from .base import BaseIndicator as _BaseIndicator, IndicatorInput as _IndicatorInput, format_output as _format_output, normalize_input as _normalize_input
    from .hybrid import TechnicalAnalysis as _HybridTechnicalAnalysis, adx as _adx, aroon as _aroon, sar as _sar
    from .live import IndicatorValue as _IndicatorValue, LiveIndicatorEngine as _LiveIndicatorEngine
    from .momentum import TechnicalAnalysis as _MomentumTechnicalAnalysis, cci as _cci, macd as _macd, rsi as _rsi, stochastic as _stochastic, williams_r as _williams_r, williamsr as _williamsr
    from .numba_compat import NUMBA_AVAILABLE as _NUMBA_AVAILABLE, njit as _njit
    from .oscillators import TechnicalAnalysis as _OscillatorTechnicalAnalysis, dpo as _dpo, ppo as _ppo
    from .statistics import TechnicalAnalysis as _StatisticsTechnicalAnalysis, linreg as _linreg
    from .trend import ema as _ema, supertrend as _supertrend, vwma as _vwma, wma as _wma
    from .volatility import TechnicalAnalysis as _VolatilityTechnicalAnalysis, atr as _atr, bbands as _bbands, keltner as _keltner
    from .volume import TechnicalAnalysis as _VolumeTechnicalAnalysis, mfi as _mfi, obv as _obv, vwap as _vwap

    BaseIndicator = _BaseIndicator  # type: ignore[assignment]
    IndicatorInput = _IndicatorInput  # type: ignore[assignment]
    IndicatorValue = _IndicatorValue  # type: ignore[assignment]
    LiveIndicatorEngine = _LiveIndicatorEngine  # type: ignore[assignment]
    format_output = _format_output  # type: ignore[assignment]
    normalize_input = _normalize_input  # type: ignore[assignment]
    NUMBA_AVAILABLE = _NUMBA_AVAILABLE
    njit = _njit

    class TechnicalAnalysis(_HybridTechnicalAnalysis, _StatisticsTechnicalAnalysis, _OscillatorTechnicalAnalysis, _MomentumTechnicalAnalysis, _VolatilityTechnicalAnalysis, _VolumeTechnicalAnalysis):  # type: ignore[misc]
        pass

    rsi = _rsi  # type: ignore[assignment]
    macd = _macd  # type: ignore[assignment]
    ppo = _ppo  # type: ignore[assignment]
    dpo = _dpo  # type: ignore[assignment]
    stochastic = _stochastic  # type: ignore[assignment]
    cci = _cci  # type: ignore[assignment]
    williamsr = _williamsr  # type: ignore[assignment]
    williams_r = _williams_r  # type: ignore[assignment]
    linreg = _linreg  # type: ignore[assignment]
    ema = _ema  # type: ignore[assignment]
    wma = _wma  # type: ignore[assignment]
    vwma = _vwma  # type: ignore[assignment]
    supertrend = _supertrend  # type: ignore[assignment]
    atr = _atr  # type: ignore[assignment]
    bbands = _bbands  # type: ignore[assignment]
    keltner = _keltner  # type: ignore[assignment]
    adx = _adx  # type: ignore[assignment]
    aroon = _aroon  # type: ignore[assignment]
    sar = _sar  # type: ignore[assignment]
    obv = _obv  # type: ignore[assignment]
    vwap = _vwap  # type: ignore[assignment]
    mfi = _mfi  # type: ignore[assignment]
except ModuleNotFoundError as exc:  # pragma: no cover - import-time fallback
    if exc.name not in {"numpy", "pandas", "numba"}:
        raise

    NUMBA_AVAILABLE = False

    def njit(*args: Any, **kwargs: Any):
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]

        def decorator(func):
            return func

        return decorator

    class IndicatorInput:  # type: ignore[no-redef]
        pass

    class BaseIndicator:  # type: ignore[no-redef]
        @staticmethod
        def validate_input(*_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        @staticmethod
        def format_output(*_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    class TechnicalAnalysis(BaseIndicator):  # type: ignore[no-redef]
        def sma(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    class IndicatorValue:  # type: ignore[no-redef]
        pass

    class LiveIndicatorEngine:  # type: ignore[no-redef]
        @classmethod
        def from_history(cls, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def ema(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def wma(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def vwma(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def supertrend(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def crossover(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def crossunder(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def highest(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def lowest(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def rising(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def falling(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def rsi(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def macd(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def ppo(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def dpo(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def stochastic(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def cci(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def williamsr(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def linreg(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def atr(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def bbands(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def keltner(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def adx(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def aroon(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def sar(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def obv(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def vwap(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

        def mfi(self, *_args: Any, **_kwargs: Any):
            raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def normalize_input(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def format_output(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def ema(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def wma(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def vwma(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def supertrend(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def rsi(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def macd(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def ppo(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def dpo(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def stochastic(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def cci(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def williamsr(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def williams_r(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def linreg(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def atr(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def bbands(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def keltner(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def adx(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def aroon(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def sar(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def obv(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def vwap(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

    def mfi(*_args: Any, **_kwargs: Any):
        raise ModuleNotFoundError("pandas and numpy are required for kite_algo_worker indicators")

ta: Any = TechnicalAnalysis()
sma = ta.sma
ema = ta.ema
wma = ta.wma
vwma = ta.vwma
supertrend = ta.supertrend
crossover = ta.crossover
crossunder = ta.crossunder
highest = ta.highest
lowest = ta.lowest
rising = ta.rising
falling = ta.falling
rsi = ta.rsi
macd = ta.macd
ppo = ta.ppo
dpo = ta.dpo
stochastic = ta.stochastic
cci = ta.cci
williamsr = ta.williamsr
williams_r = ta.williamsr
linreg = ta.linreg
atr = ta.atr
bbands = ta.bbands
keltner = ta.keltner
adx = ta.adx
aroon = ta.aroon
sar = ta.sar
obv = ta.obv
vwap = ta.vwap
mfi = ta.mfi

_INDICATOR_EXPORTS = [
    "BaseIndicator",
    "IndicatorInput",
    "IndicatorValue",
    "LiveIndicatorEngine",
    "NUMBA_AVAILABLE",
    "TechnicalAnalysis",
    "atr",
    "bbands",
    "cci",
    "dpo",
    "ema",
    "crossover",
    "crossunder",
    "falling",
    "format_output",
    "highest",
    "keltner",
    "linreg",
    "lowest",
    "mfi",
    "macd",
    "njit",
    "normalize_input",
    "obv",
    "ppo",
    "rising",
    "rsi",
    "adx",
    "aroon",
    "sar",
    "sma",
    "stochastic",
    "supertrend",
    "ta",
    "vwap",
    "williams_r",
    "williamsr",
    "vwma",
    "wma",
]

__all__ = _INDICATOR_EXPORTS
