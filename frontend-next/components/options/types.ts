export type OptionType = "call" | "put";
export type PositionSide = "long" | "short";
export type Underlying = "NIFTY" | "BANKNIFTY";

export type StrategyFamily =
  | "directional"
  | "neutral-short-premium"
  | "long-vol"
  | "premium-managed-structure";

export type ProtectionTriggerKind =
  | "index-stoploss"
  | "index-target"
  | "combined-premium-stoploss"
  | "combined-premium-target";

export type RuleInputGroup = "primary" | "secondary" | "emergency";

export type StrategyRuleInputDescriptor = {
  key: string;
  label: string;
  unit: string;
  group: RuleInputGroup;
  visible: boolean;
  required: boolean;
  recommended: boolean;
  value?: number | null;
  source: "backend_default" | "backend_required" | "user_input" | "empty_optional";
  help_text?: string | null;
};

export type NormalizedStrategyRule = {
  key: string;
  metric: "index_price" | "combined_premium_points" | "basket_mtm_rupees";
  role: "emergency_guard" | "hard_stop" | "profit_target" | "trailing_stop";
  label: string;
  operator: "lte" | "gte";
  threshold: number;
  required: boolean;
  source: "backend_default" | "backend_required" | "user_input";
};

export type CanonicalStrategyPreview = {
  user_intent: string;
  inferred_structure: string;
  inferred_family: StrategyFamily;
  direction_bias: "bullish" | "bearish" | "neutral" | "long_volatility" | "structure";
  classification_confidence: number;
  classification_reason: string;
  description: string;
  primary_metric: "index_price" | "combined_premium_points" | "basket_mtm_rupees";
  emergency_metric?: "index_price" | "combined_premium_points" | "basket_mtm_rupees" | null;
  combined_premium_entry_type?: "credit" | "debit" | null;
  entry_combined_premium_points: number;
  estimated_entry_cost_rupees: number;
  warnings: string[];
  precedence: Array<"emergency_guard" | "hard_stop" | "profit_target" | "trailing_stop">;
  inputs: Record<string, StrategyRuleInputDescriptor>;
  rules: NormalizedStrategyRule[];
};

export type OptionLeg = {
  optionType: OptionType;
  side: PositionSide;
  strike: number;
  premium: number;
  quantity?: number;
  contractSize?: number;
  expiryKey?: string;
};

export type ProtectionProfile = {
  family: StrategyFamily;
  triggers: ProtectionTriggerKind[];
  mandatoryIndexBracket: boolean;
  description?: string;
};

export type PayoffPoint = {
  spot: number;
  profitLoss: number;
};

export type PayoffSummary = {
  maxProfit: number;
  maxLoss: number;
  breakEvenSpots: number[];
  currentSpotProfitLoss: number;
};

export type RuntimeStatus = {
  brokerConnected: boolean;
  websocketStatus: string;
  paperAvailable: boolean;
  appAuthenticated: boolean;
};

export type SnapshotOptionSide = {
  token: number;
  tsym: string;
  ltp: number | null;
  iv: number | null;
  oi: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
};

export type SnapshotChainRow = {
  strike: number;
  ce: SnapshotOptionSide | null;
  pe: SnapshotOptionSide | null;
  isAtm?: boolean;
};

export type OptionSessionSnapshot = {
  underlying: string;
  spotLtp: number | null;
  atmStrike: number | null;
  expiries: string[];
  rows: SnapshotChainRow[];
};

export type MiniChainRow = {
  strike: number;
  isAtm: boolean;
  ce: {
    instrumentToken: number;
    tradingSymbol: string;
    ltp: number;
    lotSize: number;
    delta: number;
    gamma: number;
    theta: number;
    vega: number;
    iv: number;
    oi?: number;
  } | null;
  pe: {
    instrumentToken: number;
    tradingSymbol: string;
    ltp: number;
    lotSize: number;
    delta: number;
    gamma: number;
    theta: number;
    vega: number;
    iv: number;
    oi?: number;
  } | null;
};

export type MiniChainSnapshot = {
  underlying: string;
  expiry: string;
  spotPrice: number;
  atmStrike: number;
  strikes: MiniChainRow[];
};

export type StrategyTemplateConfig = {
  id: string;
  label: string;
  description: string;
  strategyType: "single_leg" | "straddle" | "strangle" | "iron_condor" | "manual";
  familyHint?: StrategyFamily;
  legBlueprints: Array<{
    optionType: "call" | "put";
    side: PositionSide;
    strikeOffset: number;
  }>;
};

export type BuilderLeg = OptionLeg & {
  instrumentToken?: number;
  tradingSymbol?: string;
  lotSize: number;
  delta?: number;
  gamma?: number;
  theta?: number;
  vega?: number;
  iv?: number;
  oi?: number;
};

export type DryRunPlan = {
  mode: "dry_run" | "execution";
  message: string;
  strategyId?: string;
  strategy?: CanonicalStrategyPreview;
  orders?: Array<{
    tradingsymbol: string;
    transaction_type: string;
    quantity: number;
    estimated_price?: number;
  }>;
  estimatedMargin?: number;
  estimatedCost?: number;
};

export type LivePosition = {
  key: string;
  tradingSymbol: string;
  exchange: string;
  product: string;
  quantity: number;
  averagePrice: number;
  lastPrice: number;
  pnl: number;
  realizedPnl?: number;
  unrealizedPnl?: number;
  badge: "strategy" | "naked" | "manual" | "algo" | "unmanaged";
};

export type NiftyImpactRow = {
  symbol: string;
  sector: string;
  weight: number;
  lastPrice?: number | null;
  changePercent?: number | null;
  contribution?: number | null;
};

export type CandlePoint = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type ChartTimeframe = "5m" | "15m" | "60m" | "1d";
