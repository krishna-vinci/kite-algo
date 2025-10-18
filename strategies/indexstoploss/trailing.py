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
# PREMIUM TRAILING (DIRECTION-AWARE)
# ═══════════════════════════════════════════════════════════════════════════════

def update_premium_trailing_sell(
    config: Dict,
    current_ltp: float
) -> Tuple[bool, Optional[Dict]]:
    """
    Update trailing stoploss for SELL positions.
    
    Logic: When premium DROPS (profit increases), trail SL UP to lock profit.
    
    Args:
        config: Premium threshold config dict with trailing settings
        current_ltp: Current option LTP
    
    Returns:
        (triggered: bool, updated_config: Dict or None)
        - triggered: True if stoploss or target was hit
        - updated_config: Updated config dict if changed, None otherwise
    """
    trailing_mode = config.get('trailing_mode')
    if trailing_mode is None or trailing_mode == 'none':
        # No trailing, just check fixed levels
        return _check_fixed_levels_sell(config, current_ltp), None
    
    entry_price = config.get('entry_price')
    trailing_distance = config.get('trailing_distance')
    lock_profit = config.get('trailing_lock_profit')
    stoploss_price = config.get('stoploss_price')
    
    if trailing_distance is None:
        logger.warning("Trailing mode set but no trailing_distance provided")
        return _check_fixed_levels_sell(config, current_ltp), None
    
    # Initialize runtime fields if first time
    if config.get('lowest_premium') is None:
        config['lowest_premium'] = entry_price
        config['current_trailing_sl'] = stoploss_price
        config['activated'] = False
        logger.debug(f"SELL: Initialized trailing (entry={entry_price:.2f})")
    
    lowest_premium = config['lowest_premium']
    current_trailing_sl = config['current_trailing_sl']
    activated = config.get('activated', False)
    
    # Check if we should activate trailing (profit lock threshold)
    if not activated and lock_profit is not None:
        profit = entry_price - current_ltp  # SELL: profit when premium drops
        if profit >= lock_profit:
            activated = True
            config['activated'] = True
            logger.info(
                f"SELL trailing ACTIVATED: profit={profit:.2f} >= lock={lock_profit:.2f} "
                f"(entry={entry_price:.2f}, current={current_ltp:.2f})"
            )
    
    # Update lowest premium if current is lower
    if current_ltp < lowest_premium:
        config['lowest_premium'] = current_ltp
        lowest_premium = current_ltp
        
        # Calculate new trailing SL (trails UP)
        new_sl = current_ltp + trailing_distance
        
        # Only move SL if activated or no lock_profit set
        if activated or lock_profit is None:
            # Don't move SL above original stoploss_price
            if stoploss_price is not None:
                new_sl = min(new_sl, stoploss_price)
            
            # Only update if new SL is tighter (lower)
            if current_trailing_sl is None or new_sl < current_trailing_sl:
                config['current_trailing_sl'] = new_sl
                logger.info(
                    f"SELL SL trailed UP: {current_trailing_sl if current_trailing_sl else 'None'} → {new_sl:.2f} "
                    f"(LTP={current_ltp:.2f}, distance={trailing_distance:.2f})"
                )
                current_trailing_sl = new_sl
    
    # Check if trailing stoploss triggered
    if current_trailing_sl is not None and current_ltp >= current_trailing_sl:
        logger.info(
            f"SELL trailing stoploss TRIGGERED: LTP={current_ltp:.2f} >= SL={current_trailing_sl:.2f}"
        )
        return True, config
    
    # Check fixed stoploss (if no trailing or before activation)
    if not activated and stoploss_price is not None and current_ltp >= stoploss_price:
        logger.info(
            f"SELL fixed stoploss TRIGGERED: LTP={current_ltp:.2f} >= SL={stoploss_price:.2f}"
        )
        return True, config
    
    # Check target price
    target_price = config.get('target_price')
    if target_price is not None and current_ltp <= target_price:
        logger.info(
            f"SELL target price REACHED: LTP={current_ltp:.2f} <= Target={target_price:.2f}"
        )
        return True, config
    
    return False, config


def update_premium_trailing_buy(
    config: Dict,
    current_ltp: float
) -> Tuple[bool, Optional[Dict]]:
    """
    Update trailing stoploss for BUY positions.
    
    Logic: When premium RISES (profit increases), trail SL DOWN to lock profit.
    
    Args:
        config: Premium threshold config dict with trailing settings
        current_ltp: Current option LTP
    
    Returns:
        (triggered: bool, updated_config: Dict or None)
        - triggered: True if stoploss or target was hit
        - updated_config: Updated config dict if changed, None otherwise
    """
    trailing_mode = config.get('trailing_mode')
    if trailing_mode is None or trailing_mode == 'none':
        # No trailing, just check fixed levels
        return _check_fixed_levels_buy(config, current_ltp), None
    
    entry_price = config.get('entry_price')
    trailing_distance = config.get('trailing_distance')
    lock_profit = config.get('trailing_lock_profit')
    stoploss_price = config.get('stoploss_price')
    
    if trailing_distance is None:
        logger.warning("Trailing mode set but no trailing_distance provided")
        return _check_fixed_levels_buy(config, current_ltp), None
    
    # Initialize runtime fields if first time
    if config.get('highest_premium') is None:
        config['highest_premium'] = entry_price
        config['current_trailing_sl'] = stoploss_price
        config['activated'] = False
        logger.debug(f"BUY: Initialized trailing (entry={entry_price:.2f})")
    
    highest_premium = config['highest_premium']
    current_trailing_sl = config['current_trailing_sl']
    activated = config.get('activated', False)
    
    # Check if we should activate trailing (profit lock threshold)
    if not activated and lock_profit is not None:
        profit = current_ltp - entry_price  # BUY: profit when premium rises
        if profit >= lock_profit:
            activated = True
            config['activated'] = True
            logger.info(
                f"BUY trailing ACTIVATED: profit={profit:.2f} >= lock={lock_profit:.2f} "
                f"(entry={entry_price:.2f}, current={current_ltp:.2f})"
            )
    
    # Update highest premium if current is higher
    if current_ltp > highest_premium:
        config['highest_premium'] = current_ltp
        highest_premium = current_ltp
        
        # Calculate new trailing SL (trails DOWN)
        new_sl = current_ltp - trailing_distance
        
        # Only move SL if activated or no lock_profit set
        if activated or lock_profit is None:
            # Don't move SL below original stoploss_price
            if stoploss_price is not None:
                new_sl = max(new_sl, stoploss_price)
            
            # Only update if new SL is tighter (higher)
            if current_trailing_sl is None or new_sl > current_trailing_sl:
                config['current_trailing_sl'] = new_sl
                logger.info(
                    f"BUY SL trailed DOWN: {current_trailing_sl if current_trailing_sl else 'None'} → {new_sl:.2f} "
                    f"(LTP={current_ltp:.2f}, distance={trailing_distance:.2f})"
                )
                current_trailing_sl = new_sl
    
    # Check if trailing stoploss triggered
    if current_trailing_sl is not None and current_ltp <= current_trailing_sl:
        logger.info(
            f"BUY trailing stoploss TRIGGERED: LTP={current_ltp:.2f} <= SL={current_trailing_sl:.2f}"
        )
        return True, config
    
    # Check fixed stoploss (if no trailing or before activation)
    if not activated and stoploss_price is not None and current_ltp <= stoploss_price:
        logger.info(
            f"BUY fixed stoploss TRIGGERED: LTP={current_ltp:.2f} <= SL={stoploss_price:.2f}"
        )
        return True, config
    
    # Check target price
    target_price = config.get('target_price')
    if target_price is not None and current_ltp >= target_price:
        logger.info(
            f"BUY target price REACHED: LTP={current_ltp:.2f} >= Target={target_price:.2f}"
        )
        return True, config
    
    return False, config


def _check_fixed_levels_sell(config: Dict, current_ltp: float) -> bool:
    """Check fixed stoploss and target for SELL position (no trailing)"""
    stoploss_price = config.get('stoploss_price')
    target_price = config.get('target_price')
    
    if stoploss_price is not None and current_ltp >= stoploss_price:
        logger.info(f"SELL stoploss TRIGGERED: LTP={current_ltp:.2f} >= SL={stoploss_price:.2f}")
        return True
    
    if target_price is not None and current_ltp <= target_price:
        logger.info(f"SELL target REACHED: LTP={current_ltp:.2f} <= Target={target_price:.2f}")
        return True
    
    return False


def _check_fixed_levels_buy(config: Dict, current_ltp: float) -> bool:
    """Check fixed stoploss and target for BUY position (no trailing)"""
    stoploss_price = config.get('stoploss_price')
    target_price = config.get('target_price')
    
    if stoploss_price is not None and current_ltp <= stoploss_price:
        logger.info(f"BUY stoploss TRIGGERED: LTP={current_ltp:.2f} <= SL={stoploss_price:.2f}")
        return True
    
    if target_price is not None and current_ltp >= target_price:
        logger.info(f"BUY target REACHED: LTP={current_ltp:.2f} >= Target={target_price:.2f}")
        return True
    
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# PREMIUM P&L CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_premium_pnl(
    transaction_type: str,
    entry_price: float,
    current_ltp: float,
    quantity: int
) -> float:
    """
    Calculate P&L for a single premium position.
    
    Args:
        transaction_type: 'SELL' or 'BUY'
        entry_price: Entry premium
        current_ltp: Current LTP
        quantity: Position quantity
    
    Returns:
        P&L in rupees (positive = profit, negative = loss)
    """
    if transaction_type == 'SELL':
        # SELL: Profit when premium drops (sold high, buy back low)
        pnl = (entry_price - current_ltp) * quantity
    else:  # BUY
        # BUY: Profit when premium rises (bought low, sell high)
        pnl = (current_ltp - entry_price) * quantity
    
    return pnl


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
