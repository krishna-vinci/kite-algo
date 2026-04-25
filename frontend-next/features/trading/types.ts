import type { RuntimeStatus } from "@/components/options/types";

export type TradingMode = "paper" | "dry_run" | "live";

export type MarketQuoteSymbol = "NIFTY" | "BANKNIFTY";

export type MarketQuote = {
  symbol: MarketQuoteSymbol;
  token: number;
  lastPrice: number | null;
  changePercent: number | null;
  connected: boolean;
};

export type StrategyCapabilities = {
  canEditRisk: boolean;
  editRiskReason: string | null;
  canExitStrategy: boolean;
  exitReason: string | null;
  allowedActions: string[];
  riskSchema: StrategyRiskField[];
};

export type StrategyRiskField = {
  key: string;
  label: string;
  type: string;
  unit?: string | null;
  group?: string | null;
  required?: boolean;
  recommended?: boolean;
  value?: number | string | null;
};

export type StrategySummaryField = {
  key: string;
  label: string;
  value?: number | string | null;
  unit?: string | null;
  group?: string | null;
};

export type TradingTimelineItem = {
  kind: string;
  timestamp: string | null;
  label: string;
};

export type TradingPositionRow = Record<string, unknown>;
export type TradingOrderRow = Record<string, unknown>;
export type TradingTradeRow = Record<string, unknown>;

export type TradingStrategyGroup = {
  strategyRunId: string;
  strategyId: string;
  displayName: string;
  strategyTag?: string | null;
  algoInstanceId?: string | null;
  mode: TradingMode;
  status: string;
  isOpen: boolean;
  openLegCount: number;
  netQuantity?: number;
  realizedPnl: number;
  unrealizedPnl: number;
  marginInUse?: number;
  lastUpdatedAt?: string | null;
  summaryFields: StrategySummaryField[];
  capabilities: StrategyCapabilities;
  positions: TradingPositionRow[];
  orders: TradingOrderRow[];
  trades: TradingTradeRow[];
  timeline: TradingTimelineItem[];
};

export type TradingPaperAccount = {
  accountScope: string;
  currency: string;
  startingBalance: number;
  availableFunds: number;
  blockedFunds: number;
  realizedPnl: number;
  unrealizedPnl: number;
  openPositionCount: number;
};

export type TradingPaperSummary = {
  accountScope: string;
  account: TradingPaperAccount;
  activeStrategyCount: number;
  strategies: TradingStrategyGroup[];
};

export type TradingBrokerPosition = {
  positionKey: string;
  tradingSymbol: string;
  exchange: string;
  product: string;
  quantity: number;
  averagePrice: number;
  lastPrice: number;
  pnl: number;
  realizedPnl: number;
  unrealizedPnl: number;
};

export type TradingBrokerSnapshot = {
  positions: TradingBrokerPosition[];
  activeCount: number;
};

export type ControlStrategySource = "paper_runtime" | "algo_worker" | "broker_unattributed";
export type ControlHealthStatus = "healthy" | "stale" | "disconnected" | "unknown";

export type ControlProtectionState = {
  source: string;
  status: string;
  summary: string;
  lastCheckedAt: string | null;
  details: Record<string, unknown>;
};

export type ControlStrategyGroup = {
  strategyRunId: string;
  displayName: string;
  source: ControlStrategySource;
  mode: TradingMode;
  status: string;
  healthStatus: ControlHealthStatus;
  heartbeatAgeSec: number | null;
  workerId: string | null;
  workerName: string | null;
  workerMetrics: Record<string, unknown>;
  isOpen: boolean;
  realizedPnl: number;
  unrealizedPnl: number;
  netPnl: number;
  positionCount: number;
  openOrderCount: number;
  tradeCount: number;
  positions: TradingPositionRow[];
  orders: TradingOrderRow[];
  trades: TradingTradeRow[];
  allowedActions: string[];
  actionReasons: Record<string, string>;
  protection: ControlProtectionState;
  lastUpdatedAt: string | null;
};

export type ControlUnattributedBucket = {
  displayName: string;
  positions: TradingPositionRow[];
  orders: TradingOrderRow[];
  realizedPnl: number;
  unrealizedPnl: number;
  netPnl: number;
};

export type ControlPlaneSnapshot = {
  generatedAt: string | null;
  totals: {
    strategyCount: number;
    openStrategyCount: number;
    positionCount: number;
    staleWorkerCount: number;
    realizedPnl: number;
    unrealizedPnl: number;
    netPnl: number;
  };
  strategies: ControlStrategyGroup[];
  unattributed: ControlUnattributedBucket;
};

export type TradingConsoleSnapshot = {
  runtime: RuntimeStatus;
  quotes: MarketQuote[];
  paper: TradingPaperSummary;
  broker: TradingBrokerSnapshot;
  control: ControlPlaneSnapshot | null;
};
