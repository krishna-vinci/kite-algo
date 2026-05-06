import { apiFetch } from "@/lib/api/client";
import type {
  AnalyticsSummaryResponse,
  CostAnalysisResponse,
  EquityCurveResponse,
  MetricPeriod,
  PaperLiveComparisonResponse,
  StrategyDeepDiveResponse,
} from "./types";

// Re-export all response types for consumer convenience
export type {
  AnalyticsSummaryResponse,
  CostAnalysisResponse,
  EquityCurveResponse,
  MetricPeriod,
  PaperLiveComparisonResponse,
  StrategyDeepDiveResponse,
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function toSearchParams(params: Record<string, string | undefined>): string {
  const entries = Object.entries(params).filter((e): e is [string, string] => e[1] !== undefined);
  return entries.length ? `?${new URLSearchParams(entries).toString()}` : "";
}

// ---------------------------------------------------------------------------
// Analytics v1 helpers
// ---------------------------------------------------------------------------

/** GET /api/analytics/v1/summary */
export async function fetchAnalyticsSummary(params: {
  environment_id: string;
  period?: MetricPeriod;
  date?: string;
}): Promise<AnalyticsSummaryResponse> {
  return apiFetch<AnalyticsSummaryResponse>(
    `/api/analytics/v1/summary${toSearchParams({
      environment_id: params.environment_id,
      period: params.period,
      date: params.date,
    })}`,
  );
}

/** GET /api/analytics/v1/strategy/{template_id} */
export async function fetchStrategyDeepDive(params: {
  environment_id: string;
  template_id: string;
  period?: MetricPeriod;
  date?: string;
}): Promise<StrategyDeepDiveResponse> {
  return apiFetch<StrategyDeepDiveResponse>(
    `/api/analytics/v1/strategy/${params.template_id}${toSearchParams({
      environment_id: params.environment_id,
      period: params.period,
      date: params.date,
    })}`,
  );
}

/** GET /api/analytics/v1/equity-curve */
export async function fetchEquityCurve(params: {
  environment_id: string;
  period?: MetricPeriod;
  date?: string;
  template_id?: string;
}): Promise<EquityCurveResponse> {
  return apiFetch<EquityCurveResponse>(
    `/api/analytics/v1/equity-curve${toSearchParams({
      environment_id: params.environment_id,
      period: params.period,
      date: params.date,
      template_id: params.template_id,
    })}`,
  );
}

/** GET /api/analytics/v1/cost-analysis */
export async function fetchCostAnalysis(params: {
  environment_id: string;
  period?: MetricPeriod;
  date?: string;
}): Promise<CostAnalysisResponse> {
  return apiFetch<CostAnalysisResponse>(
    `/api/analytics/v1/cost-analysis${toSearchParams({
      environment_id: params.environment_id,
      period: params.period,
      date: params.date,
    })}`,
  );
}

/** GET /api/analytics/v1/compare */
export async function fetchPaperLiveCompare(params: {
  template_id: string;
  paper_environment_id: string;
  live_environment_id: string;
  period?: MetricPeriod;
  date?: string;
}): Promise<PaperLiveComparisonResponse> {
  return apiFetch<PaperLiveComparisonResponse>(
    `/api/analytics/v1/compare${toSearchParams({
      template_id: params.template_id,
      paper_environment_id: params.paper_environment_id,
      live_environment_id: params.live_environment_id,
      period: params.period,
      date: params.date,
    })}`,
  );
}
