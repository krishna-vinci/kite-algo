// ---------------------------------------------------------------------------
// Journal v2 typed API response shapes — F2 phase
// Field names match backend snake_case output exactly.
// ---------------------------------------------------------------------------

export type MetricPeriod = "day" | "week" | "month" | "year" | "since_inception";

// ---------------------------------------------------------------------------
// Shared sub-types
// ---------------------------------------------------------------------------

export type JournalEnvironmentRef = {
  environment_id: string;
  mode: string;
  account_scope: string;
  display_name: string | null;
  broker_user_id: string | null;
  paper_account_key: string | null;
};

export type JournalV2StrategyRef = {
  template_id: string;
  strategy_family: string;
  template_key: string | null;
  display_name: string | null;
};

export type CostBreakdown = {
  brokerage: string | number;
  exchange_txn_charge: string | number;
  stt: string | number;
  stamp_duty: string | number;
  sebi_charge: string | number;
  gst: string | number;
  total_taxes: string | number;
  total_charges: string | number;
};

export type AnalyticsMetrics = {
  gross_pnl: string | number;
  net_pnl: string | number;
  total_charges: string | number;
  realized_pnl: string | number;
  cost_breakdown: CostBreakdown;
  cost_ratio: string | number | null;
  closed_episode_count: number;
  hold_seconds_total: number;
  hold_seconds_avg: number | null;
  win_count: number;
  loss_count: number;
  win_rate: string | number | null;
  average_win: string | number | null;
  average_loss: string | number | null;
  expectancy: string | number | null;
  profit_factor: string | number | null;
  sharpe_ratio: string | number | null;
  sortino_ratio: string | number | null;
  max_drawdown: string | number | null;
  max_drawdown_duration_days: number | null;
  cumulative_return: string | number | null;
  max_win_streak: number;
  max_loss_streak: number;
  mae: string | number | null;
  mfe: string | number | null;
  r_multiple: string | number | null;
};

export type EpisodeOutcome = {
  episode_id: string;
  gross_pnl: string | number;
  net_pnl: string | number;
  total_charges: string | number;
  realized_pnl: string | number;
  hold_seconds: number;
  cost_breakdown: CostBreakdown;
};

// ---------------------------------------------------------------------------
// Episode sub-types
// ---------------------------------------------------------------------------

export type JournalV2EpisodeCard = {
  episode_id: string;
  status: string;
  opened_at: string;
  closed_at: string | null;
  strategy: JournalV2StrategyRef | null;
  direction: string | null;
  outcome: EpisodeOutcome;
  fill_count: number;
  leg_count: number;
  notes: string;
};

export type JournalV2OpenEpisodeCard = {
  episode_id: string;
  status: string;
  opened_at: string;
  strategy: JournalV2StrategyRef | null;
  direction: string | null;
  fill_count: number;
  leg_count: number;
  current_pnl_estimate: string | number | null;
  notes: string;
};

export type JournalV2EpisodeLegView = {
  leg_id: number | null;
  leg_seq: number;
  instrument_token: number | null;
  exchange: string | null;
  tradingsymbol: string | null;
  product: string | null;
  direction: string | null;
  opened_quantity: number;
  closed_quantity: number;
  net_quantity: number;
  metadata: Record<string, unknown>;
};

export type JournalV2ExecutionFillView = {
  fact_id: number | null;
  leg_id: number | null;
  source_type: string;
  source_fact_key: string;
  order_id: string | null;
  trade_id: string | null;
  fill_timestamp: string;
  side: string;
  quantity: number;
  price: string | number;
  gross_cash_flow: string | number | null;
  fees_amount: string | number;
  taxes_amount: string | number;
  slippage_amount: string | number;
  brokerage: string | number | null;
  exchange_txn_charge: string | number | null;
  stt: string | number | null;
  stamp_duty: string | number | null;
  sebi_charge: string | number | null;
  gst: string | number | null;
  margin_required: string | number | null;
  charges_status: string | null;
  payload: Record<string, unknown>;
};

export type JournalV2TimelineEventView = {
  event_id: string | null;
  subject_type: string;
  subject_id: string;
  channel: string | null;
  event_type: string;
  actor_type: string;
  correlation_id: string | null;
  causation_id: string | null;
  occurred_at: string;
  payload: Record<string, unknown>;
};

// ---------------------------------------------------------------------------
// Journal v2 daily view
// ---------------------------------------------------------------------------

export type JournalV2DailySummary = {
  trading_date: string;
  metrics: AnalyticsMetrics;
  closed_episode_count: number;
  open_episode_count: number;
  strategy_count: number;
  notes_count: number;
};

export type JournalV2StrategyGroup = {
  strategy: JournalV2StrategyRef;
  metrics: AnalyticsMetrics;
  episodes: JournalV2EpisodeCard[];
};

export type JournalV2DailyResponse = {
  environment: JournalEnvironmentRef;
  trading_date: string;
  summary: JournalV2DailySummary;
  strategy_groups: JournalV2StrategyGroup[];
  open_episodes: JournalV2OpenEpisodeCard[];
};

// ---------------------------------------------------------------------------
// Journal v2 period view
// ---------------------------------------------------------------------------

export type JournalV2PeriodBucket = {
  bucket_start: string;
  bucket_end: string;
  label: string;
  metrics: AnalyticsMetrics;
  closed_episode_count: number;
};

export type JournalV2StrategySummaryItem = {
  strategy: JournalV2StrategyRef;
  metrics: AnalyticsMetrics;
  episode_count: number;
};

export type JournalV2PeriodResponse = {
  environment: JournalEnvironmentRef;
  from_date: string;
  to_date: string;
  granularity: string;
  summary: AnalyticsMetrics;
  buckets: JournalV2PeriodBucket[];
  strategies: JournalV2StrategySummaryItem[];
};

// ---------------------------------------------------------------------------
// Journal v2 episode detail
// ---------------------------------------------------------------------------

export type JournalV2EpisodeDetailResponse = {
  environment: JournalEnvironmentRef;
  episode: JournalV2EpisodeCard;
  legs: JournalV2EpisodeLegView[];
  fills: JournalV2ExecutionFillView[];
  timeline: JournalV2TimelineEventView[];
  notes: string;
};

// ---------------------------------------------------------------------------
// Journal v2 strategy list
// ---------------------------------------------------------------------------

export type JournalV2StrategyListResponse = {
  environment: JournalEnvironmentRef;
  period: MetricPeriod;
  anchor_date: string | null;
  items: JournalV2StrategySummaryItem[];
};
