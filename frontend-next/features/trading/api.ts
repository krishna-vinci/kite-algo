import type { RuntimeStatus } from "@/components/options/types";
import type {
  StrategyCapabilities,
  TradingBrokerPosition,
  TradingBrokerSnapshot,
  StrategyRiskControls,
  TradingPaperSummary,
  TradingStrategyGroup,
  TradingTimelineItem,
} from "@/features/trading/types";
import { apiFetch } from "@/lib/api/client";

type SessionStatusResponse = {
  app?: { authenticated?: boolean };
  broker?: {
    connected?: boolean;
    status?: string;
    mode?: "system";
    last_login?: {
      last_success_at?: string | null;
      last_failure_at?: string | null;
      last_error?: string | null;
    };
    scheduler?: { next_run?: string | null };
  };
  runtime?: {
    websocket?: { status?: string };
    paper_runtime?: { available?: boolean };
  };
};

type PaperStrategySummaryResponse = {
  account?: {
    account_scope?: string;
    currency?: string;
    starting_balance?: number;
    available_funds?: number;
    blocked_funds?: number;
    realized_pnl?: number;
    unrealized_pnl?: number;
    open_position_count?: number;
  };
  strategies?: Array<Record<string, unknown>>;
};

export type BrokerPositionsResponse = {
  position_count?: number;
  positions?: Record<
    string,
    {
      position_key?: string;
      tradingsymbol?: string;
      exchange?: string;
      product?: string;
      quantity?: number;
      average_price?: number;
      last_price?: number;
      pnl?: number;
      realized_pnl?: number;
      unrealized_pnl?: number;
    }
  >;
};

function normalizeRiskControls(raw: unknown): StrategyRiskControls {
  const value = (raw ?? {}) as Record<string, unknown>;
  return {
    indexLowerBoundary: typeof value.index_lower_boundary === "number" ? value.index_lower_boundary : null,
    indexUpperBoundary: typeof value.index_upper_boundary === "number" ? value.index_upper_boundary : null,
    combinedPremiumTarget: typeof value.combined_premium_target === "number" ? value.combined_premium_target : null,
    combinedPremiumStoploss: typeof value.combined_premium_stoploss === "number" ? value.combined_premium_stoploss : null,
    basketMtmTarget: typeof value.basket_mtm_target === "number" ? value.basket_mtm_target : null,
    basketMtmStoploss: typeof value.basket_mtm_stoploss === "number" ? value.basket_mtm_stoploss : null,
  };
}

function normalizeCapabilities(raw: unknown): StrategyCapabilities {
  const value = (raw ?? {}) as Record<string, unknown>;
  return {
    canEditRisk: Boolean(value.can_edit_risk),
    editRiskReason: typeof value.edit_risk_reason === "string" ? value.edit_risk_reason : null,
  };
}

function normalizeTimeline(items: unknown): TradingTimelineItem[] {
  if (!Array.isArray(items)) {
    return [];
  }
  return items.map((item) => {
    const value = (item ?? {}) as Record<string, unknown>;
    return {
      kind: typeof value.kind === "string" ? value.kind : "unknown",
      timestamp: typeof value.timestamp === "string" ? value.timestamp : null,
      label: typeof value.label === "string" ? value.label : "event",
    };
  });
}

export function normalizeRuntimeStatus(response: SessionStatusResponse): RuntimeStatus {
  return {
    brokerConnected: Boolean(response?.broker?.connected),
    brokerStatus: String(response?.broker?.status ?? "unknown") as RuntimeStatus["brokerStatus"],
    brokerMode: response?.broker?.mode ?? "system",
    brokerLastSuccessAt: response?.broker?.last_login?.last_success_at ?? null,
    brokerLastFailureAt: response?.broker?.last_login?.last_failure_at ?? null,
    brokerLastError: response?.broker?.last_login?.last_error ?? null,
    brokerNextRefreshAt: response?.broker?.scheduler?.next_run ?? null,
    websocketStatus: String(response?.runtime?.websocket?.status ?? "unknown"),
    paperAvailable: Boolean(response?.runtime?.paper_runtime?.available),
    appAuthenticated: Boolean(response?.app?.authenticated),
  };
}

function normalizeStrategy(item: Record<string, unknown>): TradingStrategyGroup {
  return {
    strategyId: String(item.strategy_id ?? "unknown"),
    displayName: String(item.display_name ?? item.strategy_id ?? "Unnamed strategy"),
    mode: (item.mode === "live" || item.mode === "dry_run" ? item.mode : "paper") as TradingStrategyGroup["mode"],
    status: String(item.status ?? "unknown"),
    isOpen: Boolean(item.is_open ?? (typeof item.open_leg_count === "number" && item.open_leg_count > 0)),
    openLegCount: typeof item.open_leg_count === "number" ? item.open_leg_count : 0,
    realizedPnl: typeof item.realized_pnl === "number" ? item.realized_pnl : 0,
    unrealizedPnl: typeof item.unrealized_pnl === "number" ? item.unrealized_pnl : 0,
    riskControls: normalizeRiskControls(item.risk_controls),
    capabilities: normalizeCapabilities(item.capabilities),
    positions: Array.isArray(item.positions) ? item.positions : [],
    orders: Array.isArray(item.orders) ? item.orders : [],
    trades: Array.isArray(item.trades) ? item.trades : [],
    timeline: normalizeTimeline(item.timeline),
  };
}

export function normalizePaperStrategySummary(response: PaperStrategySummaryResponse): TradingPaperSummary {
  const strategies = (response?.strategies ?? []).map((item) => normalizeStrategy(item));
  return {
    accountScope: response?.account?.account_scope ?? "default",
    activeStrategyCount: strategies.filter((item) => item.isOpen).length,
    strategies,
  };
}

export async function fetchTradingRuntimeStatus(): Promise<RuntimeStatus> {
  const response = await apiFetch<SessionStatusResponse>("/api/auth/session-status");
  return normalizeRuntimeStatus(response);
}

export async function fetchPaperStrategySummary(accountScope: string): Promise<TradingPaperSummary> {
  const response = await apiFetch<PaperStrategySummaryResponse>(`/api/system/paper/strategies?account_scope=${encodeURIComponent(accountScope)}`);
  return normalizePaperStrategySummary(response);
}

export function normalizeBrokerPositions(response: BrokerPositionsResponse): TradingBrokerSnapshot {
  const positions: TradingBrokerPosition[] = Object.entries(response.positions ?? {}).map(([key, value]) => ({
    positionKey: value.position_key ?? key,
    tradingSymbol: value.tradingsymbol ?? key,
    exchange: value.exchange ?? "NFO",
    product: value.product ?? "MIS",
    quantity: value.quantity ?? 0,
    averagePrice: value.average_price ?? 0,
    lastPrice: value.last_price ?? 0,
    pnl: value.pnl ?? 0,
    realizedPnl: value.realized_pnl ?? 0,
    unrealizedPnl: value.unrealized_pnl ?? 0,
  }));

  return {
    positions,
    activeCount: positions.filter((item) => item.quantity !== 0).length,
  };
}

export async function fetchBrokerPositions(): Promise<TradingBrokerSnapshot> {
  const response = await apiFetch<BrokerPositionsResponse>("/api/positions/realtime");
  return normalizeBrokerPositions(response);
}

export async function updatePaperStrategyRisk(
  strategyId: string,
  payload: Partial<{
    combined_premium_target: number | null;
    combined_premium_stoploss: number | null;
    basket_mtm_target: number | null;
    basket_mtm_stoploss: number | null;
    index_lower_boundary: number | null;
    index_upper_boundary: number | null;
  }>,
  accountScope = "default",
) {
  return apiFetch(`/api/system/paper/strategies/${encodeURIComponent(strategyId)}/risk?account_scope=${encodeURIComponent(accountScope)}`, {
    method: "PATCH",
    json: payload,
  });
}
