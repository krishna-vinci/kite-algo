export type Instrument = {
	instrument_token: number;
	id: string;
	name: string;
	qty: number;
	change?: number;
	percentChange: number;
	price: number;
	last_price?: number;
	tradingsymbol?: string;
};

export type NiftyInstrument = {
	instrument_token: number;
	tradingsymbol: string;
	company_name: string;
	sector: string;
	source_list: string;
	// OHLC baseline data
	open: number | null;
	high: number | null;
	low: number | null;
	close: number | null; // Previous day's close (baseline reference)
	ltp: number | null; // Last traded price at baseline capture
	net_change: number | null; // Absolute change (ltp - close)
	net_change_percent: number | null; // Percentage change
	// Index metrics
	return_attribution: number | null;
	index_weight: number | null;
	freefloat_marketcap: number | null;
	last_updated: string;
	// Live overlay fields (computed in frontend)
	ltp_live?: number;
	change_percent_live?: number;
	ff_mc_live?: number;
	attribution_pp?: number;
	weight_live?: number;
};

export type SnapshotEntry = {
	last_price?: number;
	change_percent?: number;
	tick_timestamp?: number;
};

export type Sectors = {
	[sector: string]: NiftyInstrument[];
};

export type Group = { id: string; name: string; instruments: Instrument[] };
export type WatchlistData = { groups: Group[]; activeGroupIndex: number };

/**
 * Alerts types (consolidated)
 */
export type Comparator = 'gt' | 'lt';
export type TargetType = 'absolute' | 'percent';

export interface Alert {
  id: string;
  instrument_token: number;
  comparator: Comparator;
  target_type: TargetType;
  absolute_target: number | null;
  percent: number | null;
  baseline_price: number | null;
  one_time: boolean;
  name: string | null;
  notes: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  triggered_at: string | null;
  tradingsymbol?: string;
}

export interface AlertCreateRequest {
  instrument_token: number;
  comparator: Comparator;
  target_type: TargetType;
  absolute_target?: number;
  percent?: number;
  baseline_price?: number;
  one_time?: boolean;
  name?: string;
  notes?: string;
}

export type AlertPatchRequest = Partial<Pick<
  Alert,
  'comparator' | 'target_type' | 'absolute_target' | 'percent' | 'name' | 'notes' | 'one_time'
>>;

export interface ListAlertsResponse {
  items: Alert[];
  total: number;
  limit: number;
  offset: number;
}

// Alerts UI: Instrument picker row type (shared)
export type InstrumentRow = {
  instrument_token: number;
  tradingsymbol: string;
  name?: string;
  exchange: string;
  instrument_type?: string;
  segment?: string;
  underlying?: string;
  option_type?: 'CE' | 'PE';
  expiry?: string;
  strike?: number;
};


// ========== Options API types ==========

/**
 * Single session request item.
 * - window: number of strikes on each side of ATM (total rows ~ 2k+1). Default 12.
 * - cadence_sec: server compute cadence in seconds. Default 5.
 */
export interface SessionRequestItem {
  underlying: string;
  window?: number;
  cadence_sec?: number;
}

/**
 * Batch sessions request body for POST /options/sessions
 * - replace: when true, stop sessions not in this list before starting new ones.
 */
export interface SessionsRequest {
  items: SessionRequestItem[];
  replace?: boolean;
}

/**
 * Watchlist item returned by POST /options/sessions
 */
export interface WatchlistItem {
  underlying: string;
  is_running: boolean;
  desired_tokens: number;
}

/**
 * Option Greeks semantics:
 * - theta is per calendar day
 * - vega is per 1% volatility change
 */
export interface OptionGreeks {
  delta: number | null;
  gamma: number | null;
  theta: number | null; // per calendar day
  vega: number | null;  // per 1% vol
  rho: number | null;
}

/**
 * Per-instrument data for a CE/PE leg in the option chain.
 */
export interface OptionInstrumentData extends OptionGreeks {
  token: number;
  tsym: string;
  ltp: number | null;
  iv: number | null;
  oi: number | null;
  updated_at: string | null;     // ISO timestamp or null
  stale_age_sec: number | null;  // seconds since exchange_timestamp, if available
}

/**
 * One row in the option chain for a given strike.
 */
export interface OptionChainRow {
  strike: number;
  CE: OptionInstrumentData | null;
  PE: OptionInstrumentData | null;
}

/**
 * Per-expiry aggregation and rows.
 */
export interface PerExpiryData {
  forward: number | null;
  sigma_expiry: number | null;
  atm_strike: number | null;
  strikes: number[];
  rows: OptionChainRow[];
}

/**
 * Snapshot returned by:
 * - GET /options/session/{underlying}
 * - GET /options/chain/{underlying_symbol} (alias)
 * And streamed on WS /ws/options/session/{underlying}.
 */
export interface OptionsSessionSnapshot {
  underlying: string;
  spot_token: number;
  spot_ltp: number | null;
  cadence_sec: number;
  expiries: string[]; // ISO date YYYY-MM-DD
  per_expiry: Record<string, PerExpiryData>;
  desired_token_count: number;
  updated_at: string; // ISO timestamp
}

/**
 * Response shape for DELETE /options/session/{underlying}
 */
export interface StopSessionResponse {
  status: 'stopped';
  underlying: string;
}

export interface ErrorResponse {
  code: string;
  message: string;
}
