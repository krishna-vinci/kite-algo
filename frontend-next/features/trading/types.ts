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

export type StrategyRiskControls = {
  indexLowerBoundary: number | null;
  indexUpperBoundary: number | null;
  combinedPremiumTarget: number | null;
  combinedPremiumStoploss: number | null;
  basketMtmTarget: number | null;
  basketMtmStoploss: number | null;
};

export type StrategyCapabilities = {
  canEditRisk: boolean;
  editRiskReason: string | null;
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
  riskControls: StrategyRiskControls;
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

export type TradingConsoleSnapshot = {
  runtime: RuntimeStatus;
  quotes: MarketQuote[];
  paper: TradingPaperSummary;
  broker: TradingBrokerSnapshot;
};
