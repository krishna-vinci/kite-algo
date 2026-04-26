import type { RuntimeStatus } from "@/components/options/types";
import type {
  ControlPlaneSnapshot,
  TradingPaperAccount,
  StrategyCapabilities,
  StrategySummaryField,
  TradingBrokerPosition,
  TradingBrokerSnapshot,
  StrategyRiskField,
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

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function recordValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function normalizeSummaryFields(raw: unknown): StrategySummaryField[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    .map((item) => ({
      key: typeof item.key === "string" ? item.key : "unknown",
      label: typeof item.label === "string" ? item.label : String(item.key ?? "Unknown field"),
      value:
        typeof item.value === "number" || typeof item.value === "string" || item.value == null
          ? (item.value as number | string | null)
          : null,
      unit: typeof item.unit === "string" ? item.unit : null,
      group: typeof item.group === "string" ? item.group : null,
    }));
}

function normalizeCapabilities(raw: unknown): StrategyCapabilities {
  const value = (raw ?? {}) as Record<string, unknown>;
  return {
    canEditRisk: Boolean(value.can_edit_risk),
    editRiskReason: typeof value.edit_risk_reason === "string" ? value.edit_risk_reason : null,
    canExitStrategy: Boolean(value.can_exit_strategy),
    exitReason: typeof value.exit_reason === "string" ? value.exit_reason : null,
    allowedActions: Array.isArray(value.allowed_actions) ? value.allowed_actions.filter((item): item is string => typeof item === "string") : [],
    riskSchema: Array.isArray(value.risk_schema)
      ? value.risk_schema
          .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
          .map(
            (item): StrategyRiskField => ({
              key: typeof item.key === "string" ? item.key : "unknown",
              label: typeof item.label === "string" ? item.label : String(item.key ?? "Unknown field"),
              type: typeof item.type === "string" ? item.type : "number",
              unit: typeof item.unit === "string" ? item.unit : null,
              group: typeof item.group === "string" ? item.group : null,
              required: Boolean(item.required),
              recommended: Boolean(item.recommended),
              value:
                typeof item.value === "number" || typeof item.value === "string" || item.value == null
                  ? (item.value as number | string | null)
                  : null,
            }),
          )
      : [],
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
  const strategyRunId = String(item.strategy_run_id ?? item.strategy_id ?? "unknown");
  return {
    strategyRunId,
    strategyId: strategyRunId,
    displayName: String(item.display_name ?? item.strategy_id ?? "Unnamed strategy"),
    strategyTag: typeof item.strategy_tag === "string" ? item.strategy_tag : null,
    algoInstanceId: typeof item.algo_instance_id === "string" ? item.algo_instance_id : null,
    mode: (item.mode === "live" || item.mode === "dry_run" ? item.mode : "paper") as TradingStrategyGroup["mode"],
    status: String(item.status ?? "unknown"),
    isOpen: Boolean(item.is_open ?? (typeof item.open_leg_count === "number" && item.open_leg_count > 0)),
    openLegCount: typeof item.open_leg_count === "number" ? item.open_leg_count : 0,
    netQuantity: typeof item.net_quantity === "number" ? item.net_quantity : 0,
    realizedPnl: typeof item.realized_pnl === "number" ? item.realized_pnl : 0,
    unrealizedPnl: typeof item.unrealized_pnl === "number" ? item.unrealized_pnl : 0,
    marginInUse: typeof item.margin_in_use === "number" ? item.margin_in_use : 0,
    lastUpdatedAt: typeof item.last_updated_at === "string" ? item.last_updated_at : null,
    summaryFields: normalizeSummaryFields(item.summary_fields),
    capabilities: normalizeCapabilities(item.capabilities),
    positions: Array.isArray(item.positions) ? item.positions : [],
    orders: Array.isArray(item.orders) ? item.orders : [],
    trades: Array.isArray(item.trades) ? item.trades : [],
    timeline: normalizeTimeline(item.timeline),
  };
}

function normalizePaperAccount(response: PaperStrategySummaryResponse): TradingPaperAccount {
  return {
    accountScope: response?.account?.account_scope ?? "default",
    currency: response?.account?.currency ?? "INR",
    startingBalance: response?.account?.starting_balance ?? 0,
    availableFunds: response?.account?.available_funds ?? 0,
    blockedFunds: response?.account?.blocked_funds ?? 0,
    realizedPnl: response?.account?.realized_pnl ?? 0,
    unrealizedPnl: response?.account?.unrealized_pnl ?? 0,
    openPositionCount: response?.account?.open_position_count ?? 0,
  };
}

export function normalizePaperStrategySummary(response: PaperStrategySummaryResponse): TradingPaperSummary {
  const strategies = (response?.strategies ?? []).map((item) => normalizeStrategy(item));
  const account = normalizePaperAccount(response);
  return {
    accountScope: account.accountScope,
    account,
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

export function normalizeControlPlaneSnapshot(response: Record<string, unknown>): ControlPlaneSnapshot {
  const totals = recordValue(response.totals);
  const unattributed = recordValue(response.unattributed);
  const strategies = Array.isArray(response.strategies) ? response.strategies : [];

  return {
    generatedAt: stringOrNull(response.generated_at),
    totals: {
      strategyCount: numberValue(totals.strategy_count),
      openStrategyCount: numberValue(totals.open_strategy_count),
      positionCount: numberValue(totals.position_count),
      staleWorkerCount: numberValue(totals.stale_worker_count),
      realizedPnl: numberValue(totals.realized_pnl),
      unrealizedPnl: numberValue(totals.unrealized_pnl),
      netPnl: numberValue(totals.net_pnl),
    },
    strategies: strategies
      .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
      .map((item) => {
        const protection = recordValue(item.protection);
        return {
          strategyRunId: String(item.strategy_run_id ?? "unknown"),
          displayName: String(item.display_name ?? item.strategy_run_id ?? "Unnamed strategy"),
          source: String(item.source ?? "algo_worker") as ControlPlaneSnapshot["strategies"][number]["source"],
          mode: (item.mode === "live" || item.mode === "dry_run" ? item.mode : "paper") as ControlPlaneSnapshot["strategies"][number]["mode"],
          status: String(item.status ?? "unknown"),
          healthStatus: String(item.health_status ?? "unknown") as ControlPlaneSnapshot["strategies"][number]["healthStatus"],
          heartbeatAgeSec: typeof item.heartbeat_age_sec === "number" ? item.heartbeat_age_sec : null,
          workerId: stringOrNull(item.worker_id),
          workerName: stringOrNull(item.worker_name),
          workerMetrics: recordValue(item.worker_metrics),
          isOpen: Boolean(item.is_open),
          realizedPnl: numberValue(item.realized_pnl),
          unrealizedPnl: numberValue(item.unrealized_pnl),
          netPnl: numberValue(item.net_pnl),
          positionCount: numberValue(item.position_count),
          openOrderCount: numberValue(item.open_order_count),
          tradeCount: numberValue(item.trade_count),
          positions: Array.isArray(item.positions) ? item.positions : [],
          orders: Array.isArray(item.orders) ? item.orders : [],
          trades: Array.isArray(item.trades) ? item.trades : [],
          allowedActions: Array.isArray(item.allowed_actions)
            ? item.allowed_actions.filter((action): action is string => typeof action === "string")
            : [],
          actionReasons: Object.fromEntries(Object.entries(recordValue(item.action_reasons)).map(([key, value]) => [key, String(value)])),
          protection: {
            source: String(protection.source ?? "none"),
            status: String(protection.status ?? "unknown"),
            summary: String(protection.summary ?? "No protection runtime attached"),
            lastCheckedAt: stringOrNull(protection.last_checked_at),
            details: recordValue(protection.details),
          },
          lastUpdatedAt: stringOrNull(item.last_updated_at),
        };
      }),
    unattributed: {
      displayName: String(unattributed.display_name ?? "Manual / unattributed broker exposure"),
      positions: Array.isArray(unattributed.positions) ? unattributed.positions : [],
      orders: Array.isArray(unattributed.orders) ? unattributed.orders : [],
      realizedPnl: numberValue(unattributed.realized_pnl),
      unrealizedPnl: numberValue(unattributed.unrealized_pnl),
      netPnl: numberValue(unattributed.net_pnl),
    },
  };
}

export async function fetchControlPlaneSnapshot(): Promise<ControlPlaneSnapshot> {
  const response = await apiFetch<Record<string, unknown>>("/api/control/strategy-positions");
  return normalizeControlPlaneSnapshot(response);
}

export async function exitControlStrategy(
  strategyRunId: string,
  payload: { reason?: string; dryRun?: boolean; accountScope?: string } = {},
) {
  return apiFetch(`/api/control/strategies/${encodeURIComponent(strategyRunId)}/exit`, {
    method: "POST",
    body: JSON.stringify({
      reason: payload.reason ?? "operator_exit",
      dry_run: Boolean(payload.dryRun),
      account_scope: payload.accountScope ?? "default",
    }),
  });
}

export async function cancelControlStrategyOrders(strategyRunId: string, payload: { reason?: string } = {}) {
  return apiFetch(`/api/control/strategies/${encodeURIComponent(strategyRunId)}/cancel-orders`, {
    method: "POST",
    body: JSON.stringify({ reason: payload.reason ?? "operator_cancel", dry_run: false, account_scope: "default" }),
  });
}

export async function reconcileControlPlane() {
  return apiFetch("/api/control/reconcile", { method: "POST" });
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

export async function exitPaperStrategy(accountScope: string, strategyId: string): Promise<{
  status: string;
  strategy_id: string;
  results?: unknown[];
  message?: string;
}> {
  return apiFetch(`/api/system/paper/accounts/${encodeURIComponent(accountScope)}/exit-strategy`, {
    method: "POST",
    json: { strategy_id: strategyId },
  });
}
