/**
 * Types for the market-data option-chain endpoints (`/api/options/*`,
 * `backend/options/api/market_router.py` → `OptionsMarketService`).
 */

export type OptionUnderlying = "NIFTY" | "BANKNIFTY" | "FINNIFTY" | "SENSEX";

export const OPTION_UNDERLYINGS: OptionUnderlying[] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"];

export type OptionContractView = {
  token: number | string | null;
  tsym: string | null;
  lot_size: number | null;
  ltp: number | null;
  iv: number | null;
  oi: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  rho: number | null;
  updated_at: string | null;
};

export type OptionChainRow = {
  strike: number | null;
  ce: OptionContractView | null;
  pe: OptionContractView | null;
};

export type OptionSession = {
  underlying: string;
  expiries: string[];
  spot_ltp: number | null;
  updated_at: string | null;
  resource_error?: unknown;
};

export type OptionExpiries = {
  underlying: string;
  expiries: string[];
  spot_ltp: number | null;
  updated_at: string | null;
};

export type OptionChain = {
  underlying: string;
  expiry: string;
  spot_ltp: number | null;
  atm_strike: number | null;
  strikes: number[];
  chain: OptionChainRow[];
  updated_at: string | null;
  resource_error?: unknown;
};

export type OptionAnalyticValue = {
  underlying: string;
  expiry: string;
  value: number | null;
  updated_at: string | null;
};

export type StartOptionSessionsPayload = {
  replace?: boolean;
  items: Array<{ underlying: string; window?: number; cadence_sec?: number }>;
};

export type StartOptionSessionsResponse = {
  status: string;
  watchlist: string[];
};
