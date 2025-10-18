/**
 * TypeScript types for Position Protection System
 * Phase 1: Core types matching backend API models
 */

// ═══════════════════════════════════════════════════════════════════════════════
// ENUMS
// ═══════════════════════════════════════════════════════════════════════════════

export type MonitoringMode = 'index' | 'premium' | 'hybrid' | 'combined_premium';

export type StrategyType = 'manual' | 'straddle' | 'strangle' | 'iron_condor' | 'single_leg';

export type StrategyStatus = 'active' | 'paused' | 'completed' | 'triggered' | 'error' | 'partial';

export type TrailingMode = 'none' | 'continuous' | 'step' | 'atr';

export type OrderType = 'MARKET' | 'LIMIT' | 'SL-M';

export type ExitLogic = 'any' | 'all';

export type CombinedPremiumEntryType = 'credit' | 'debit';

// ═══════════════════════════════════════════════════════════════════════════════
// POSITION MODELS
// ═══════════════════════════════════════════════════════════════════════════════

export interface PositionSnapshot {
	instrument_token: number;
	tradingsymbol: string;
	exchange: string;
	product: string;
	transaction_type: 'BUY' | 'SELL';
	quantity: number;
	lot_size: number;
	lots: number;
	average_price: number;
	current_ltp?: number;
}

export interface PositionFilter {
	exchange?: string;
	product?: string;
	tradingsymbols?: string[];
	instrument_tokens?: number[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// PREMIUM MONITORING MODELS
// ═══════════════════════════════════════════════════════════════════════════════

export interface PremiumMonitoringState {
	instrument_token: number;
	tradingsymbol: string;
	transaction_type: 'BUY' | 'SELL';
	entry_price: number;
	current_ltp?: number;
	current_pnl?: number;
	stoploss_price?: number;
	target_price?: number;
	trailing_activated: boolean;
	current_trailing_sl?: number;
	distance_to_sl?: number;
	distance_to_target?: number;
}

export interface CombinedPremiumLevel {
	level_number: number;
	profit_points: number;
	exit_percent: number;
	executed: boolean;
	execution_time?: string;
}

export interface CombinedPremiumState {
	entry_type: string;
	initial_net_premium: number;
	current_net_premium: number;
	net_pnl: number;
	net_pnl_rupees: number;
	best_net_premium?: number;
	profit_target?: number;
	trailing_enabled: boolean;
	trailing_sl?: number;
	distance_to_sl?: number;
	distance_to_target?: number;
	levels: CombinedPremiumLevel[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// STRATEGY RESPONSE MODELS
// ═══════════════════════════════════════════════════════════════════════════════

export interface ProtectionStrategyResponse {
	strategy_id: string;
	name?: string;
	strategy_type: string;
	monitoring_mode: string;
	status: string;
	
	// Index config
	index_instrument_token?: number;
	index_tradingsymbol?: string;
	index_upper_stoploss?: number;
	index_lower_stoploss?: number;
	
	// Trailing state
	trailing_mode?: string;
	trailing_distance?: number;
	trailing_activated: boolean;
	trailing_current_level?: number;
	
	// Premium monitoring
	premium_monitoring?: Record<string, PremiumMonitoringState>;
	
	// Combined premium
	combined_premium_state?: CombinedPremiumState;
	
	// Position snapshot
	positions_captured: number;
	total_lots: number;
	position_snapshot: PositionSnapshot[];
	
	// Runtime tracking
	remaining_quantities: Record<string, any>;
	placed_orders: any[];
	levels_executed: string[];
	stoploss_executed: boolean;
	
	// Evaluation state
	last_evaluated_price?: number;
	last_evaluated_at?: string;
	
	// Timestamps
	created_at: string;
	updated_at: string;
}

export interface StrategyListItem {
	strategy_id: string;
	name?: string;
	monitoring_mode: string;
	status: string;
	total_lots: number;
	index_instrument_token?: number;
	index_tradingsymbol?: string;
	index_upper_stoploss?: number;
	index_lower_stoploss?: number;
	last_evaluated_at?: string;
	created_at: string;
}

export interface StrategyListResponse {
	total: number;
	strategies: StrategyListItem[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// EVENT MODELS
// ═══════════════════════════════════════════════════════════════════════════════

export interface StrategyEvent {
	event_id: number;
	strategy_id: string;
	event_type: string;
	trigger_price?: number;
	trigger_type?: string;
	level_name?: string;
	quantity_affected?: number;
	lots_affected?: number;
	order_id?: string;
	correlation_id?: string;
	order_status?: string;
	instrument_token?: number;
	error_message?: string;
	meta?: Record<string, any>;
	created_at: string;
}

export interface EventsResponse {
	strategy_id: string;
	total_events: number;
	events: StrategyEvent[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// HEALTH CHECK MODELS
// ═══════════════════════════════════════════════════════════════════════════════

export interface EngineHealthResponse {
	status: string;
	engine_running: boolean;
	active_strategies: number;
	monitoring_modes: Record<string, number>;
	last_evaluation?: string;
	websocket_status: string;
	evaluation_interval_ms: number;
	orders_placed_today: number;
}

// ═══════════════════════════════════════════════════════════════════════════════
// REAL-TIME POSITION MODELS
// ═══════════════════════════════════════════════════════════════════════════════

export interface RealtimePosition {
	tradingsymbol: string;
	exchange: string;
	product: string;
	quantity: number;
	average_price: number;
	last_price: number;
	pnl: number;
	day_buy_quantity: number;
	day_sell_quantity: number;
	instrument_token: number;
	transaction_type?: 'BUY' | 'SELL';
}

export interface RealtimePositionsResponse {
	net: RealtimePosition[];
	day: RealtimePosition[];
}

// ═══════════════════════════════════════════════════════════════════════════════
// PHASE 3: POSITION BUILDER & OPTION CHAIN MODELS
// ═══════════════════════════════════════════════════════════════════════════════

export interface OptionGreeks {
	delta: number;
	gamma: number;
	theta: number;
	vega: number;
	iv: number; // Implied Volatility
}

export interface OptionSide {
	instrument_token: number;
	tradingsymbol: string;
	ltp: number;
	lot_size: number;
	greeks: OptionGreeks;
}

export interface OptionChainStrike {
	strike: number;
	ce: OptionSide | null;
	pe: OptionSide | null;
	is_atm: boolean;
}

export interface MiniChainResponse {
	underlying: string;
	expiry: string;
	spot_price: number;
	atm_strike: number;
	strikes: OptionChainStrike[];
	timestamp: string;
}

export interface StrikeSuggestion {
	strategy_type: string;
	strikes: {
		ce?: {
			strike: number;
			tradingsymbol: string;
			instrument_token: number;
			delta: number;
			ltp: number;
			lot_size: number;
		};
		pe?: {
			strike: number;
			tradingsymbol: string;
			instrument_token: number;
			delta: number;
			ltp: number;
			lot_size: number;
		};
	};
	suggested_lots: number;
	total_margin_required?: number;
	max_loss?: number;
	max_profit?: number;
	notes: string;
}

export interface SelectedStrike {
	instrument_token: number;
	tradingsymbol: string;
	strike: number;
	option_type: 'CE' | 'PE';
	ltp: number;
	lot_size: number;
	delta: number;
	lots: number; // User-selected lots
	transaction_type: 'BUY' | 'SELL';
}

export interface PositionBuildOrder {
	tradingsymbol: string;
	instrument_token: number;
	transaction_type: 'BUY' | 'SELL';
	quantity: number;
	lot_size: number;
	lots: number;
	estimated_price: number;
}

export interface PositionBuildPlan {
	strategy_type: string;
	underlying: string;
	expiry: string;
	orders: PositionBuildOrder[];
	total_lots: number;
	estimated_cost: number;
	estimated_margin: number;
	max_profit?: number;
	max_loss?: number;
	protection_config?: any;
}

export interface BuildPositionRequest {
	underlying: string;
	expiry: string;
	strategy_type: string;
	selected_strikes?: SelectedStrike[];  // Manual strike selection
	target_delta?: number;  // Auto-selection based on delta
	risk_amount?: number;
	protection_config?: any;
	place_orders: boolean;
}

export interface BuildPositionResponse {
	mode: 'dry_run' | 'execution';
	status?: 'success' | 'failed' | 'partial';
	plan?: PositionBuildPlan;
	orders_placed?: any[];
	orders_failed?: any[];
	strategy_id?: string;
	message: string;
}
