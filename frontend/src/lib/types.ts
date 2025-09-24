export type Instrument = {
	instrument_token: number;
	id: string;
	name: string;
	qty: number;
	change?: number;
	percentChange: number;
	price: number;
	last_price?: number;
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
