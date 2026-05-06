// ---------------------------------------------------------------------------
// Analytics v1 typed API response shapes — F2 phase
// Field names match backend snake_case output exactly.
// ---------------------------------------------------------------------------

import type { AnalyticsMetrics, CostBreakdown, JournalEnvironmentRef, JournalV2StrategyRef, MetricPeriod } from "@/lib/journal/types-v2";

// Re-export shared types for analytics consumers
export type { AnalyticsMetrics, CostBreakdown, JournalEnvironmentRef, JournalV2StrategyRef, MetricPeriod };

// ---------------------------------------------------------------------------
// Analytics summary
// ---------------------------------------------------------------------------

export type AnalyticsStrategySummaryItem = {
  strategy: JournalV2StrategyRef;
  metrics: AnalyticsMetrics;
};

export type AnalyticsSummaryResponse = {
  environment: JournalEnvironmentRef;
  period: MetricPeriod;
  anchor_date: string | null;
  metrics: AnalyticsMetrics;
  strategies: AnalyticsStrategySummaryItem[];
};

// ---------------------------------------------------------------------------
// Strategy deep dive
// ---------------------------------------------------------------------------

export type EquityCurvePoint = {
  trading_date: string;
  realized_pnl: string | number;
  total_charges: string | number;
  ending_equity: string | number | null;
  starting_equity: string | number | null;
  return_pct: string | number | null;
  benchmark_return_pct: string | number | null;
  excess_return_pct: string | number | null;
};

export type StrategyDeepDiveResponse = {
  environment: JournalEnvironmentRef;
  period: MetricPeriod;
  anchor_date: string | null;
  strategy: JournalV2StrategyRef;
  metrics: AnalyticsMetrics;
  equity_curve: EquityCurvePoint[];
};

// ---------------------------------------------------------------------------
// Equity curve
// ---------------------------------------------------------------------------

export type EquityCurveResponse = {
  environment: JournalEnvironmentRef;
  period: MetricPeriod;
  anchor_date: string | null;
  template_id: string | null;
  metrics: AnalyticsMetrics;
  points: EquityCurvePoint[];
};

// ---------------------------------------------------------------------------
// Cost analysis
// ---------------------------------------------------------------------------

export type StrategyCostAnalysisItem = {
  strategy: JournalV2StrategyRef;
  cost_breakdown: CostBreakdown;
  total_charges: string | number;
  cost_ratio: string | number | null;
  closed_episode_count: number;
};

export type CostAnalysisResponse = {
  environment: JournalEnvironmentRef;
  period: MetricPeriod;
  anchor_date: string | null;
  metrics: AnalyticsMetrics;
  cost_breakdown: CostBreakdown;
  strategies: StrategyCostAnalysisItem[];
};

// ---------------------------------------------------------------------------
// Paper vs live comparison
// ---------------------------------------------------------------------------

export type ComparisonMetricDelta = {
  paper: string | number | null;
  live: string | number | null;
  delta: string | number | null;
  deviation_pct: string | number | null;
};

export type PaperLiveComparisonResponse = {
  template_id: string;
  period: MetricPeriod;
  anchor_date: string | null;
  paper_environment: JournalEnvironmentRef | null;
  live_environment: JournalEnvironmentRef | null;
  paper: AnalyticsMetrics;
  live: AnalyticsMetrics;
  delta: Record<string, ComparisonMetricDelta>;
  combined: Record<string, unknown> | null;
};
