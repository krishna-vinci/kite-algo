import type {
  CanonicalStrategyPreview,
  CandlePoint,
  ChartTimeframe,
  DryRunPlan,
  LivePosition,
  MiniChainSnapshot,
  NiftyImpactRow,
  OptionSessionSnapshot,
  RuntimeStatus,
  SnapshotOptionSide,
  Underlying,
} from "@/components/options/types";
import { apiFetch } from "@/lib/api/client";

type SessionStatusResponse = {
  app?: { authenticated?: boolean };
  broker?: {
    connected?: boolean;
    status?: "connected" | "reconnecting" | "degraded" | "disconnected" | "unknown";
    mode?: "system";
    last_login?: { last_success_at?: string | null; last_failure_at?: string | null; last_error?: string | null };
    scheduler?: { next_run?: string | null };
  };
  runtime?: { websocket?: { status?: string }; paper_runtime?: { available?: boolean } };
};

type SessionSnapshotResponse = {
  underlying: string;
  spot_ltp?: number | null;
  expiries: string[];
  updated_at?: string | null;
  per_expiry: Record<
    string,
    {
      forward?: number | null;
      sigma_expiry?: number | null;
      atm_strike?: number | null;
      strikes?: number[];
      rows: Array<{
        strike: number;
        CE?: {
          token: number;
          tsym: string;
          ltp?: number | null;
          lot_size?: number | null;
          iv?: number | null;
          oi?: number | null;
          delta?: number | null;
          gamma?: number | null;
          theta?: number | null;
          vega?: number | null;
        } | null;
        PE?: {
          token: number;
          tsym: string;
          ltp?: number | null;
          lot_size?: number | null;
          iv?: number | null;
          oi?: number | null;
          delta?: number | null;
          gamma?: number | null;
          theta?: number | null;
          vega?: number | null;
        } | null;
      }>;
    }
  >;
};

type MiniChainResponse = {
  underlying: string;
  expiry: string;
  spot_price: number;
  atm_strike: number;
  strikes: Array<{
    strike: number;
    is_atm: boolean;
    ce: {
      instrument_token: number;
      tradingsymbol: string;
      ltp: number;
      lot_size: number;
      greeks?: { delta?: number; gamma?: number; theta?: number; vega?: number; iv?: number };
      oi?: number;
    } | null;
    pe: {
      instrument_token: number;
      tradingsymbol: string;
      ltp: number;
      lot_size: number;
      greeks?: { delta?: number; gamma?: number; theta?: number; vega?: number; iv?: number };
      oi?: number;
    } | null;
  }>;
};

type SuggestStrikesResponse = {
  strategy_type?: string;
  strikes?: {
    ce?: { strike: number; tradingsymbol: string; instrument_token: number; delta: number; ltp: number; lot_size: number };
    pe?: { strike: number; tradingsymbol: string; instrument_token: number; delta: number; ltp: number; lot_size: number };
  };
  suggested_lots?: number;
};

type BuildPositionResponse = {
  mode: "dry_run" | "paper" | "live" | "execution";
  message: string;
  strategy?: CanonicalStrategyPreview;
  plan?: {
    orders?: Array<{
      tradingsymbol: string;
      transaction_type: string;
      quantity: number;
      estimated_price?: number;
    }>;
    estimated_margin?: number;
    estimated_cost?: number;
  };
  strategy_id?: string;
  status?: string;
  orders_placed?: unknown[];
  orders_failed?: unknown[];
};

type PreviewStrategyResponse = {
  strategy: CanonicalStrategyPreview;
};

type RealtimePositionsResponse = {
  positions?: Record<
    string,
    {
      tradingsymbol: string;
      exchange: string;
      product: string;
      quantity: number;
      average_price: number;
      last_price: number;
      pnl: number;
      realized_pnl?: number;
      unrealized_pnl?: number;
    }
  >;
};

type Nifty50ApiRow = {
  tradingsymbol?: string;
  sector?: string | null;
  index_weight?: number | null;
  ltp?: number | null;
  net_change_percent?: number | null;
  return_attribution?: number | null;
};

type CandlesApiResponse = {
  candles: CandlePoint[];
};

export async function ensureOptionsSessions(): Promise<void> {
  await apiFetch("/api/options/sessions", {
    method: "POST",
    json: {
      replace: false,
      items: [
        { underlying: "NIFTY", window: 12, cadence_sec: 5 },
        { underlying: "BANKNIFTY", window: 12, cadence_sec: 5 },
      ],
    },
  });
}

export async function loginApp(payload: { username: string; password: string }): Promise<{ user?: { username: string; role: string } }> {
  return apiFetch("/api/auth/login", {
    method: "POST",
    json: payload,
  });
}

export async function loginToBroker(): Promise<{ authenticated?: boolean; session_id?: string }> {
  return apiFetch("/api/login_kite", { method: "POST" });
}

export async function fetchRuntimeStatus(): Promise<RuntimeStatus> {
  const response = await apiFetch<SessionStatusResponse>("/api/auth/session-status");
  const brokerStatus = response.broker?.status ? response.broker.status.toLowerCase() : "unknown";
  const websocketStatus = response.runtime?.websocket?.status ? response.runtime.websocket.status.toLowerCase() : "unknown";
  return {
    brokerConnected: Boolean(response.broker?.connected),
    brokerStatus: brokerStatus as RuntimeStatus["brokerStatus"],
    brokerMode: response.broker?.mode ?? "system",
    brokerLastSuccessAt: response.broker?.last_login?.last_success_at ?? null,
    brokerLastFailureAt: response.broker?.last_login?.last_failure_at ?? null,
    brokerLastError: response.broker?.last_login?.last_error ?? null,
    brokerNextRefreshAt: response.broker?.scheduler?.next_run ?? null,
    websocketStatus: websocketStatus,
    paperAvailable: Boolean(response.runtime?.paper_runtime?.available),
    appAuthenticated: Boolean(response.app?.authenticated),
  };
}

export function normalizeOptionSessionSnapshot(response: SessionSnapshotResponse): OptionSessionSnapshot {
  const expiries = response.expiries ?? [];
  const normalizedPerExpiry = Object.fromEntries(
    Object.entries(response.per_expiry ?? {}).map(([expiry, expiryData]) => [
      expiry,
      {
        forward: expiryData.forward ?? null,
        sigmaExpiry: expiryData.sigma_expiry ?? null,
        atmStrike: expiryData.atm_strike ?? null,
        strikes: expiryData.strikes ?? [],
        rows: expiryData.rows.map((row) => ({
          strike: row.strike,
          ce: row.CE
            ? {
                token: row.CE.token,
                tsym: row.CE.tsym,
                ltp: row.CE.ltp ?? null,
                lotSize: row.CE.lot_size ?? null,
                iv: row.CE.iv ?? null,
                oi: row.CE.oi ?? null,
                delta: row.CE.delta ?? null,
                gamma: row.CE.gamma ?? null,
                theta: row.CE.theta ?? null,
                vega: row.CE.vega ?? null,
              }
            : null,
          pe: row.PE
            ? {
                token: row.PE.token,
                tsym: row.PE.tsym,
                ltp: row.PE.ltp ?? null,
                lotSize: row.PE.lot_size ?? null,
                iv: row.PE.iv ?? null,
                oi: row.PE.oi ?? null,
                delta: row.PE.delta ?? null,
                gamma: row.PE.gamma ?? null,
                theta: row.PE.theta ?? null,
                vega: row.PE.vega ?? null,
              }
            : null,
          isAtm: row.strike === expiryData.atm_strike,
        })),
      },
    ]),
  );
  const primaryExpiry = expiries[0];
  const primaryExpiryData = primaryExpiry ? normalizedPerExpiry[primaryExpiry] : undefined;
  return {
    underlying: response.underlying,
    spotLtp: response.spot_ltp ?? null,
    atmStrike: primaryExpiryData?.atmStrike ?? null,
    expiries,
    perExpiry: normalizedPerExpiry,
    rows: primaryExpiryData?.rows ?? [],
    updatedAt: response.updated_at ?? null,
  };
}

function mergeSnapshotSide(
  previous: SnapshotOptionSide | null | undefined,
  next: SnapshotOptionSide | null | undefined,
): SnapshotOptionSide | null {
  if (!next) {
    return null;
  }
  if (!previous) {
    return next;
  }
  return {
    ...previous,
    ...next,
    lotSize: next.lotSize ?? previous.lotSize ?? null,
  };
}

export function mergeOptionSessionSnapshot(
  previous: OptionSessionSnapshot | null | undefined,
  next: OptionSessionSnapshot,
): OptionSessionSnapshot {
  if (!previous) {
    return next;
  }

  const previousPerExpiry = previous.perExpiry ?? {};
  const mergedPerExpiry = Object.fromEntries(
    Object.entries(next.perExpiry ?? {}).map(([expiry, expiryData]) => {
      const previousRowsByStrike = new Map(
        (previousPerExpiry[expiry]?.rows ?? []).map((row) => [row.strike, row]),
      );

      return [
        expiry,
        {
          ...expiryData,
          rows: expiryData.rows.map((row) => {
            const previousRow = previousRowsByStrike.get(row.strike);
            return {
              ...row,
              ce: mergeSnapshotSide(previousRow?.ce, row.ce),
              pe: mergeSnapshotSide(previousRow?.pe, row.pe),
            };
          }),
        },
      ];
    }),
  );

  const primaryExpiry = next.expiries[0];
  return {
    ...next,
    perExpiry: mergedPerExpiry,
    rows: primaryExpiry ? mergedPerExpiry[primaryExpiry]?.rows ?? next.rows : next.rows,
  };
}

export async function fetchOptionSession(underlying: Underlying): Promise<OptionSessionSnapshot> {
  const response = await apiFetch<SessionSnapshotResponse>(`/api/options/session/${underlying}`);
  return normalizeOptionSessionSnapshot(response);
}

export async function fetchAvailableExpiries(underlying: Underlying): Promise<string[]> {
  const response = await apiFetch<{ expiries?: string[] }>(`/api/strategies/available-expiries/${underlying}`);
  return response.expiries ?? [];
}

export async function fetchMiniChain(underlying: Underlying, expiry: string, centerStrike?: number): Promise<MiniChainSnapshot> {
  const suffix = centerStrike ? `?center_strike=${centerStrike}&count=11` : "?count=11";
  const response = await apiFetch<MiniChainResponse>(`/api/strategies/mini-chain/${underlying}/${expiry}${suffix}`);
  return {
    underlying: response.underlying,
    expiry: response.expiry,
    spotPrice: response.spot_price,
    atmStrike: response.atm_strike,
    strikes: response.strikes.map((row) => ({
      strike: row.strike,
      isAtm: row.is_atm,
      ce: row.ce
        ? {
            instrumentToken: row.ce.instrument_token,
            tradingSymbol: row.ce.tradingsymbol,
            ltp: row.ce.ltp,
            lotSize: row.ce.lot_size,
            delta: row.ce.greeks?.delta ?? 0,
            gamma: row.ce.greeks?.gamma ?? 0,
            theta: row.ce.greeks?.theta ?? 0,
            vega: row.ce.greeks?.vega ?? 0,
            iv: row.ce.greeks?.iv ?? 0,
            oi: row.ce.oi,
          }
        : null,
      pe: row.pe
        ? {
            instrumentToken: row.pe.instrument_token,
            tradingSymbol: row.pe.tradingsymbol,
            ltp: row.pe.ltp,
            lotSize: row.pe.lot_size,
            delta: row.pe.greeks?.delta ?? 0,
            gamma: row.pe.greeks?.gamma ?? 0,
            theta: row.pe.greeks?.theta ?? 0,
            vega: row.pe.greeks?.vega ?? 0,
            iv: row.pe.greeks?.iv ?? 0,
            oi: row.pe.oi,
          }
        : null,
    })),
  };
}

export async function suggestStrikes(payload: {
  underlying: Underlying;
  expiry: string;
  strategyType: string;
  targetDelta: number;
  riskAmount?: number;
}): Promise<SuggestStrikesResponse> {
  return apiFetch("/api/strategies/suggest-strikes", {
    method: "POST",
    json: {
      underlying: payload.underlying,
      expiry: payload.expiry,
      strategy_type: payload.strategyType,
      target_delta: payload.targetDelta,
      risk_amount: payload.riskAmount,
    },
  });
}

export async function buildPositionDryRun(payload: {
  underlying: Underlying;
  expiry: string;
  strategyType: string;
  templateId?: string;
  selectedLegs: Array<Record<string, unknown>>;
  protectionConfig?: Record<string, unknown>;
  currentSpot?: number;
}): Promise<DryRunPlan> {
  const response = await apiFetch<BuildPositionResponse>("/api/strategies/build-position", {
    method: "POST",
    json: {
      underlying: payload.underlying,
      expiry: payload.expiry,
      strategy_type: payload.strategyType,
      template_id: payload.templateId,
      selected_strikes: payload.selectedLegs,
      protection_config: payload.protectionConfig,
      current_spot: payload.currentSpot,
      execution_mode: "dry_run",
      place_orders: false,
    },
  });
  return {
    mode: "dry_run",
    message: response.message,
    strategyId: response.strategy_id,
    strategy: response.strategy,
    orders: response.plan?.orders,
    estimatedMargin: response.plan?.estimated_margin,
    estimatedCost: response.plan?.estimated_cost,
  };
}

export async function previewOptionStrategy(payload: {
  underlying: Underlying;
  expiry: string;
  strategyType: string;
  templateId?: string;
  selectedLegs: Array<Record<string, unknown>>;
  protectionConfig?: Record<string, unknown>;
  currentSpot?: number;
}): Promise<CanonicalStrategyPreview> {
  const response = await apiFetch<PreviewStrategyResponse>("/api/strategies/preview-option-strategy", {
    method: "POST",
    json: {
      underlying: payload.underlying,
      expiry: payload.expiry,
      strategy_type: payload.strategyType,
      template_id: payload.templateId,
      selected_strikes: payload.selectedLegs,
      protection_config: payload.protectionConfig,
      current_spot: payload.currentSpot,
    },
  });
  return response.strategy;
}

export async function executePaperOptionStrategy(payload: {
  accountScope: string;
  underlying: Underlying;
  expiry: string;
  strategyType: string;
  templateId?: string;
  selectedLegs: Array<Record<string, unknown>>;
  protectionConfig?: Record<string, unknown>;
  currentSpot?: number;
}): Promise<{ mode: string; status: string; strategyId?: string; strategy?: CanonicalStrategyPreview; message: string }> {
  const response = await apiFetch<BuildPositionResponse>("/api/strategies/build-position", {
    method: "POST",
    json: {
      underlying: payload.underlying,
      expiry: payload.expiry,
      strategy_type: payload.strategyType,
      template_id: payload.templateId,
      selected_strikes: payload.selectedLegs,
      protection_config: payload.protectionConfig,
      current_spot: payload.currentSpot,
      account_scope: payload.accountScope,
      execution_mode: "paper",
      place_orders: false,
    },
  });
  return {
    mode: response.mode,
    status: response.status ?? "unknown",
    strategyId: response.strategy_id,
    strategy: response.strategy,
    message: response.message,
  };
}

export async function fetchRealtimePositions(): Promise<LivePosition[]> {
  const response = await apiFetch<RealtimePositionsResponse>("/api/strategies/positions/realtime-summary");
  return Object.entries(response.positions ?? {}).map(([key, value]) => ({
    key,
    tradingSymbol: value.tradingsymbol,
    exchange: value.exchange,
    product: value.product,
    quantity: value.quantity,
    averagePrice: value.average_price,
    lastPrice: value.last_price,
    pnl: value.pnl,
    realizedPnl: value.realized_pnl,
    unrealizedPnl: value.unrealized_pnl,
    badge: Math.abs(value.quantity) > 0 && /(CE|PE)$/i.test(value.tradingsymbol) ? "naked" : "unmanaged",
  }));
}

export async function fetchNifty50Impact(): Promise<NiftyImpactRow[]> {
  const response = await apiFetch<Record<string, Nifty50ApiRow[]>>("/api/nifty50");
  return Object.entries(response).flatMap(([sector, rows]) =>
    rows.map((row) => ({
      symbol: row.tradingsymbol ?? "UNKNOWN",
      sector: row.sector ?? sector,
      weight: Number(row.index_weight ?? 0),
      lastPrice: row.ltp ?? null,
      changePercent: row.net_change_percent ?? null,
      contribution: row.return_attribution ?? null,
    })),
  );
}

/**
 * Normalize UI timeframe aliases to backend canonical timeframe strings.
 * Must match backend's TIMEFRAME_ALIASES in candles_api.py.
 */
const TIMEFRAME_ALIASES: Record<string, string> = {
  "1m": "minute",
  min: "minute",
  minute: "minute",
  "3m": "3minute",
  "3minute": "3minute",
  "5m": "5minute",
  "5minute": "5minute",
  "10m": "10minute",
  "10minute": "10minute",
  "15m": "15minute",
  "15minute": "15minute",
  "30m": "30minute",
  "30minute": "30minute",
  "60m": "60minute",
  "1h": "60minute",
  "60minute": "60minute",
  "1d": "day",
  day: "day",
};

function publicApiBaseUrl() {
  return typeof window === "undefined" ? (process.env.NEXT_PUBLIC_API_BASE_URL ?? "") : "";
}

export function normalizeTimeframe(timeframe: string): string {
  const normalized = TIMEFRAME_ALIASES[timeframe.toLowerCase()];
  if (normalized) {
    return normalized;
  }
  const validTimeframes = ["minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day"];
  if (validTimeframes.includes(timeframe)) {
    return timeframe;
  }
  throw new Error(`Invalid timeframe alias: "${timeframe}"`);
}

export async function fetchCandles(payload: { identifier: string; timeframe: ChartTimeframe; fromIso: string; toIso: string }): Promise<CandlePoint[]> {
  const canonicalTimeframe = normalizeTimeframe(payload.timeframe);
  const params = new URLSearchParams({
    timeframe: canonicalTimeframe,
    from: payload.fromIso,
    to: payload.toIso,
    ingest: "true",
  });
  const response = await apiFetch<CandlesApiResponse>(`/api/candles/${encodeURIComponent(payload.identifier)}?${params.toString()}`);
  return response.candles ?? [];
}

export function buildCandlesStreamUrl(identifier: string, timeframe: ChartTimeframe): string {
  const canonicalTimeframe = normalizeTimeframe(timeframe);
  const base = publicApiBaseUrl();
  return `${base}/api/candles/stream/${encodeURIComponent(identifier)}?timeframe=${encodeURIComponent(canonicalTimeframe)}`;
}

export function buildOptionsSessionSseUrl(underlying: Underlying): string {
  const base = publicApiBaseUrl();
  return `${base}/api/sse/options/session/${encodeURIComponent(underlying)}`;
}

export async function executePaperBasket(payload: {
  accountScope: string;
  orders: Array<{
    tradingsymbol: string;
    transaction_type: string;
    quantity: number;
    exchange?: string;
    product?: string;
    order_type?: string;
  }>;
  strategyTag?: string;
  notes?: string;
}): Promise<{ mode: string; status: string; results?: unknown[]; errors?: unknown[] }> {
  return apiFetch(`/api/system/paper/accounts/${payload.accountScope}/basket`, {
    method: "POST",
    json: {
      strategy_tag: payload.strategyTag,
      notes: payload.notes,
      all_or_none: true,
      orders: payload.orders.map((order) => ({
        exchange: order.exchange ?? "NFO",
        tradingsymbol: order.tradingsymbol,
        product: order.product ?? "MIS",
        transaction_type: order.transaction_type,
        order_type: order.order_type ?? "MARKET",
        quantity: order.quantity,
      })),
    },
  });
}
