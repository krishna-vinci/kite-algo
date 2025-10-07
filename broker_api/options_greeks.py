# broker_api/options_greeks.py

import mibian
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union
import numpy as np
from numba import njit
from math import exp, log, pi, sqrt, erf

# --- New Vectorized Engine Configuration ---
OPTIONS_ENGINE_USE_VECTORIZED = True

# --- Numba-compatible CDF and PDF ---
@njit(fastmath=True, nogil=True)
def _norm_cdf(x: float) -> float:
    """Numba-compatible CDF of the standard normal distribution."""
    return (1.0 + erf(x / sqrt(2.0))) / 2.0

@njit(fastmath=True, nogil=True)
def _norm_pdf(x: float) -> float:
    """Numba-compatible PDF of the standard normal distribution."""
    return exp(-0.5 * x**2) / sqrt(2.0 * pi)

# --- Scalar-only, Numba-jittable helpers ---
@njit(nogil=True, fastmath=True)
def _norm_cdf_scalar(x: float) -> float:
    """Scalar-only CDF of the standard normal distribution."""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))

@njit(nogil=True, fastmath=True)
def _norm_pdf_scalar(x: float) -> float:
    """Scalar-only PDF of the standard normal distribution."""
    return exp(-0.5 * x**2) / sqrt(2.0 * pi)

# --- Vectorized and JIT-compiled Black-76 Kernels ---

@njit(nogil=True, fastmath=True)
def _black76_price_scalar_kernel(is_call: bool, F: float, K: float, T: float, sigma: float) -> float:
    """High-performance, scalar Black-76 pricing kernel."""
    if T <= 1e-12 or sigma <= 1e-12:
        if is_call:
            return max(0.0, F - K)
        else:
            return max(0.0, K - F)

    d1 = (log(F / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)

    if is_call:
        price = F * _norm_cdf_scalar(d1) - K * _norm_cdf_scalar(d2)
    else:
        price = K * _norm_cdf_scalar(-d2) - F * _norm_cdf_scalar(-d1)
    return price

@njit(fastmath=True, nogil=True)
def _black76_price_kernel(is_call: bool, F: float, K: np.ndarray, T: float, sigma: float) -> np.ndarray:
    """High-performance, array-capable Black-76 pricing kernel."""
    n = K.shape[0]
    out = np.empty(n, dtype=np.float64)

    for i in range(n):
        k_val = K[i]
        if T <= 1e-12 or sigma <= 1e-12:
            if is_call:
                out[i] = max(0.0, F - k_val)
            else:
                out[i] = max(0.0, k_val - F)
            continue

        d1 = (log(F / k_val) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
        d2 = d1 - sigma * sqrt(T)

        if is_call:
            price = F * _norm_cdf_scalar(d1) - k_val * _norm_cdf_scalar(d2)
        else:
            price = k_val * _norm_cdf_scalar(-d2) - F * _norm_cdf_scalar(-d1)
        out[i] = price
    return out

@njit(fastmath=True, nogil=True)
def _black76_greeks_kernel(is_call: bool, F: float, K: np.ndarray, T: float, sigma: float):
    """High-performance, array-capable Black-76 Greeks kernel."""
    n = K.shape[0]
    delta = np.empty(n, dtype=np.float64)
    gamma = np.empty(n, dtype=np.float64)
    theta = np.empty(n, dtype=np.float64)
    vega = np.empty(n, dtype=np.float64)

    for i in range(n):
        k_val = K[i]
        if T <= 1e-12 or sigma <= 1e-12:
            if is_call:
                if F > k_val:
                    delta[i] = 1.0
                elif F == k_val:
                    delta[i] = 0.5
                else:
                    delta[i] = 0.0
            else:
                if F < k_val:
                    delta[i] = -1.0
                elif F == k_val:
                    delta[i] = -0.5
                else:
                    delta[i] = 0.0
            gamma[i] = 0.0
            theta[i] = 0.0
            vega[i] = 0.0
            continue

        d1 = (log(F / k_val) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
        d2 = d1 - sigma * sqrt(T)
        
        pdf_d1 = _norm_pdf_scalar(d1)
        
        gamma[i] = pdf_d1 / (F * sigma * sqrt(T))
        vega[i] = F * pdf_d1 * sqrt(T)
        theta_term1 = -(F * pdf_d1 * sigma) / (2 * sqrt(T))
        
        if is_call:
            delta[i] = _norm_cdf_scalar(d1)
            theta[i] = theta_term1
        else:
            delta[i] = _norm_cdf_scalar(d1) - 1.0
            theta[i] = theta_term1
            
    return delta, gamma, theta, vega

@njit(fastmath=True, nogil=True)
def _implied_vol_kernel(is_call: bool, F: float, K: float, T: float, price: float, max_iter: int = 50, tol: float = 1e-6) -> float:
    """
    JIT-compatible implied volatility solver (Newton-Raphson with bisection fallback).
    This kernel operates on a single value; the wrapper handles vectorization.
    """
    if T <= 1e-12 or F <= 0 or K <= 0 or price <= 0:
        return np.nan

    intrinsic = max(0.0, F - K if is_call else K - F)
    if price < intrinsic - tol:
        return np.nan

    sigma = 0.2  # Initial guess
    sigma_min, sigma_max = 1e-4, 4.0

    for _ in range(max_iter):
        price_model = _black76_price_scalar_kernel(is_call, F, K, T, sigma)
        
        if abs(price_model - price) < tol:
            return sigma

        d1 = (log(F / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
        vega = F * _norm_pdf_scalar(d1) * sqrt(T)

        if vega < 1e-12:
            if price_model < price:
                sigma_min = sigma
            else:
                sigma_max = sigma
            sigma = (sigma_min + sigma_max) / 2.0
            continue

        sigma_new = sigma - (price_model - price) / vega

        if not (sigma_min < sigma_new < sigma_max):
            if price_model < price:
                sigma_min = sigma
            else:
                sigma_max = sigma
            sigma = (sigma_min + sigma_max) / 2.0
        else:
            sigma = sigma_new
            
    final_price = _black76_price_scalar_kernel(is_call, F, K, T, sigma)
    return sigma if abs(final_price - price) < tol else np.nan

# --- Legacy Scalar Implementations (for fallback) ---

def _black76_price_scalar(option_type: str, F: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0, F - K) if option_type.upper() == 'CE' else max(0, K - F)
    d1 = (log(F / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    if option_type.upper() == 'CE':
        return F * _norm_cdf(d1) - K * _norm_cdf(d2)
    else:
        return K * _norm_cdf(-d2) - F * _norm_cdf(-d1)

def _black76_greeks_scalar(option_type: str, F: float, K: float, T: float, sigma: float) -> Dict[str, float]:
    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    if T <= 0 or sigma <= 0:
        return greeks
    d1 = (log(F / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    pdf_d1 = _norm_pdf(d1)
    greeks["gamma"] = pdf_d1 / (F * sigma * sqrt(T))
    greeks["vega"] = F * pdf_d1 * sqrt(T)
    theta_term1 = -(F * pdf_d1 * sigma) / (2 * sqrt(T))
    if option_type.upper() == 'CE':
        greeks["delta"] = _norm_cdf(d1)
        greeks["theta"] = theta_term1
    else:
        greeks["delta"] = _norm_cdf(d1) - 1
        greeks["theta"] = theta_term1
    return greeks

def _implied_vol_from_price_black76_scalar(
    option_type: str, F: float, K: float, T: float, price: float,
    sigma_lo: float = 0.01, sigma_hi: float = 3.0,
    tol: float = 1e-6, max_iter: int = 50
) -> Optional[float]:
    if T <= 0 or F <= 0 or K <= 0 or price <= 0:
        return None
    for _ in range(max_iter):
        sigma_mid = (sigma_lo + sigma_hi) / 2
        if sigma_mid < tol: return sigma_lo
        price_mid = _black76_price_scalar(option_type, F, K, T, sigma_mid)
        if abs(price_mid - price) < tol: return sigma_mid
        if price_mid < price: sigma_lo = sigma_mid
        else: sigma_hi = sigma_mid
    return (sigma_lo + sigma_hi) / 2

# --- Public API Functions (Wrappers) ---

def black76_price(option_type: str, F: float, K: Union[float, np.ndarray], T: float, sigma: float) -> Union[float, np.ndarray]:
    """
    Calculates the price of a European option using the Black-76 model.
    Supports scalar or array inputs for strike price (K).
    Switches between vectorized and scalar engines via OPTIONS_ENGINE_USE_VECTORIZED flag.
    """
    is_scalar = not isinstance(K, np.ndarray)
    if not OPTIONS_ENGINE_USE_VECTORIZED:
        if is_scalar:
            return _black76_price_scalar(option_type, F, K, T, sigma)
        else:
            return np.array([_black76_price_scalar(option_type, F, k_val, T, sigma) for k_val in K])

    is_call = option_type.upper() == 'CE'
    k_arr = np.atleast_1d(K)
    prices = _black76_price_kernel(is_call, F, k_arr, T, sigma)
    return prices.item() if is_scalar else prices

def black76_greeks(option_type: str, F: float, K: Union[float, np.ndarray], T: float, sigma: float) -> Union[Dict[str, float], Dict[str, np.ndarray]]:
    """
    Calculates the Greeks for a European option using the Black-76 model.
    Supports scalar or array inputs for strike price (K).
    """
    is_scalar = not isinstance(K, np.ndarray)
    if not OPTIONS_ENGINE_USE_VECTORIZED:
        if is_scalar:
            return _black76_greeks_scalar(option_type, F, K, T, sigma)
        else:
            results = [_black76_greeks_scalar(option_type, F, k_val, T, sigma) for k_val in K]
            return {greek: np.array([r[greek] for r in results]) for greek in results[0]}

    is_call = option_type.upper() == 'CE'
    k_arr = np.atleast_1d(K)
    delta, gamma, theta, vega = _black76_greeks_kernel(is_call, F, k_arr, T, sigma)
    greeks = {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": 0.0}
    
    if is_scalar:
        return {k: v.item() if hasattr(v, 'item') else v for k, v in greeks.items()}
    return greeks

def implied_vol_from_price_black76(
    option_type: str, F: float, K: Union[float, np.ndarray], T: float, price: Union[float, np.ndarray]
) -> Union[Optional[float], np.ndarray]:
    """
    Calculates the implied volatility using the Black-76 model.
    Supports scalar or array inputs.
    """
    is_scalar = not isinstance(K, np.ndarray)
    if not OPTIONS_ENGINE_USE_VECTORIZED:
        if is_scalar:
            return _implied_vol_from_price_black76_scalar(option_type, F, K, T, price)
        else:
            return np.array([_implied_vol_from_price_black76_scalar(option_type, F, k_val, T, p_val) for k_val, p_val in zip(K, price)])

    is_call = option_type.upper() == 'CE'
    k_arr = np.atleast_1d(K)
    price_arr = np.atleast_1d(price)

    ivs = np.array([_implied_vol_kernel(is_call, F, k_val, T, p_val) for k_val, p_val in zip(k_arr, price_arr)])
    
    if is_scalar:
        result = ivs.item()
        return result if result is not None and not np.isnan(result) else None
    return ivs

def prewarm_options_engine():
    """
    Executes a minimal call to compile Numba kernels to reduce first-call latency.
    """
    if OPTIONS_ENGINE_USE_VECTORIZED:
        try:
            _black76_price_kernel(True, 100.0, np.array([100.0]), 0.1, 0.2)
            _black76_greeks_kernel(True, 100.0, np.array([100.0]), 0.1, 0.2)
            _implied_vol_kernel(True, 100.0, 100.0, 0.1, 5.0)
            print("Numba options kernels pre-warmed successfully.")
        except Exception as e:
            print(f"Numba kernel pre-warming failed: {e}")

# --- Original Mibian Implementation (Legacy) ---

def days_to_expiry(expiry_date_str: str, today_str: Optional[str] = None) -> int:
    expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d').date()
    today = datetime.strptime(today_str, '%Y-%m-%d').date() if today_str else datetime.now(timezone.utc).date()
    return max(0, (expiry_date - today).days)

class OptionGreeksCalculator:
    def __init__(self, risk_free_rate: float = 0.0):
        self.risk_free_rate = risk_free_rate

    def calculate_greeks(
        self, option_type: str, underlying_price: float, strike_price: float,
        expiry_date_str: str, option_ltp: float, today_str: Optional[str] = None
    ) -> Dict[str, Any]:
        days = days_to_expiry(expiry_date_str, today_str)
        if days == 0:
            return {"implied_volatility": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "model_price": option_ltp}

        mibian_risk_free_rate = self.risk_free_rate * 100
        bs_inputs = [underlying_price, strike_price, mibian_risk_free_rate, days]

        try:
            if option_type.upper() == "CE":
                bs = mibian.BS(bs_inputs, callPrice=option_ltp)
                iv = bs.impliedVolatility
                greeks = mibian.BS(bs_inputs, volatility=iv)
                return {"implied_volatility": iv, "delta": greeks.callDelta, "gamma": greeks.gamma, "theta": greeks.callTheta, "vega": greeks.vega, "model_price": greeks.callPrice}
            elif option_type.upper() == "PE":
                bs = mibian.BS(bs_inputs, putPrice=option_ltp)
                iv = bs.impliedVolatility
                greeks = mibian.BS(bs_inputs, volatility=iv)
                return {"implied_volatility": iv, "delta": greeks.putDelta, "gamma": greeks.gamma, "theta": greeks.putTheta, "vega": greeks.vega, "model_price": greeks.putPrice}
            else:
                raise ValueError("option_type must be 'CE' or 'PE'")
        except Exception as e:
            print(f"Error calculating Mibian Greeks for {option_type} {strike_price}: {e}")
            return {"implied_volatility": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "model_price": 0.0}

def ewma(prev: Optional[float], new: float, alpha: float = 0.2) -> float:
    if prev is None:
        return new
    return alpha * new + (1 - alpha) * prev

# --- Example Usage and Sanity Check ---
if __name__ == "__main__":
    # Pre-warm the JIT kernels
    prewarm_options_engine()

    # --- Sanity Check: Vectorized vs. Scalar ---
    print("\n--- Sanity Check: Vectorized vs. Scalar ---")
    F_test, T_test, sigma_test = 100.0, 0.25, 0.3
    K_test_scalar = 105.0
    K_test_array = np.array([95.0, 100.0, 105.0, 110.0])
    
    # Price Comparison
    price_scalar_legacy = _black76_price_scalar('CE', F_test, K_test_scalar, T_test, sigma_test)
    price_scalar_new = black76_price('CE', F_test, K_test_scalar, T_test, sigma_test)
    price_array_new = black76_price('CE', F_test, K_test_array, T_test, sigma_test)
    print(f"Price (Scalar Legacy): {price_scalar_legacy:.4f}")
    print(f"Price (Scalar New):    {price_scalar_new:.4f}")
    print(f"Price (Array New):     {[f'{p:.4f}' for p in price_array_new]}")

    # Greeks Comparison
    greeks_scalar_legacy = _black76_greeks_scalar('CE', F_test, K_test_scalar, T_test, sigma_test)
    greeks_scalar_new = black76_greeks('CE', F_test, K_test_scalar, T_test, sigma_test)
    greeks_array_new = black76_greeks('CE', F_test, K_test_array, T_test, sigma_test)
    print(f"\nDelta (Scalar Legacy): {greeks_scalar_legacy['delta']:.4f}")
    print(f"Delta (Scalar New):    {greeks_scalar_new['delta']:.4f}")
    print(f"Delta (Array New):     {[f'{d:.4f}' for d in greeks_array_new['delta']]}")

    # Implied Volatility Comparison
    test_price_scalar = 6.0
    test_price_array = np.array([10.0, 6.0, 3.5])
    K_iv_test = np.array([95.0, 100.0, 105.0])

    iv_scalar_legacy = _implied_vol_from_price_black76_scalar('CE', F_test, 100.0, T_test, test_price_scalar)
    iv_scalar_new = implied_vol_from_price_black76('CE', F_test, 100.0, T_test, test_price_scalar)
    iv_array_new = implied_vol_from_price_black76('CE', F_test, K_iv_test, T_test, test_price_array)
    print(f"\nIV (Scalar Legacy): {iv_scalar_legacy:.4f}")
    print(f"IV (Scalar New):    {iv_scalar_new:.4f}")
    print(f"IV (Array New):     {[f'{iv:.4f}' for iv in iv_array_new]}")

    # --- Internal Sanity Check for Scalar Kernels ---
    print("\n--- Internal Sanity Check: Scalar Kernels ---")
    price_scalar_kernel_val = _black76_price_scalar_kernel(True, F_test, K_test_scalar, T_test, sigma_test)
    print(f"Price (Scalar Kernel): {price_scalar_kernel_val:.4f} vs (Scalar New): {price_scalar_new:.4f}")
    assert np.isclose(price_scalar_kernel_val, price_scalar_new), "Scalar kernel price mismatch"
    print("Scalar kernel sanity check passed.")
