# broker_api/options_greeks.py

import mibian
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# Helper function from the notebook
def days_to_expiry(expiry_date_str: str, today_str: Optional[str] = None) -> int:
    """
    Calculates the number of calendar days from today to an expiry date.
    Mibian expects the time to expiry in calendar days.
    """
    expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d').date()
    if today_str:
        today = datetime.strptime(today_str, '%Y-%m-%d').date()
    else:
        today = datetime.now(timezone.utc).date()
    return max(0, (expiry_date - today).days)

class OptionGreeksCalculator:
    """
    Calculates implied volatility and Greeks for options using the Mibian library.
    """
    def __init__(self, risk_free_rate: float = 0.0):
        self.risk_free_rate = risk_free_rate

    def calculate_greeks(
        self,
        option_type: str, # "CE" for Call, "PE" for Put
        underlying_price: float,
        strike_price: float,
        expiry_date_str: str, # YYYY-MM-DD format
        option_ltp: float,
        today_str: Optional[str] = None # For testing/historical calculation
    ) -> Dict[str, Any]:
        """
        Calculates IV and Greeks for a single option.
        Returns a dictionary of calculated values.
        """
        days = days_to_expiry(expiry_date_str, today_str)
        if days == 0:
            # Handle expiry day or already expired options
            # Mibian might have issues with 0 days, or Greeks are undefined
            return {
                "implied_volatility": 0.0,
                "delta": 0.0,
                "gamma": 0.0,
                "theta": 0.0,
                "vega": 0.0,
                "model_price": option_ltp # Assume market price on expiry
            }

        # Mibian expects risk_free_rate and volatility as percentages (e.g., 5.0 for 5%)
        # Convert risk_free_rate from decimal (0.05) to percentage (5.0)
        mibian_risk_free_rate = self.risk_free_rate * 100

        bs_inputs = [underlying_price, strike_price, mibian_risk_free_rate, days]

        try:
            if option_type.upper() == "CE":
                bs_object = mibian.BS(bs_inputs, callPrice=option_ltp)
                iv = bs_object.impliedVolatility
                greeks_object = mibian.BS(bs_inputs, volatility=iv)
                return {
                    "implied_volatility": iv,
                    "delta": greeks_object.callDelta,
                    "gamma": greeks_object.gamma,
                    "theta": greeks_object.callTheta,
                    "vega": greeks_object.vega,
                    "model_price": greeks_object.callPrice
                }
            elif option_type.upper() == "PE":
                bs_object = mibian.BS(bs_inputs, putPrice=option_ltp)
                iv = bs_object.impliedVolatility
                greeks_object = mibian.BS(bs_inputs, volatility=iv)
                return {
                    "implied_volatility": iv,
                    "delta": greeks_object.putDelta,
                    "gamma": greeks_object.gamma,
                    "theta": greeks_object.putTheta,
                    "vega": greeks_object.vega,
                    "model_price": greeks_object.putPrice
                }
            else:
                raise ValueError("option_type must be 'CE' or 'PE'")
        except Exception as e:
            # Mibian can raise errors for invalid inputs (e.g., IV not found)
            print(f"Error calculating Greeks for {option_type} {strike_price} on {expiry_date_str}: {e}")
            return {
                "implied_volatility": 0.0,
                "delta": 0.0,
                "gamma": 0.0,
                "theta": 0.0,
                "vega": 0.0,
                "model_price": 0.0 # Indicate calculation failure
            }

from math import exp, log, pi, sqrt
from scipy.stats import norm

# Black-76 Model for pricing options on futures/forwards

def black76_price(option_type: str, F: float, K: float, T: float, sigma: float) -> float:
    """
    Calculates the price of a European option using the Black-76 model.
    Assumes a discount factor of 1 (zero interest rates).
    """
    if T <= 0 or sigma <= 0:
        return max(0, F - K) if option_type.upper() == 'CE' else max(0, K - F)

    d1 = (log(F / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)

    if option_type.upper() == 'CE':
        price = F * norm.cdf(d1) - K * norm.cdf(d2)
    elif option_type.upper() == 'PE':
        price = K * norm.cdf(-d2) - F * norm.cdf(-d1)
    else:
        raise ValueError("option_type must be 'CE' or 'PE'")
    return price

def black76_greeks(option_type: str, F: float, K: float, T: float, sigma: float) -> Dict[str, float]:
    """
    Calculates the Greeks for a European option using the Black-76 model.
    Delta is with respect to the forward price. Rho is included for interface
    consistency but is effectively zero with a discount factor of 1.
    """
    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    if T <= 0 or sigma <= 0:
        return greeks

    d1 = (log(F / K) + (0.5 * sigma**2) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)

    pdf_d1 = norm.pdf(d1)
    
    greeks["gamma"] = pdf_d1 / (F * sigma * sqrt(T))
    greeks["vega"] = F * pdf_d1 * sqrt(T)
    
    theta_term1 = -(F * pdf_d1 * sigma) / (2 * sqrt(T))

    if option_type.upper() == 'CE':
        greeks["delta"] = norm.cdf(d1)
        greeks["theta"] = theta_term1
    elif option_type.upper() == 'PE':
        greeks["delta"] = norm.cdf(d1) - 1
        greeks["theta"] = theta_term1
    else:
        raise ValueError("option_type must be 'CE' or 'PE'")
        
    return greeks

def implied_vol_from_price_black76(
    option_type: str, F: float, K: float, T: float, price: float,
    sigma_lo: float = 0.01, sigma_hi: float = 3.0,
    tol: float = 1e-6, max_iter: int = 50
) -> Optional[float]:
    """
    Calculates the implied volatility using the Black-76 model via bisection.
    Returns None if convergence is not achieved or inputs are invalid.
    """
    if T <= 0 or F <= 0 or K <= 0 or price <= 0:
        return None

    for _ in range(max_iter):
        sigma_mid = (sigma_lo + sigma_hi) / 2
        if sigma_mid < tol:
            return sigma_lo # Avoid division by zero if sigma is tiny
            
        price_mid = black76_price(option_type, F, K, T, sigma_mid)
        
        if abs(price_mid - price) < tol:
            return sigma_mid
            
        if price_mid < price:
            sigma_lo = sigma_mid
        else:
            sigma_hi = sigma_mid
            
    return (sigma_lo + sigma_hi) / 2

def ewma(prev: Optional[float], new: float, alpha: float = 0.2) -> float:
    """
    Calculates the Exponentially Weighted Moving Average.
    """
    if prev is None:
        return new
    return alpha * new + (1 - alpha) * prev

# Example Usage (for testing within the module)
if __name__ == "__main__":
    calculator = OptionGreeksCalculator(risk_free_rate=0.0)

    # Example from notebook
    underlying = 24736.64
    call_strike = 24650
    put_strike = 24650
    call_ltp = 210
    put_ltp = 129.90
    expiry_date = '2020-03-30' # Using a past date for consistency with notebook demo
    today_date = '2020-02-28'

    print("--- Call Option Greeks ---")
    call_greeks = calculator.calculate_greeks(
        option_type="CE",
        underlying_price=underlying,
        strike_price=call_strike,
        expiry_date_str=expiry_date,
        option_ltp=call_ltp,
        today_str=today_date
    )
    for k, v in call_greeks.items():
        print(f"{k}: {v:.4f}")

    print("\n--- Put Option Greeks ---")
    put_greeks = calculator.calculate_greeks(
        option_type="PE",
        underlying_price=underlying,
        strike_price=put_strike,
        expiry_date_str=expiry_date,
        option_ltp=put_ltp,
        today_str=today_date
    )
    for k, v in put_greeks.items():
        print(f"{k}: {v:.4f}")