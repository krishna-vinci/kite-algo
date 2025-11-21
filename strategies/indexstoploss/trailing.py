"""
Trailing stoploss logic for Position Protection System
Phase 2: Direction-aware premium trailing

Direction-aware logic:
- SELL positions: Premium drops → Trail SL UP (lock profit)
- BUY positions: Premium rises → Trail SL DOWN (lock profit)
"""

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# COMBINED PREMIUM TRAILING (Phase 4)
# ═══════════════════════════════════════════════════════════════════════════════

def update_combined_premium_trailing(
    config: Dict,
    current_net_premium: float,
    entry_type: str
) -> Tuple[bool, Optional[Dict]]:
    """
    Update trailing stoploss for combined premium strategies.
    
    Phase 4: Net P&L based trailing for straddles/strangles.
    
    Args:
        config: Strategy configuration dict
        current_net_premium: Current total premium across all positions
        entry_type: 'credit' or 'debit'
    
    Returns:
        Tuple of (triggered: bool, updated_config: Dict or None)
    """
    initial_premium = config.get('initial_net_premium')
    if initial_premium is None:
        return False, None
    
    trailing_enabled = config.get('combined_premium_trailing_enabled', False)
    if not trailing_enabled:
        return False, None
    
    trailing_distance = config.get('combined_premium_trailing_distance')
    lock_profit = config.get('combined_premium_trailing_lock_profit')
    profit_target = config.get('combined_premium_profit_target')
    
    if trailing_distance is None:
        return False, None
    
    # Calculate net P&L
    if entry_type == 'credit':
        # SELL strategy: Profit when premium decays
        net_pnl = initial_premium - current_net_premium
    else:  # debit
        # BUY strategy: Profit when premium rises
        net_pnl = current_net_premium - initial_premium
    
    # Check profit target (fixed exit)
    if profit_target and net_pnl >= profit_target:
        return True, None
    
    # Track best premium for trailing
    best_premium = config.get('best_net_premium')
    
    if entry_type == 'credit':
        # Credit: Track LOWEST premium (best profit)
        if best_premium is None or current_net_premium < best_premium:
            best_premium = current_net_premium
            config['best_net_premium'] = best_premium
    else:  # debit
        # Debit: Track HIGHEST premium (best profit)
        if best_premium is None or current_net_premium > best_premium:
            best_premium = current_net_premium
            config['best_net_premium'] = best_premium
    
    # Check if trailing should be activated
    trailing_sl = config.get('combined_premium_trailing_sl')
    activated = config.get('trailing_activated', False)
    
    if not activated and lock_profit:
        # Activate only after reaching lock_profit threshold
        if net_pnl >= lock_profit:
            activated = True
            config['trailing_activated'] = activated
    
    if not activated and lock_profit:
        # Not yet activated, don't trail
        return False, config
    
    # Update trailing stoploss
    if best_premium is not None:
        if entry_type == 'credit':
            # Credit: Trail UP (as premium drops, raise SL)
            new_trailing_sl = best_premium + trailing_distance
            
            if trailing_sl is None or new_trailing_sl < trailing_sl:
                config['combined_premium_trailing_sl'] = new_trailing_sl
                trailing_sl = new_trailing_sl
            
            # Check if trailing SL hit (premium rose back up)
            if current_net_premium >= trailing_sl:
                return True, config
        
        else:  # debit
            # Debit: Trail DOWN (as premium rises, lower SL)
            new_trailing_sl = best_premium - trailing_distance
            
            if trailing_sl is None or new_trailing_sl > trailing_sl:
                config['combined_premium_trailing_sl'] = new_trailing_sl
                trailing_sl = new_trailing_sl
            
            # Check if trailing SL hit (premium dropped back down)
            if current_net_premium <= trailing_sl:
                return True, config
    
    return False, config
