import { apiFetch } from "@/lib/api/client";

export type PaperAccountSummary = {
  account_scope: string;
  currency: string;
  starting_balance: number;
  available_funds: number;
  blocked_funds: number;
  realized_pnl: number;
  unrealized_pnl: number;
  open_position_count: number;
};

export type PaperStrategyPosition = {
  instrument_token: number;
  tradingsymbol: string | null;
  product: string;
  exchange: string;
  net_quantity: number;
  average_price: number;
  last_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
  side: "LONG" | "SHORT" | "FLAT";
  metadata: Record<string, unknown>;
};

export type PaperStrategyOrder = {
  order_id: string;
  tradingsymbol: string | null;
  transaction_type: string;
  quantity: number;
  status: string;
  average_price: number | null;
  placed_at: string | null;
  metadata: Record<string, unknown>;
};

export type PaperStrategyTrade = {
  trade_id: string;
  order_id: string;
  instrument_token: number;
  transaction_type: string;
  quantity: number;
  price: number;
  trade_timestamp: string | null;
  metadata: Record<string, unknown>;
};

export type PaperStrategyGroup = {
  strategy_id: string;
  display_name: string;
  strategy_tag: string | null;
  algo_instance_id: string | null;
  status: string;
  leg_count: number;
  open_leg_count: number;
  net_quantity: number;
  unrealized_pnl: number;
  realized_pnl: number;
  margin_in_use: number;
  last_updated_at: string | null;
  positions: PaperStrategyPosition[];
  orders: PaperStrategyOrder[];
  trades: PaperStrategyTrade[];
};

export type PaperStrategySummaryResponse = {
  account: PaperAccountSummary;
  strategies: PaperStrategyGroup[];
};

export async function fetchPaperStrategies(accountScope: string): Promise<PaperStrategySummaryResponse> {
  return apiFetch(`/api/system/paper/strategies?account_scope=${encodeURIComponent(accountScope)}`);
}

export async function exitPaperStrategy(accountScope: string, strategyId: string): Promise<{ status: string; strategy_id: string; results?: unknown[]; message?: string }> {
  return apiFetch(`/api/system/paper/accounts/${encodeURIComponent(accountScope)}/exit-strategy`, {
    method: "POST",
    json: { strategy_id: strategyId },
  });
}
