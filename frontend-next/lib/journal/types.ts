// ---------------------------------------------------------------------------
// Journal domain types — API response shapes and view-model types
// ---------------------------------------------------------------------------

export type StrategyFamily =
  | "options_strategy"
  | "indicator_strategy"
  | "investment_strategy"
  | "discretionary_strategy";

export type ExecutionMode = "live" | "paper" | "dry_run";

export type JournalFilterParams = {
  strategy_family?: StrategyFamily;
  execution_mode?: ExecutionMode;
};

export type RunStatus = "open" | "closed" | "expired" | "cancelled" | "reviewed";

export type ReviewStatus = "pending" | "in_progress" | "completed" | "skipped";

export type RuleState = "draft" | "active" | "reinforced" | "decaying" | "retired";

export type InsightKind = "pattern" | "rule_suggestion" | "streak" | "anomaly" | "milestone";

export type AnalysisPeriod = "day" | "week" | "month" | "year" | "inception" | "since_inception";

export type JournalEnvironmentMode = "live" | "paper" | "dry_run_preview";

export type JournalEnvironment = {
  id: string;
  mode: JournalEnvironmentMode;
  account_scope: string;
  display_name: string | null;
  broker_user_id: string | null;
  paper_account_key: string | null;
  environment_epoch: number;
  metadata: Record<string, unknown>;
};

export type JournalEpisode = {
  id: string;
  environment_id: string;
  execution_context_id: string;
  episode_seq: number;
  status: string;
  opened_at: string;
  closed_at: string | null;
  metadata: Record<string, unknown>;
};

export type JournalV2AnalyticsMetrics = {
  gross_pnl: number | string;
  net_pnl: number | string;
  total_charges: number | string;
  realized_pnl: number | string;
  hold_seconds: number;
  closed_episode_count: number;
  win_rate: number | string | null;
  average_win: number | string | null;
  average_loss: number | string | null;
  expectancy: number | string | null;
  profit_factor: number | string | null;
  mae: Record<string, unknown>;
  mfe: Record<string, unknown>;
  r_multiple: Record<string, unknown>;
};

export type JournalV2AnalyticsSummary = {
  environment_id: string;
  closed_episode_count: number;
  metrics: JournalV2AnalyticsMetrics;
};

export type JournalV2StrategyScorecard = {
  template_id: string;
  strategy_family: string;
  display_name: string;
  metrics: JournalV2AnalyticsMetrics;
};

export type JournalV2StrategyAnalytics = {
  environment_id: string;
  items: JournalV2StrategyScorecard[];
  count: number;
};

export type JournalV2PaperLiveComparison = {
  template_id: string;
  paper_environment_id: string;
  live_environment_id: string;
  paper: JournalV2AnalyticsMetrics;
  live: JournalV2AnalyticsMetrics;
  combined: null;
};

export type JournalV2UnresolvedItem = {
  id: string;
  environment_id: string;
  execution_context_id: string | null;
  source_system: string;
  reason: string;
  raw_identity: Record<string, unknown>;
  candidate_mappings: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
  status: string;
  created_at: string;
  resolved_at: string | null;
};

export type JournalV2UnresolvedQueue = {
  environment_id: string;
  items: JournalV2UnresolvedItem[];
  count: number;
};

export type JournalTimelineEvent = {
  id: string;
  environment_id: string;
  episode_id: string | null;
  execution_context_id: string | null;
  subject_type: string;
  subject_id: string;
  event_type: string;
  channel: string | null;
  actor_type: string;
  correlation_id: string | null;
  causation_id: string | null;
  occurred_at: string;
  payload: Record<string, unknown>;
};

export type JournalNote = {
  id: string;
  environment_id: string;
  subject_type: string;
  subject_id: string;
  episode_id: string | null;
  note_type: string;
  title: string;
  body_markdown: string;
  body_text?: string;
  body_json?: Record<string, unknown> | null;
  tags: string[];
  metadata?: Record<string, unknown>;
  updated_at: string;
};

export type JournalNoteRevision = {
  note_id: string;
  revision_no: number;
  body_markdown: string;
  body_text?: string;
  edited_at?: string;
  change_reason?: string | null;
};

// ---------------------------------------------------------------------------
// API response types
// ---------------------------------------------------------------------------

export type JournalSummary = {
  period: AnalysisPeriod;
  run_count: number;
  closed_run_count: number;
  gross_profit: number;
  gross_loss: number;
  net_pnl: number;
  total_charges: number;
  win_rate: number | null;
  average_win: number | null;
  average_loss: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  cumulative_return: number | null;
  benchmark_return: number | null;
  excess_return: number | null;
  max_drawdown: number | null;
  drawdown_duration_days: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  best_streak: number | null;
  worst_streak: number | null;
  review_completion_rate: number | null;
  rule_adherence_rate: number | null;
};

export type BenchmarkComparison = {
  benchmark_id: string;
  benchmark_name: string;
  period: AnalysisPeriod;
  portfolio_return: number | null;
  benchmark_return: number | null;
  excess_return: number | null;
  portfolio_series: Array<{ date: string; value: number }>;
  benchmark_series: Array<{ date: string; value: number }>;
};

export type CalendarDay = {
  date: string;
  net_pnl: number;
  run_count: number;
  win_count: number;
  loss_count: number;
};

export type JournalRun = {
  id: string;
  strategy_family: StrategyFamily;
  strategy_name: string;
  status: RunStatus;
  review_status: ReviewStatus;
  entry_surface: string | null;
  opened_at: string;
  closed_at: string | null;
  net_pnl: number | null;
  total_charges: number | null;
  notes: string | null;
  tags: string[];
  decision_events: DecisionEvent[];
};

export type DecisionEvent = {
  id: string;
  event_type: string;
  description: string;
  created_at: string;
};

export type JournalTrade = {
  id: string;
  run_id: string;
  tradingsymbol: string;
  transaction_type: "BUY" | "SELL";
  quantity: number;
  price: number;
  executed_at: string;
  charges: number | null;
};

export type StrategyPerformance = {
  strategy_family: StrategyFamily;
  strategy_name: string;
  run_count: number;
  closed_count: number;
  net_pnl: number;
  win_rate: number | null;
  avg_pnl: number | null;
  profit_factor: number | null;
  best_run_pnl: number | null;
  worst_run_pnl: number | null;
};

export type JournalRule = {
  id: string;
  title: string;
  description: string;
  state: RuleState;
  category: string;
  adherence_rate: number | null;
  total_checks: number;
  created_at: string;
  updated_at: string;
};

export type RuleCreatePayload = {
  title: string;
  description: string;
  category: string;
};

export type RuleUpdatePayload = {
  title?: string;
  description?: string;
  state?: RuleState;
  category?: string;
};

export type ReviewQueueItem = {
  run_id: string;
  strategy_name: string;
  strategy_family: StrategyFamily;
  status: RunStatus;
  review_status: ReviewStatus;
  net_pnl: number | null;
  closed_at: string | null;
  opened_at: string;
};

export type ReviewUpdatePayload = {
  review_status: ReviewStatus;
  notes?: string;
};

export type JournalInsight = {
  id: string;
  kind: InsightKind;
  title: string;
  description: string;
  relevance_score: number | null;
  created_at: string;
  related_run_ids: string[];
  related_rule_ids: string[];
};

// ---------------------------------------------------------------------------
// Paginated response wrapper
// ---------------------------------------------------------------------------

export type Paginated<T> = {
  items: T[];
  total: number;
  page: number;
  page_size: number;
};
