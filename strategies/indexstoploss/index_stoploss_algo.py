"""
Position Protection Engine - Core Implementation
Phase 1: Index-based monitoring with bracket stoploss support

Architecture follows proven AlertsEngine pattern:
- Single-process, no multi-threading
- 500ms evaluation loop with in-memory cache
- DB refresh every 5 seconds
- Async order execution with idempotency
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set
from uuid import UUID

from broker_api.kite_orders import OrdersService, PlaceOrderRequest, TransactionType, Variety, Product, OrderType, Validity, Exchange
from broker_api.websocket_manager import WebSocketManager
from database import Database

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION PROTECTION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class PositionProtectionEngine:
    """
    Main controller for position protection system.
    
    Monitors positions and executes exits based on:
    - Index price movements (with two-way bracket support)
    - Premium price movements (Phase 2+)
    - Hybrid logic (Phase 4+)
    - Combined premium P&L (Phase 4+)
    
    Phase 1 Focus: Index-based monitoring only
    """
    
    def __init__(
        self,
        db: Database,
        ws_manager: WebSocketManager,
        orders_service: OrdersService,
        app: Any,
        interval_ms: int = 500,
        refresh_interval_sec: int = 5
    ):
        """
        Initialize the engine.
        
        Args:
            db: Async database connection
            ws_manager: WebSocket manager for price feeds
            orders_service: Order placement service
            app: FastAPI app instance (for state access)
            interval_ms: Evaluation loop interval (default 500ms)
            refresh_interval_sec: DB refresh interval (default 5s)
        """
        self.db = db
        self.ws_manager = ws_manager
        self.orders_service = orders_service
        self.app = app
        self.interval_ms = interval_ms
        self.refresh_interval_sec = refresh_interval_sec
        
        # In-memory cache of active strategies
        self._strategies: Dict[str, Dict] = {}
        self._last_db_refresh = 0
        
        # Server-side subscriptions that this engine owns
        self._engine_subscribed_tokens: Set[int] = set()
        
        # Task management
        self._task: Optional[asyncio.Task] = None
        self._running = False
        
        # WS status tracking to re-assert subscriptions on reconnect
        self._last_ws_status: Optional[str] = None
        
        # Statistics
        self._stats = {
            "evaluations": 0,
            "triggers": 0,
            "orders_placed": 0,
            "errors": 0
        }
        
        logger.info(
            f"PositionProtectionEngine initialized "
            f"(interval={interval_ms}ms, refresh={refresh_interval_sec}s)"
        )
    
    def start(self):
        """Start the evaluation loop"""
        if self._running:
            logger.warning("Engine already running")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._evaluation_loop())
        logger.info("PositionProtectionEngine started")
    
    async def stop(self):
        """Stop the evaluation loop"""
        if not self._running:
            return
        
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        # Unsubscribe engine-owned tokens
        await self._engine_unsubscribe_all_safe()
        
        logger.info("PositionProtectionEngine stopped")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # MAIN EVALUATION LOOP
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _evaluation_loop(self):
        """
        Main evaluation loop (runs every 500ms).
        
        Pattern:
        1. Get latest prices from WebSocket
        2. Refresh strategies from DB (every 5s)
        3. Evaluate each active strategy
        4. Sleep for interval
        """
        logger.info("Evaluation loop starting...")
        
        while self._running:
            try:
                loop_start = asyncio.get_event_loop().time()
                
                # 0. Re-assert engine subscriptions on WS reconnect
                await self._handle_ws_reconnect()
                
                # 1. Refresh strategies from DB if needed
                await self._refresh_strategies_if_needed()
                
                # 2. Get latest price data from WebSocket
                latest_ticks = self.ws_manager.latest_ticks
                
                # 3. Evaluate each active strategy
                for strategy_id, strategy in list(self._strategies.items()):
                    try:
                        await self._evaluate_strategy(strategy, latest_ticks)
                    except Exception as e:
                        logger.error(
                            f"Error evaluating strategy {strategy_id}: {e}",
                            exc_info=True
                        )
                        self._stats["errors"] += 1
                
                self._stats["evaluations"] += 1
                
                # 4. Sleep for remaining interval
                elapsed_ms = (asyncio.get_event_loop().time() - loop_start) * 1000
                sleep_ms = max(0, self.interval_ms - elapsed_ms)
                
                if sleep_ms > 0:
                    await asyncio.sleep(sleep_ms / 1000)
                else:
                    logger.warning(
                        f"Evaluation took {elapsed_ms:.1f}ms "
                        f"(exceeds {self.interval_ms}ms interval)"
                    )
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in evaluation loop: {e}", exc_info=True)
                await asyncio.sleep(1)  # Back off on error
    
    # ═══════════════════════════════════════════════════════════════════════════
    # STRATEGY LOADING
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _refresh_strategies_if_needed(self):
        """Refresh strategies from DB every N seconds"""
        now = asyncio.get_event_loop().time()
        
        if now - self._last_db_refresh < self.refresh_interval_sec:
            return
        
        await self._load_active_strategies()
        self._last_db_refresh = now
    
    async def _load_active_strategies(self):
        """Load all active/partial strategies from database"""
        try:
            import json
            
            query = """
                SELECT 
                    id, name, strategy_type, monitoring_mode, status,
                    index_instrument_token, index_tradingsymbol, index_exchange,
                    index_upper_stoploss, index_lower_stoploss,
                    stoploss_order_type, stoploss_limit_offset,
                    trailing_mode, trailing_distance, trailing_unit,
                    trailing_lock_profit, trailing_highest_price,
                    trailing_current_level, trailing_activated,
                    premium_thresholds, exit_logic,
                    position_snapshot, remaining_quantities,
                    placed_orders, stoploss_executed,
                    last_evaluated_price, last_evaluated_at,
                    created_at, updated_at
                FROM position_protection_strategies
                WHERE status IN ('active', 'partial')
                ORDER BY created_at DESC
            """
            
            rows = await self.db.fetch_all(query)
            
            # Update in-memory cache
            new_strategies = {}
            for row in rows:
                strategy_id = str(row['id'])
                strategy_dict = dict(row)
                
                # Parse JSONB fields from string to dict/list if needed
                # The async database driver may return JSONB as strings
                jsonb_fields = {
                    'premium_thresholds': {},
                    'position_snapshot': [],
                    'remaining_quantities': {},
                    'placed_orders': [],
                    'execution_errors': [],
                    'levels_executed': [],
                    'combined_premium_levels': [],
                    'exit_levels': [],
                    'takeprofit_levels': []
                }
                
                for field, default_value in jsonb_fields.items():
                    value = strategy_dict.get(field)
                    if value is not None and isinstance(value, str):
                        try:
                            strategy_dict[field] = json.loads(value)
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse {field} for strategy {strategy_id}, using default")
                            strategy_dict[field] = default_value
                
                new_strategies[strategy_id] = strategy_dict
            
            # Collect all required tokens from active strategies (Phase 2: Add option tokens)
            required_tokens = set()
            for strategy in new_strategies.values():
                # Add index token
                if strategy['index_instrument_token']:
                    required_tokens.add(strategy['index_instrument_token'])
                
                # Add option tokens from premium_thresholds (Phase 2)
                premium_thresholds = strategy.get('premium_thresholds')
                if premium_thresholds:
                    for token_str in premium_thresholds.keys():
                        required_tokens.add(int(token_str))
            
            # Update subscriptions (only for tokens not used by clients)
            await self._update_engine_subscriptions(required_tokens)
            
            self._strategies = new_strategies
            logger.debug(f"Loaded {len(self._strategies)} active strategies")
            
        except Exception as e:
            logger.error(f"Failed to load strategies: {e}", exc_info=True)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # STRATEGY EVALUATION
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _evaluate_strategy(self, strategy: Dict, latest_ticks: Dict[int, Dict]):
        """
        Evaluate a single strategy.
        
        Phase 2: Index and Premium modes
        - Check bracket stoploss (upper and lower boundaries)
        - Check premium triggers with direction-aware trailing
        - Execute exits when triggered
        """
        strategy_id = str(strategy['id'])
        monitoring_mode = strategy['monitoring_mode']
        
        if monitoring_mode == 'index':
            # Phase 1: Index mode
            await self._check_index_triggers(strategy, latest_ticks)
        
        elif monitoring_mode == 'premium':
            # Phase 2: Premium mode (NEW)
            await self._check_premium_triggers(strategy, latest_ticks)
        
        elif monitoring_mode == 'hybrid':
            # Phase 2: Hybrid mode (NEW)
            await self._check_hybrid_triggers(strategy, latest_ticks)
        
        elif monitoring_mode == 'combined_premium':
            # Phase 4: Combined premium mode (NEW)
            await self._check_combined_premium_triggers(strategy, latest_ticks)
    
    async def _check_index_triggers(self, strategy: Dict, latest_ticks: Dict[int, Dict]):
        """
        Check index-based bracket stoploss.
        
        Supports TWO-WAY protection:
        - Upper boundary: Exit if index >= upper_stoploss (protects from rally)
        - Lower boundary: Exit if index <= lower_stoploss (protects from crash)
        
        For market-neutral strategies (straddles/strangles), both should be set.
        For directional strategies, set only the relevant boundary.
        """
        index_token = strategy.get('index_instrument_token')
        if not index_token:
            return
        
        # Check if we have price data for this index
        if index_token not in latest_ticks:
            logger.debug(
                f"Strategy {strategy['id']}: No price data for index token {index_token}"
            )
            return
        
        current_index_price = latest_ticks[index_token]['last_price']
        strategy_id = str(strategy['id'])
        
        # Check UPPER boundary (protects from upward rally)
        upper_sl = strategy.get('index_upper_stoploss')
        if upper_sl is not None and current_index_price >= upper_sl:
            logger.info(
                f"Strategy {strategy_id}: Index UPPER stoploss triggered! "
                f"Index={current_index_price:.2f} >= {upper_sl:.2f}"
            )
            await self._execute_exit(
                strategy,
                "index_upper_stoploss_triggered",
                current_index_price
            )
            return
        
        # Check LOWER boundary (protects from downward crash)
        lower_sl = strategy.get('index_lower_stoploss')
        if lower_sl is not None and current_index_price <= lower_sl:
            logger.info(
                f"Strategy {strategy_id}: Index LOWER stoploss triggered! "
                f"Index={current_index_price:.2f} <= {lower_sl:.2f}"
            )
            await self._execute_exit(
                strategy,
                "index_lower_stoploss_triggered",
                current_index_price
            )
            return
        
        # Update last evaluated price
        await self._update_last_evaluated(strategy_id, current_index_price)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ORDER EXECUTION
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _execute_exit(
        self,
        strategy: Dict,
        trigger_reason: str,
        trigger_price: float
    ):
        """
        Execute exit orders for all positions in the strategy.
        
        Uses idempotency to prevent duplicate orders.
        Phase 1: MARKET orders only
        """
        strategy_id = str(strategy['id'])
        
        # Check if already executed
        if strategy.get('stoploss_executed'):
            logger.info(f"Strategy {strategy_id}: Already executed, skipping")
            return
        
        correlation_id = str(uuid.uuid4())
        position_snapshot = strategy.get('position_snapshot', [])
        
        if not position_snapshot:
            logger.warning(f"Strategy {strategy_id}: No positions to exit")
            return
        
        logger.info(
            f"Strategy {strategy_id}: Executing exit for {len(position_snapshot)} positions "
            f"(reason={trigger_reason}, price={trigger_price:.2f})"
        )
        
        # Get KiteConnect instance from app state
        kite = await self._get_kite_instance()
        if not kite:
            logger.error("Failed to get KiteConnect instance, cannot place orders")
            await self._log_event(
                strategy_id,
                "error",
                error_message="Failed to get KiteConnect instance",
                meta={"trigger_reason": trigger_reason}
            )
            return
        
        orders_placed = []
        errors = []
        
        # Place exit order for each position
        for position in position_snapshot:
            try:
                # Determine opposite transaction type for exit
                exit_transaction = (
                    TransactionType.BUY if position['transaction_type'] == 'SELL'
                    else TransactionType.SELL
                )
                
                # Generate idempotency key
                idempotency_key = f"strategy_{strategy_id[:8]}_{trigger_reason}_{position['instrument_token']}"
                
                # Create order request
                order_req = PlaceOrderRequest(
                    exchange=Exchange.NFO,
                    tradingsymbol=position['tradingsymbol'],
                    transaction_type=exit_transaction,
                    variety=Variety.REGULAR,
                    product=Product[position['product']],
                    order_type=OrderType.MARKET,
                    quantity=position['quantity'],
                    validity=Validity.DAY
                )
                
                # Place order using OrdersService
                order_response = await self.orders_service.place_order(
                    kite=kite,
                    req=order_req,
                    corr_id=correlation_id,
                    idempotency_key=idempotency_key,
                    session_id="system"
                )
                
                order_id = order_response.order_id
                
                orders_placed.append({
                    "order_id": order_id,
                    "instrument_token": position['instrument_token'],
                    "tradingsymbol": position['tradingsymbol'],
                    "quantity": position['quantity'],
                    "transaction_type": exit_transaction.value,
                    "idempotency_key": idempotency_key,
                    "correlation_id": correlation_id,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                
                logger.info(
                    f"Order placed: {order_id} for {position['tradingsymbol']} "
                    f"({exit_transaction.value} {position['quantity']})"
                )
                
                # Log order placed event
                await self._log_event(
                    strategy_id,
                    "order_placed",
                    order_id=order_id,
                    instrument_token=position['instrument_token'],
                    quantity_affected=position['quantity'],
                    trigger_price=trigger_price,
                    meta={
                        "trigger_reason": trigger_reason,
                        "tradingsymbol": position['tradingsymbol'],
                        "correlation_id": correlation_id
                    }
                )
                
            except Exception as e:
                error_msg = f"Failed to place order for {position['tradingsymbol']}: {e}"
                logger.error(error_msg, exc_info=True)
                errors.append({
                    "tradingsymbol": position['tradingsymbol'],
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
        
        # Update strategy in database
        await self._update_strategy_after_exit(
            strategy_id,
            trigger_reason,
            trigger_price,
            orders_placed,
            errors
        )
        
        # Update stats
        self._stats["triggers"] += 1
        self._stats["orders_placed"] += len(orders_placed)
        
        logger.info(
            f"Strategy {strategy_id}: Exit complete "
            f"({len(orders_placed)} orders placed, {len(errors)} errors)"
        )
    
    async def _update_strategy_after_exit(
        self,
        strategy_id: str,
        trigger_reason: str,
        trigger_price: float,
        orders_placed: List[Dict],
        errors: List[Dict]
    ):
        """Update strategy status and order history after exit execution"""
        try:
            query = """
                UPDATE position_protection_strategies
                SET 
                    status = 'triggered',
                    stoploss_executed = TRUE,
                    placed_orders = placed_orders || $1::jsonb,
                    execution_errors = execution_errors || $2::jsonb,
                    last_evaluated_price = $3,
                    last_evaluated_at = NOW(),
                    updated_at = NOW()
                WHERE id = $4
            """
            
            import json
            await self.db.execute(
                query,
                json.dumps(orders_placed),
                json.dumps(errors),
                trigger_price,
                UUID(strategy_id)
            )
            
            # Log trigger event
            await self._log_event(
                strategy_id,
                trigger_reason,
                trigger_price=trigger_price,
                meta={
                    "orders_placed": len(orders_placed),
                    "errors": len(errors)
                }
            )
            
            # Remove from in-memory cache (will be reloaded as 'triggered' on next refresh)
            if strategy_id in self._strategies:
                del self._strategies[strategy_id]
            
        except Exception as e:
            logger.error(f"Failed to update strategy {strategy_id}: {e}", exc_info=True)
    
    async def _update_last_evaluated(self, strategy_id: str, price: float):
        """Update last evaluated price and timestamp"""
        try:
            query = """
                UPDATE position_protection_strategies
                SET 
                    last_evaluated_price = $1,
                    last_evaluated_at = NOW()
                WHERE id = $2
            """
            await self.db.execute(query, price, UUID(strategy_id))
        except Exception as e:
            logger.debug(f"Failed to update last_evaluated for {strategy_id}: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PREMIUM MONITORING (Phase 2)
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _check_premium_triggers(self, strategy: Dict, latest_ticks: Dict[int, Dict]):
        """
        Check premium-based triggers for each position.
        
        Phase 2: Per-position monitoring with direction-aware trailing.
        
        Logic:
        - For each position with premium threshold configured
        - Get current LTP from WebSocket
        - Update trailing stoploss based on transaction type (SELL/BUY)
        - Execute exit if stoploss or target triggered
        """
        from .trailing import update_premium_trailing_sell, update_premium_trailing_buy
        import json
        
        strategy_id = str(strategy['id'])
        premium_thresholds = strategy.get('premium_thresholds', {})
        
        if not premium_thresholds:
            logger.debug(f"Strategy {strategy_id}: No premium thresholds configured")
            return
        
        # Track if any config changed (for DB update)
        config_updated = False
        triggered_positions = []
        
        # Check each position's premium threshold
        for token_str, config in premium_thresholds.items():
            instrument_token = int(token_str)
            tradingsymbol = config.get('tradingsymbol', 'Unknown')
            transaction_type = config.get('transaction_type')
            
            # Get current LTP from WebSocket
            if instrument_token not in latest_ticks:
                logger.debug(
                    f"Strategy {strategy_id}: No price data for {tradingsymbol} (token={instrument_token})"
                )
                continue
            
            current_ltp = latest_ticks[instrument_token]['last_price']
            
            # Direction-aware trailing logic
            triggered = False
            updated_config = None
            
            if transaction_type == 'SELL':
                triggered, updated_config = update_premium_trailing_sell(config, current_ltp)
            elif transaction_type == 'BUY':
                triggered, updated_config = update_premium_trailing_buy(config, current_ltp)
            else:
                logger.warning(
                    f"Strategy {strategy_id}: Unknown transaction_type '{transaction_type}' "
                    f"for {tradingsymbol}"
                )
                continue
            
            # Update config if changed
            if updated_config is not None:
                premium_thresholds[token_str] = updated_config
                config_updated = True
            
            # If triggered, add to exit list
            if triggered:
                logger.info(
                    f"Strategy {strategy_id}: Premium trigger for {tradingsymbol} "
                    f"({transaction_type}, LTP={current_ltp:.2f})"
                )
                triggered_positions.append({
                    'instrument_token': instrument_token,
                    'tradingsymbol': tradingsymbol,
                    'current_ltp': current_ltp,
                    'config': config
                })
        
        # Persist config changes to DB
        if config_updated:
            await self._update_premium_thresholds(strategy_id, premium_thresholds)
        
        # Execute exits for triggered positions
        if triggered_positions:
            await self._execute_premium_exit(
                strategy,
                triggered_positions,
                "premium_stoploss_triggered"
            )
    
    async def _check_hybrid_triggers(self, strategy: Dict, latest_ticks: Dict[int, Dict]):
        """
        Check hybrid triggers (both index and premium).
        
        Phase 2: Monitor both index and premium, exit based on exit_logic.
        """
        strategy_id = str(strategy['id'])
        exit_logic = strategy.get('exit_logic', 'any')
        
        # Check index triggers (don't execute, just check)
        index_triggered = await self._is_index_triggered(strategy, latest_ticks)
        
        # Check premium triggers (don't execute, just check)
        premium_triggered = await self._is_premium_triggered(strategy, latest_ticks)
        
        # Apply exit logic
        should_exit = False
        if exit_logic == 'any':
            should_exit = index_triggered or premium_triggered
        elif exit_logic == 'all':
            should_exit = index_triggered and premium_triggered
        
        if should_exit:
            logger.info(
                f"Strategy {strategy_id}: Hybrid trigger "
                f"(index={index_triggered}, premium={premium_triggered}, logic={exit_logic})"
            )
            # Execute exit for all positions
            index_price = latest_ticks.get(strategy.get('index_instrument_token'), {}).get('last_price', 0)
            await self._execute_exit(strategy, "hybrid_trigger", index_price)
    
    async def _is_index_triggered(self, strategy: Dict, latest_ticks: Dict[int, Dict]) -> bool:
        """Check if index stoploss is triggered (without executing)"""
        index_token = strategy.get('index_instrument_token')
        if not index_token or index_token not in latest_ticks:
            return False
        
        current_index_price = latest_ticks[index_token]['last_price']
        upper_sl = strategy.get('index_upper_stoploss')
        lower_sl = strategy.get('index_lower_stoploss')
        
        if upper_sl is not None and current_index_price >= upper_sl:
            return True
        if lower_sl is not None and current_index_price <= lower_sl:
            return True
        
        return False
    
    async def _is_premium_triggered(self, strategy: Dict, latest_ticks: Dict[int, Dict]) -> bool:
        """Check if any premium stoploss is triggered (without executing)"""
        from .trailing import update_premium_trailing_sell, update_premium_trailing_buy
        
        premium_thresholds = strategy.get('premium_thresholds', {})
        if not premium_thresholds:
            return False
        
        for token_str, config in premium_thresholds.items():
            instrument_token = int(token_str)
            transaction_type = config.get('transaction_type')
            
            if instrument_token not in latest_ticks:
                continue
            
            current_ltp = latest_ticks[instrument_token]['last_price']
            
            triggered = False
            if transaction_type == 'SELL':
                triggered, _ = update_premium_trailing_sell(config, current_ltp)
            elif transaction_type == 'BUY':
                triggered, _ = update_premium_trailing_buy(config, current_ltp)
            
            if triggered:
                return True
        
        return False
    
    async def _update_premium_thresholds(self, strategy_id: str, premium_thresholds: Dict):
        """Update premium_thresholds in database (for runtime trailing state)"""
        try:
            import json
            query = """
                UPDATE position_protection_strategies
                SET 
                    premium_thresholds = $1::jsonb,
                    updated_at = NOW()
                WHERE id = $2
            """
            await self.db.execute(
                query,
                json.dumps(premium_thresholds),
                UUID(strategy_id)
            )
        except Exception as e:
            logger.error(f"Failed to update premium thresholds for {strategy_id}: {e}")
    
    async def _execute_premium_exit(
        self,
        strategy: Dict,
        triggered_positions: List[Dict],
        trigger_reason: str
    ):
        """
        Execute exit orders for premium-triggered positions.
        
        Similar to _execute_exit but only for specific positions.
        Phase 2: Per-position exits
        """
        strategy_id = str(strategy['id'])
        position_snapshot = strategy.get('position_snapshot', [])
        
        correlation_id = str(uuid.uuid4())
        
        logger.info(
            f"Strategy {strategy_id}: Executing premium exit for "
            f"{len(triggered_positions)} position(s)"
        )
        
        # Get KiteConnect instance
        kite = await self._get_kite_instance()
        if not kite:
            logger.error("Failed to get KiteConnect instance")
            await self._log_event(
                strategy_id,
                "error",
                error_message="Failed to get KiteConnect instance for premium exit",
                meta={"trigger_reason": trigger_reason}
            )
            return
        
        orders_placed = []
        errors = []
        
        # Place exit order for each triggered position
        for trig_pos in triggered_positions:
            instrument_token = trig_pos['instrument_token']
            tradingsymbol = trig_pos['tradingsymbol']
            current_ltp = trig_pos['current_ltp']
            
            # Find position in snapshot
            position = next(
                (p for p in position_snapshot if p['instrument_token'] == instrument_token),
                None
            )
            
            if not position:
                logger.warning(f"Position {tradingsymbol} not found in snapshot")
                continue
            
            try:
                # Determine opposite transaction type for exit
                exit_transaction = (
                    TransactionType.BUY if position['transaction_type'] == 'SELL'
                    else TransactionType.SELL
                )
                
                # Generate idempotency key
                idempotency_key = f"strategy_{strategy_id[:8]}_{trigger_reason}_{instrument_token}"
                
                # Create order request
                order_req = PlaceOrderRequest(
                    exchange=Exchange.NFO,
                    tradingsymbol=tradingsymbol,
                    transaction_type=exit_transaction,
                    variety=Variety.REGULAR,
                    product=Product[position['product']],
                    order_type=OrderType.MARKET,
                    quantity=position['quantity'],
                    validity=Validity.DAY
                )
                
                # Place order
                order_response = await self.orders_service.place_order(
                    kite=kite,
                    req=order_req,
                    corr_id=correlation_id,
                    idempotency_key=idempotency_key,
                    session_id="system"
                )
                
                order_id = order_response.order_id
                
                orders_placed.append({
                    "order_id": order_id,
                    "instrument_token": instrument_token,
                    "tradingsymbol": tradingsymbol,
                    "quantity": position['quantity'],
                    "transaction_type": exit_transaction.value,
                    "trigger_price": current_ltp,
                    "idempotency_key": idempotency_key,
                    "correlation_id": correlation_id,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                
                logger.info(
                    f"Order placed: {order_id} for {tradingsymbol} "
                    f"({exit_transaction.value} {position['quantity']} @ LTP={current_ltp:.2f})"
                )
                
                # Log order event
                await self._log_event(
                    strategy_id,
                    "order_placed",
                    order_id=order_id,
                    instrument_token=instrument_token,
                    quantity_affected=position['quantity'],
                    trigger_price=current_ltp,
                    trigger_type="premium",
                    meta={
                        "trigger_reason": trigger_reason,
                        "tradingsymbol": tradingsymbol,
                        "correlation_id": correlation_id
                    }
                )
                
            except Exception as e:
                error_msg = f"Failed to place order for {tradingsymbol}: {e}"
                logger.error(error_msg, exc_info=True)
                errors.append({
                    "tradingsymbol": tradingsymbol,
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
        
        # Update strategy status (mark as partial if not all positions exited)
        await self._update_strategy_after_premium_exit(
            strategy_id,
            trigger_reason,
            orders_placed,
            errors
        )
        
        self._stats["triggers"] += 1
        self._stats["orders_placed"] += len(orders_placed)
        
        logger.info(
            f"Strategy {strategy_id}: Premium exit complete "
            f"({len(orders_placed)} orders, {len(errors)} errors)"
        )
    
    async def _update_strategy_after_premium_exit(
        self,
        strategy_id: str,
        trigger_reason: str,
        orders_placed: List[Dict],
        errors: List[Dict]
    ):
        """Update strategy after premium exit (may be partial)"""
        try:
            import json
            
            # Determine new status: 'partial' if some positions remain, 'triggered' if all exited
            # For now, mark as triggered (can enhance later for partial tracking)
            query = """
                UPDATE position_protection_strategies
                SET 
                    status = 'triggered',
                    placed_orders = placed_orders || $1::jsonb,
                    execution_errors = execution_errors || $2::jsonb,
                    last_evaluated_at = NOW(),
                    updated_at = NOW()
                WHERE id = $3
            """
            
            await self.db.execute(
                query,
                json.dumps(orders_placed),
                json.dumps(errors),
                UUID(strategy_id)
            )
            
            # Log trigger event
            await self._log_event(
                strategy_id,
                trigger_reason,
                trigger_type="premium",
                meta={
                    "orders_placed": len(orders_placed),
                    "errors": len(errors)
                }
            )
            
            # Remove from in-memory cache
            if strategy_id in self._strategies:
                del self._strategies[strategy_id]
            
        except Exception as e:
            logger.error(f"Failed to update strategy {strategy_id}: {e}", exc_info=True)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # COMBINED PREMIUM MONITORING (Phase 4)
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _check_combined_premium_triggers(self, strategy: Dict, latest_ticks: Dict[int, Dict]):
        """
        Check combined premium triggers (net P&L across all positions).
        
        Phase 4: Monitor straddles/strangles with net premium tracking.
        
        Logic:
        - Calculate net premium across all positions
        - Check profit targets
        - Apply trailing based on net P&L
        - Also check index bracket stops
        """
        from .trailing import update_combined_premium_trailing
        import json
        
        strategy_id = str(strategy['id'])
        entry_type = strategy.get('combined_premium_entry_type')
        position_snapshot = strategy.get('position_snapshot', [])
        
        if not entry_type or not position_snapshot:
            logger.debug(f"Strategy {strategy_id}: Invalid combined premium config")
            return
        
        # Calculate net premium from all positions
        current_net_premium = 0.0
        for pos in position_snapshot:
            token = pos['instrument_token']
            tick = latest_ticks.get(token)
            if not tick or 'last_price' not in tick:
                logger.debug(
                    f"Strategy {strategy_id}: No price data for {pos['tradingsymbol']} (token={token})"
                )
                # Can't evaluate without all prices
                return
            
            current_ltp = tick['last_price']
            current_net_premium += current_ltp
        
        # Initialize initial_net_premium if first evaluation
        initial_premium = strategy.get('initial_net_premium')
        if initial_premium is None:
            # Calculate from position snapshot average prices
            initial_premium = sum(pos['average_price'] for pos in position_snapshot)
            strategy['initial_net_premium'] = initial_premium
            await self._update_combined_premium_state(strategy_id, {
                'initial_net_premium': initial_premium,
                'current_net_premium': current_net_premium
            })
            logger.info(
                f"Strategy {strategy_id}: Initialized combined premium "
                f"(initial={initial_premium:.2f}, current={current_net_premium:.2f})"
            )
        
        # Calculate net P&L
        if entry_type == 'credit':
            net_pnl = initial_premium - current_net_premium
        else:  # debit
            net_pnl = current_net_premium - initial_premium
        
        # Check if index bracket stops triggered (takes precedence)
        index_triggered = await self._is_index_triggered(strategy, latest_ticks)
        if index_triggered:
            logger.info(
                f"Strategy {strategy_id}: Combined premium - Index bracket triggered, "
                f"exiting all positions (net_pnl={net_pnl:.2f})"
            )
            index_price = latest_ticks.get(strategy.get('index_instrument_token'), {}).get('last_price', 0)
            await self._execute_exit(strategy, "combined_premium_index_bracket", index_price)
            return
        
        # Check profit target (fixed)
        profit_target = strategy.get('combined_premium_profit_target')
        if profit_target and net_pnl >= profit_target:
            logger.info(
                f"Strategy {strategy_id}: Combined premium profit target REACHED "
                f"(P&L={net_pnl:.2f} >= target={profit_target:.2f})"
            )
            await self._execute_exit(strategy, "combined_premium_profit_target", current_net_premium)
            return
        
        # Check partial exit levels
        levels = strategy.get('combined_premium_levels', [])
        for level in levels:
            if level.get('executed', False):
                continue
            
            level_profit = level.get('profit_points')
            if net_pnl >= level_profit:
                logger.info(
                    f"Strategy {strategy_id}: Combined premium level {level['level_number']} triggered "
                    f"(P&L={net_pnl:.2f} >= {level_profit:.2f})"
                )
                # Execute partial exit
                await self._execute_partial_exit(strategy, level, current_net_premium)
                return
        
        # Check trailing stoploss
        trailing_enabled = strategy.get('combined_premium_trailing_enabled', False)
        if trailing_enabled:
            triggered, updated_config = update_combined_premium_trailing(
                config=strategy,
                current_net_premium=current_net_premium,
                entry_type=entry_type
            )
            
            if updated_config:
                # Update state in database
                await self._update_combined_premium_state(strategy_id, updated_config)
            
            if triggered:
                logger.info(
                    f"Strategy {strategy_id}: Combined premium trailing triggered "
                    f"(net_premium={current_net_premium:.2f})"
                )
                await self._execute_exit(strategy, "combined_premium_trailing", current_net_premium)
                return
        
        # Update current premium in DB (periodic)
        if strategy.get('_last_premium_update', 0) % 10 == 0:  # Every 10 cycles (~5s)
            await self._update_combined_premium_state(strategy_id, {
                'current_net_premium': current_net_premium
            })
        strategy['_last_premium_update'] = strategy.get('_last_premium_update', 0) + 1
    
    async def _update_combined_premium_state(self, strategy_id: str, updates: Dict):
        """Update combined premium runtime state in database"""
        try:
            import json
            
            # Build SET clause dynamically
            set_clauses = []
            params = []
            param_count = 1
            
            for key, value in updates.items():
                set_clauses.append(f"{key} = ${param_count}")
                params.append(value)
                param_count += 1
            
            if not set_clauses:
                return
            
            query = f"""
                UPDATE position_protection_strategies
                SET 
                    {', '.join(set_clauses)},
                    updated_at = NOW()
                WHERE id = ${param_count}
            """
            params.append(UUID(strategy_id))
            
            await self.db.execute(query, *params)
        except Exception as e:
            logger.error(f"Failed to update combined premium state for {strategy_id}: {e}")
    
    async def _execute_partial_exit(self, strategy: Dict, level: Dict, trigger_premium: float):
        """
        Execute partial exit for combined premium level.
        
        Phase 4: Exit percentage of positions at profit level.
        """
        strategy_id = str(strategy['id'])
        exit_percent = level.get('exit_percent', 100)
        level_number = level.get('level_number')
        
        logger.info(
            f"Strategy {strategy_id}: Executing {exit_percent}% exit at level {level_number}"
        )
        
        # For now, execute full exit (partial exit requires quantity tracking enhancement)
        # This is a placeholder for future implementation
        await self._execute_exit(
            strategy,
            f"combined_premium_level_{level_number}",
            trigger_premium
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # EVENT LOGGING
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _log_event(
        self,
        strategy_id: str,
        event_type: str,
        trigger_price: Optional[float] = None,
        order_id: Optional[str] = None,
        instrument_token: Optional[int] = None,
        quantity_affected: Optional[int] = None,
        error_message: Optional[str] = None,
        meta: Optional[Dict] = None
    ):
        """Log an event to strategy_events table"""
        try:
            query = """
                INSERT INTO strategy_events (
                    strategy_id, event_type, trigger_price,
                    order_id, instrument_token, quantity_affected,
                    error_message, meta, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            """
            
            import json
            await self.db.execute(
                query,
                UUID(strategy_id),
                event_type,
                trigger_price,
                order_id,
                instrument_token,
                quantity_affected,
                error_message,
                json.dumps(meta) if meta else None
            )
        except Exception as e:
            logger.error(f"Failed to log event: {e}", exc_info=True)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # UTILITIES
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _get_kite_instance(self):
        """Get system KiteConnect instance from app state"""
        try:
            from broker_api.broker_api import get_system_access_token
            from database import SessionLocal
            from kiteconnect import KiteConnect
            from broker_api.kite_auth import API_KEY
            
            db = SessionLocal()
            try:
                system_token = get_system_access_token(db)
                if system_token:
                    kite = KiteConnect(api_key=API_KEY)
                    kite.set_access_token(system_token)
                    return kite
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Failed to get KiteConnect instance: {e}", exc_info=True)
        
        return None
    
    async def _update_engine_subscriptions(self, desired_tokens: Set[int]):
        """Update engine subscriptions based on required tokens (only for tokens not used by clients)"""
        # Filter out tokens that clients are already subscribed to
        client_map: Dict[int, int] = getattr(self.ws_manager, "token_refcount", {}) or {}
        desired = {t for t in desired_tokens if int(client_map.get(t, 0) or 0) == 0}
        
        # Determine deltas
        add = sorted(list(desired - self._engine_subscribed_tokens))
        remove = sorted(list(self._engine_subscribed_tokens - desired))
        
        if add:
            await self._engine_subscribe_safe(add)
            self._engine_subscribed_tokens.update(add)
        
        if remove:
            await self._engine_unsubscribe_safe(remove)
            for t in remove:
                self._engine_subscribed_tokens.discard(t)
    
    async def _engine_subscribe_safe(self, tokens: List[int]):
        """Subscribe to tokens at KiteTicker level (backend subscription)"""
        if not tokens:
            return
        try:
            if self.ws_manager and getattr(self.ws_manager, "kws", None) and self.ws_manager.kws.is_connected():
                # Subscribe
                try:
                    self.ws_manager.kws.subscribe(tokens)
                    logger.info(f"[PROTECTION-ENGINE] subscribe tokens={tokens}")
                except Exception as e:
                    logger.error(f"[PROTECTION-ENGINE] subscribe failed: {e}")
                
                # Set mode to ltp ONLY for tokens not already tracked with an aggregate mode
                token_mode_agg: Dict[int, str] = getattr(self.ws_manager, "token_mode_agg", {}) or {}
                set_mode_tokens = [t for t in tokens if t not in token_mode_agg]
                if set_mode_tokens:
                    try:
                        self.ws_manager.kws.set_mode("ltp", set_mode_tokens)
                        logger.info(f"[PROTECTION-ENGINE] set_mode ltp tokens={set_mode_tokens}")
                    except Exception as e:
                        logger.error(f"[PROTECTION-ENGINE] set_mode(ltp) failed: {e}")
            else:
                logger.debug("[PROTECTION-ENGINE] skip subscribe; WS not connected")
        except Exception as e:
            logger.error(f"[PROTECTION-ENGINE] subscribe unexpected error: {e}", exc_info=True)
    
    async def _engine_unsubscribe_safe(self, tokens: List[int]):
        """Unsubscribe from tokens at KiteTicker level (backend unsubscription)"""
        if not tokens:
            return
        try:
            if self.ws_manager and getattr(self.ws_manager, "kws", None) and self.ws_manager.kws.is_connected():
                # Double-check no clients currently hold the token
                client_map: Dict[int, int] = getattr(self.ws_manager, "token_refcount", {}) or {}
                safe_tokens = [t for t in tokens if int(client_map.get(t, 0) or 0) == 0]
                if not safe_tokens:
                    return
                try:
                    self.ws_manager.kws.unsubscribe(safe_tokens)
                    logger.info(f"[PROTECTION-ENGINE] unsubscribe tokens={safe_tokens}")
                except Exception as e:
                    logger.error(f"[PROTECTION-ENGINE] unsubscribe failed: {e}")
        except Exception as e:
            logger.error(f"[PROTECTION-ENGINE] unsubscribe unexpected error: {e}", exc_info=True)
    
    async def _engine_unsubscribe_all_safe(self):
        """Unsubscribe from all engine-owned tokens"""
        if not self._engine_subscribed_tokens:
            return
        await self._engine_unsubscribe_safe(sorted(list(self._engine_subscribed_tokens)))
        self._engine_subscribed_tokens.clear()
    
    async def _handle_ws_reconnect(self):
        """Re-assert engine subscriptions on WebSocket reconnect"""
        try:
            current_status = self.ws_manager.get_websocket_status()
            if current_status == "CONNECTED" and self._last_ws_status != "CONNECTED":
                # WS just reconnected - re-subscribe to engine tokens
                if self._engine_subscribed_tokens:
                    logger.info(f"[PROTECTION-ENGINE] WS reconnected, re-subscribing to {len(self._engine_subscribed_tokens)} tokens")
                    await self._engine_subscribe_safe(sorted(list(self._engine_subscribed_tokens)))
            self._last_ws_status = current_status
        except Exception as e:
            logger.error(f"[PROTECTION-ENGINE] handle_ws_reconnect error: {e}", exc_info=True)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics"""
        return {
            "running": self._running,
            "active_strategies": len(self._strategies),
            "engine_subscribed_tokens": len(self._engine_subscribed_tokens),
            "evaluations": self._stats["evaluations"],
            "triggers": self._stats["triggers"],
            "orders_placed": self._stats["orders_placed"],
            "errors": self._stats["errors"],
            "interval_ms": self.interval_ms
        }
