"""
Strike Selector for Position Protection System
Phase 3: Delta-based strike selection and automated position building
"""

import logging
from datetime import date
from typing import Dict, List, Optional, Tuple, Any

from broker_api.instruments_repository import InstrumentsRepository
from broker_api.options_sessions import OptionsSessionManager

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# STRIKE SELECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class StrikeSelector:
    """
    Handles delta-based strike selection and position building.
    
    Integrates with OptionsSessionManager for real-time Greeks
    and InstrumentsRepository for strike lookups.
    """
    
    def __init__(
        self,
        options_session_manager: OptionsSessionManager,
        instruments_repo: InstrumentsRepository
    ):
        self.osm = options_session_manager
        self.repo = instruments_repo
    
    async def get_mini_chain(
        self,
        underlying: str,
        expiry: date,
        center_strike: Optional[float] = None,
        count: int = 11
    ) -> Dict[str, Any]:
        """
        Get a mini option chain with live Greeks.
        
        Args:
            underlying: Index symbol (e.g., 'NIFTY', 'BANKNIFTY')
            expiry: Expiry date
            center_strike: Center strike (if None, uses ATM)
            count: Number of strikes to return (default: 11)
        
        Returns:
            Dict with chain data including strikes, premiums, Greeks
        """
        # Get session snapshot
        session = self.osm.sessions.get(underlying)
        if not session or not session.snapshot:
            return {
                "error": f"No active session for {underlying}",
                "underlying": underlying,
                "expiry": expiry.isoformat()
            }
        
        snapshot = session.snapshot
        spot_ltp = snapshot.get('spot_ltp')
        
        if not spot_ltp:
            return {
                "error": "No spot LTP available",
                "underlying": underlying,
                "expiry": expiry.isoformat()
            }
        
        # Get expiry data
        expiry_str = expiry.isoformat()
        expiry_data = snapshot.get('per_expiry', {}).get(expiry_str)
        
        if not expiry_data:
            return {
                "error": f"No data available for expiry {expiry_str}",
                "underlying": underlying,
                "expiry": expiry_str,
                "available_expiries": snapshot.get('expiries', [])
            }
        
        # Determine center strike
        if center_strike is None:
            center_strike = expiry_data.get('atm_strike')
        
        if not center_strike:
            return {
                "error": "Could not determine ATM strike",
                "underlying": underlying,
                "expiry": expiry_str
            }
        
        # Get all strikes and window around center
        all_strikes = self.repo.get_distinct_strikes(underlying, expiry)
        window_strikes = self.repo.get_strikes_around_atm(center_strike, all_strikes, count)
        
        # Build mini chain from snapshot rows
        rows = expiry_data.get('rows', [])
        formatted_strikes = []
        
        for row in rows:
            if row['strike'] not in window_strikes:
                continue
                
            # Transform CE side
            ce_data = None
            if row.get('CE'):
                ce = row['CE']
                # Get lot size from instruments repo using token
                lot_size = self.repo.get_lot_size(ce.get('token'))
                if not lot_size:
                    # Fallback defaults based on underlying
                    lot_size = 50 if underlying == 'NIFTY' else 15
                
                ce_data = {
                    "instrument_token": ce.get('token'),
                    "tradingsymbol": ce.get('tsym'),
                    "ltp": ce.get('ltp') or 0,
                    "lot_size": lot_size,
                    "oi": ce.get('oi'),
                    "greeks": {
                        "delta": ce.get('delta') or 0,
                        "gamma": ce.get('gamma') or 0,
                        "theta": ce.get('theta') or 0,
                        "vega": ce.get('vega') or 0,
                        "iv": ce.get('iv') or 0
                    }
                }
            
            # Transform PE side
            pe_data = None
            if row.get('PE'):
                pe = row['PE']
                # Get lot size from instruments repo using token
                lot_size = self.repo.get_lot_size(pe.get('token'))
                if not lot_size:
                    # Fallback defaults based on underlying
                    lot_size = 50 if underlying == 'NIFTY' else 15
                
                pe_data = {
                    "instrument_token": pe.get('token'),
                    "tradingsymbol": pe.get('tsym'),
                    "ltp": pe.get('ltp') or 0,
                    "lot_size": lot_size,
                    "oi": pe.get('oi'),
                    "greeks": {
                        "delta": pe.get('delta') or 0,
                        "gamma": pe.get('gamma') or 0,
                        "theta": pe.get('theta') or 0,
                        "vega": pe.get('vega') or 0,
                        "iv": pe.get('iv') or 0
                    }
                }
            
            formatted_strikes.append({
                "strike": row['strike'],
                "ce": ce_data,
                "pe": pe_data,
                "is_atm": row['strike'] == center_strike
            })
        
        return {
            "underlying": underlying,
            "expiry": expiry_str,
            "spot_price": spot_ltp,
            "atm_strike": center_strike,
            "strikes": formatted_strikes,
            "timestamp": snapshot.get('updated_at')
        }
    
    def find_strike_by_delta(
        self,
        chain_data: Dict[str, Any],
        option_type: str,
        target_delta: float,
        tolerance: float = 0.05
    ) -> Optional[Dict[str, Any]]:
        """
        Find strike closest to target delta.
        
        Args:
            chain_data: Mini chain data from get_mini_chain()
            option_type: 'CE' or 'PE'
            target_delta: Target delta value (e.g., 0.30 for 30 delta)
            tolerance: Acceptable deviation (default: 0.05)
        
        Returns:
            Dict with strike data, or None if not found
        """
        if not chain_data.get('strikes'):
            return None
        
        # For CE: delta is positive (0 to 1)
        # For PE: delta is negative (-1 to 0), but we work with absolute values
        target_abs = abs(target_delta)
        
        best_match = None
        best_delta_diff = float('inf')
        
        option_key = 'ce' if option_type == 'CE' else 'pe'

        for row in chain_data['strikes']:
            option_data = row.get(option_key)
            if not option_data:
                continue
            
            greeks = option_data.get('greeks') or {}
            delta = greeks.get('delta')
            if delta is None:
                continue
            
            delta_abs = abs(delta)
            delta_diff = abs(delta_abs - target_abs)
            
            if delta_diff < best_delta_diff:
                best_delta_diff = delta_diff
                best_match = {
                    'strike': row['strike'],
                    'option_type': option_type,
                    'delta': delta,
                    'delta_abs': delta_abs,
                    'ltp': option_data.get('ltp'),
                    'token': option_data.get('instrument_token'),
                    'tsym': option_data.get('tradingsymbol'),
                    'iv': greeks.get('iv'),
                    'gamma': greeks.get('gamma'),
                    'theta': greeks.get('theta'),
                    'vega': greeks.get('vega')
                }
        
        if best_match and best_delta_diff <= tolerance:
            return best_match
        
        return None
    
    async def suggest_strikes(
        self,
        underlying: str,
        expiry: date,
        strategy_type: str,
        target_delta: float = 0.30,
        risk_amount: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Suggest strikes for a strategy based on delta.
        
        Args:
            underlying: Index symbol
            expiry: Target expiry
            strategy_type: 'straddle', 'strangle', 'iron_condor', 'single_leg'
            target_delta: Target delta for option selection (default: 0.30)
            risk_amount: Maximum risk in rupees (for lot calculation)
        
        Returns:
            Dict with suggested strikes and position details
        """
        # Get mini chain
        chain_data = await self.get_mini_chain(underlying, expiry, count=15)
        
        if 'error' in chain_data:
            return chain_data
        
        suggestions = {
            "underlying": underlying,
            "expiry": expiry.isoformat(),
            "strategy_type": strategy_type,
            "target_delta": target_delta,
            "spot_ltp": chain_data.get('spot_ltp'),
            "atm_strike": chain_data.get('atm_strike'),
            "legs": []
        }
        
        if strategy_type == 'straddle':
            # ATM straddle
            atm_strike = chain_data.get('atm_strike')
            ce_leg = self._find_leg_by_strike(chain_data, atm_strike, 'CE')
            pe_leg = self._find_leg_by_strike(chain_data, atm_strike, 'PE')
            
            if ce_leg:
                suggestions['legs'].append({**ce_leg, 'transaction_type': 'SELL', 'leg_name': 'CE_SHORT'})
            if pe_leg:
                suggestions['legs'].append({**pe_leg, 'transaction_type': 'SELL', 'leg_name': 'PE_SHORT'})
        
        elif strategy_type == 'strangle':
            # OTM strangle based on delta
            ce_leg = self.find_strike_by_delta(chain_data, 'CE', target_delta)
            pe_leg = self.find_strike_by_delta(chain_data, 'PE', target_delta)
            
            if ce_leg:
                suggestions['legs'].append({**ce_leg, 'transaction_type': 'SELL', 'leg_name': 'CE_SHORT'})
            if pe_leg:
                suggestions['legs'].append({**pe_leg, 'transaction_type': 'SELL', 'leg_name': 'PE_SHORT'})
        
        elif strategy_type == 'single_leg':
            # Single OTM option
            ce_leg = self.find_strike_by_delta(chain_data, 'CE', target_delta)
            if ce_leg:
                suggestions['legs'].append({**ce_leg, 'transaction_type': 'SELL', 'leg_name': 'CE_SHORT'})
        
        elif strategy_type == 'iron_condor':
            # Iron condor: Sell OTM strangle + Buy further OTM for protection
            # Sell legs at target_delta
            ce_sell = self.find_strike_by_delta(chain_data, 'CE', target_delta)
            pe_sell = self.find_strike_by_delta(chain_data, 'PE', target_delta)
            
            # Buy legs at target_delta * 0.5 (further OTM)
            ce_buy = self.find_strike_by_delta(chain_data, 'CE', target_delta * 0.5, tolerance=0.10)
            pe_buy = self.find_strike_by_delta(chain_data, 'PE', target_delta * 0.5, tolerance=0.10)
            
            if ce_sell:
                suggestions['legs'].append({**ce_sell, 'transaction_type': 'SELL', 'leg_name': 'CE_SHORT'})
            if pe_sell:
                suggestions['legs'].append({**pe_sell, 'transaction_type': 'SELL', 'leg_name': 'PE_SHORT'})
            if ce_buy:
                suggestions['legs'].append({**ce_buy, 'transaction_type': 'BUY', 'leg_name': 'CE_LONG'})
            if pe_buy:
                suggestions['legs'].append({**pe_buy, 'transaction_type': 'BUY', 'leg_name': 'PE_LONG'})
        
        # Calculate lots if risk_amount provided
        if risk_amount and suggestions['legs']:
            suggestions = self._calculate_lots_for_risk(suggestions, risk_amount)
        
        return suggestions
    
    def _find_leg_by_strike(
        self,
        chain_data: Dict[str, Any],
        strike: float,
        option_type: str
    ) -> Optional[Dict[str, Any]]:
        """Find option leg by exact strike"""
        option_key = 'ce' if option_type == 'CE' else 'pe'
        for row in chain_data.get('strikes', []):
            if row['strike'] == strike:
                option_data = row.get(option_key)
                if option_data:
                    greeks = option_data.get('greeks') or {}
                    return {
                        'strike': strike,
                        'option_type': option_type,
                        'delta': greeks.get('delta'),
                        'ltp': option_data.get('ltp'),
                        'token': option_data.get('instrument_token'),
                        'tsym': option_data.get('tradingsymbol'),
                        'iv': greeks.get('iv')
                    }
        return None
    
    def _calculate_lots_for_risk(
        self,
        suggestions: Dict[str, Any],
        risk_amount: float
    ) -> Dict[str, Any]:
        """
        Calculate lot sizes based on risk amount.
        
        For credit strategies (straddle/strangle), risk is unlimited,
        so we calculate based on margin or premium received.
        """
        # Get lot size from first leg
        first_leg = suggestions['legs'][0] if suggestions['legs'] else None
        if not first_leg:
            return suggestions
        
        token = first_leg.get('token')
        if not token:
            return suggestions
        
        lot_size = self.repo.get_lot_size(token)
        if not lot_size:
            lot_size = 50  # Default for NIFTY/BANKNIFTY
        
        # Calculate total premium for one lot
        total_premium = 0
        for leg in suggestions['legs']:
            ltp = leg.get('ltp', 0) or 0
            multiplier = 1 if leg['transaction_type'] == 'SELL' else -1
            total_premium += ltp * lot_size * multiplier
        
        # Calculate lots based on risk amount
        if total_premium > 0:
            # Credit strategy: Use 10% of risk as premium target
            target_premium = risk_amount * 0.10
            lots = max(1, int(target_premium / total_premium))
        else:
            # Debit strategy: Use full risk amount
            lots = max(1, int(risk_amount / abs(total_premium)))
        
        # Add lot information to suggestions
        suggestions['lot_size'] = lot_size
        suggestions['recommended_lots'] = lots
        suggestions['total_premium_per_lot'] = total_premium
        suggestions['total_premium'] = total_premium * lots
        suggestions['quantity_per_leg'] = lot_size * lots
        
        return suggestions


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

class PositionBuilder:
    """
    Builds positions atomically with protection strategies.
    """
    
    def __init__(
        self,
        strike_selector: StrikeSelector,
        instruments_repo: InstrumentsRepository
    ):
        self.selector = strike_selector
        self.repo = instruments_repo

    def _manual_strategy_leg(self, strike: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "instrument_token": strike["instrument_token"],
            "tradingsymbol": strike["tradingsymbol"],
            "strike": strike["strike"],
            "option_type": strike["option_type"],
            "transaction_type": strike["transaction_type"],
            "ltp": strike["ltp"],
            "lot_size": strike["lot_size"],
            "lots": strike["lots"],
            "quantity": strike["lot_size"] * strike["lots"],
        }

    def _suggestion_strategy_leg(self, leg: Dict[str, Any], quantity: int) -> Dict[str, Any]:
        lot_size = self.repo.get_lot_size(leg.get("token")) or 1
        resolved_quantity = max(quantity or 0, lot_size)
        lots = max(1, int(resolved_quantity / lot_size)) if resolved_quantity else 1
        return {
            "instrument_token": leg["token"],
            "tradingsymbol": leg["tsym"],
            "strike": leg["strike"],
            "option_type": leg["option_type"],
            "transaction_type": leg["transaction_type"],
            "ltp": leg.get("ltp") or 0,
            "lot_size": lot_size,
            "lots": lots,
            "quantity": resolved_quantity,
        }
    
    async def build_position_plan_from_strikes(
        self,
        underlying: str,
        expiry: date,
        strategy_type: str,
        selected_strikes: List[Dict[str, Any]],
        protection_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a position building plan from manually selected strikes.
        
        Args:
            underlying: Index symbol
            expiry: Target expiry
            strategy_type: Strategy type
            selected_strikes: List of manually selected strikes with lots
            protection_config: Protection strategy configuration
        
        Returns:
            Complete position plan with orders and protection setup
        """
        # Build order plan from selected strikes
        orders = []
        total_premium = 0
        
        for strike in selected_strikes:
            qty = strike['lot_size'] * strike['lots']
            premium = strike['ltp'] * qty
            
            # Credit for SELL, debit for BUY
            if strike['transaction_type'] == 'SELL':
                total_premium += premium
            else:
                total_premium -= premium
            
            orders.append({
                'tradingsymbol': strike['tradingsymbol'],
                'instrument_token': strike['instrument_token'],
                'exchange': 'NFO',
                'transaction_type': strike['transaction_type'],
                'quantity': qty,
                'lot_size': strike['lot_size'],
                'lots': strike['lots'],
                'product': 'MIS',
                'order_type': 'MARKET',
                'price': 0,
                'strike': strike['strike'],
                'option_type': strike['option_type'],
                'estimated_price': strike['ltp']
            })
        
        # Build protection plan
        protection_plan = None
        if protection_config:
            protection_plan = self._build_protection_plan_from_strikes(
                selected_strikes, protection_config, underlying
            )
        
        return {
            "plan_type": "position_build",
            "underlying": underlying,
            "expiry": expiry.isoformat(),
            "strategy_type": strategy_type,
            "strategy_legs": [self._manual_strategy_leg(strike) for strike in selected_strikes],
            "orders": orders,
            "protection_plan": protection_plan,
            "total_lots": sum(s['lots'] for s in selected_strikes),
            "estimated_cost": abs(total_premium),
            "estimated_margin": abs(total_premium) * 0.25,  # Rough estimate
            "max_profit": total_premium if total_premium > 0 else None,
            "max_loss": abs(total_premium) if total_premium < 0 else None
        }
    
    async def build_position_plan(
        self,
        underlying: str,
        expiry: date,
        strategy_type: str,
        target_delta: float = 0.30,
        risk_amount: Optional[float] = None,
        protection_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a position building plan (dry run).
        
        Args:
            underlying: Index symbol
            expiry: Target expiry
            strategy_type: Strategy type
            target_delta: Target delta
            risk_amount: Risk amount in rupees
            protection_config: Protection strategy configuration
        
        Returns:
            Complete position plan with orders and protection setup
        """
        # Get strike suggestions
        suggestions = await self.selector.suggest_strikes(
            underlying, expiry, strategy_type, target_delta, risk_amount
        )
        
        if 'error' in suggestions:
            return suggestions
        
        # Build order plan
        orders = []
        for leg in suggestions.get('legs', []):
            orders.append({
                'tradingsymbol': leg['tsym'],
                'instrument_token': leg['token'],
                'exchange': 'NFO',
                'transaction_type': leg['transaction_type'],
                'quantity': suggestions.get('quantity_per_leg', 0),
                'product': 'MIS',
                'order_type': 'MARKET',
                'price': 0,
                'strike': leg['strike'],
                'option_type': leg['option_type'],
                'expected_ltp': leg['ltp']
            })
        
        # Build protection plan
        protection_plan = None
        if protection_config:
            protection_plan = self._build_protection_plan(
                suggestions, protection_config
            )
        
        return {
            "plan_type": "position_build",
            "underlying": underlying,
            "expiry": expiry.isoformat(),
            "strategy_type": strategy_type,
            "target_delta": target_delta,
            "suggestions": suggestions,
            "strategy_legs": [
                self._suggestion_strategy_leg(leg, suggestions.get('quantity_per_leg', 0))
                for leg in suggestions.get('legs', [])
            ],
            "orders": orders,
            "protection_plan": protection_plan,
            "execution_summary": {
                "total_legs": len(orders),
                "total_quantity": suggestions.get('quantity_per_leg', 0) * len(orders),
                "estimated_premium": suggestions.get('total_premium', 0),
                "lot_size": suggestions.get('lot_size', 0),
                "lots": suggestions.get('recommended_lots', 0)
            }
        }
    
    def _build_protection_plan_from_strikes(
        self,
        selected_strikes: List[Dict[str, Any]],
        protection_config: Dict[str, Any],
        underlying: str
    ) -> Optional[Dict[str, Any]]:
        """Build protection plan from manually selected strikes."""
        if not protection_config.get('enabled'):
            return None
        
        return {
            "monitoring_mode": protection_config.get('monitoring_mode', 'index'),
            "index_tradingsymbol": protection_config.get('index_tradingsymbol'),
            "index_upper_stoploss": protection_config.get('index_upper_stoploss'),
            "index_lower_stoploss": protection_config.get('index_lower_stoploss'),
            "trailing_enabled": protection_config.get('trailing_enabled', False),
            "trailing_distance": protection_config.get('trailing_distance')
        }
    
    def _build_protection_plan(
        self,
        suggestions: Dict[str, Any],
        protection_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Build protection strategy configuration.
        
        Creates premium_thresholds for each leg based on config.
        """
        monitoring_mode = protection_config.get('monitoring_mode', 'premium')
        stoploss_percent = protection_config.get('premium_stoploss_percent', 100)
        target_percent = protection_config.get('premium_target_percent', 50)
        trailing_enabled = protection_config.get('trailing_enabled', True)
        trailing_distance = protection_config.get('trailing_distance', 20.0)
        
        premium_thresholds = {}
        
        for leg in suggestions.get('legs', []):
            token = str(leg['token'])
            entry_price = leg.get('ltp', 0) or 0
            transaction_type = leg['transaction_type']
            
            # Calculate stoploss and target
            if transaction_type == 'SELL':
                stoploss_price = entry_price * (1 + stoploss_percent / 100)
                target_price = entry_price * (1 - target_percent / 100)
            else:  # BUY
                stoploss_price = entry_price * (1 - stoploss_percent / 100)
                target_price = entry_price * (1 + target_percent / 100)
            
            premium_thresholds[token] = {
                'tradingsymbol': leg['tsym'],
                'transaction_type': transaction_type,
                'entry_price': entry_price,
                'stoploss_price': stoploss_price,
                'target_price': target_price,
                'trailing_mode': 'continuous' if trailing_enabled else 'none',
                'trailing_distance': trailing_distance if trailing_enabled else None,
                'trailing_lock_profit': entry_price * 0.20 if trailing_enabled else None
            }
        
        return {
            'monitoring_mode': monitoring_mode,
            'premium_thresholds': premium_thresholds,
            'strategy_type': suggestions.get('strategy_type', 'manual')
        }
