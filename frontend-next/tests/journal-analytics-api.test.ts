import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchDailyView,
  fetchEpisodeDetail,
  fetchJournalStrategies,
  fetchPeriodView,
  patchEpisodeNotes,
} from "@/lib/journal/api-v2";

import {
  fetchAnalyticsSummary,
  fetchCostAnalysis,
  fetchEquityCurve,
  fetchPaperLiveCompare,
  fetchStrategyDeepDive,
} from "@/lib/analytics/api";

const apiFetchMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/client", () => ({
  apiFetch: apiFetchMock,
}));

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

const ENV_REF = {
  environment_id: "env-1",
  mode: "paper",
  account_scope: "kite:paper",
  display_name: null,
  broker_user_id: null,
  paper_account_key: null,
};

const EMPTY_METRICS = {
  gross_pnl: "0",
  net_pnl: "0",
  total_charges: "0",
  realized_pnl: "0",
  cost_breakdown: {},
  cost_ratio: null,
  closed_episode_count: 0,
  hold_seconds_total: 0,
  hold_seconds_avg: null,
  win_count: 0,
  loss_count: 0,
  win_rate: null,
  average_win: null,
  average_loss: null,
  expectancy: null,
  profit_factor: null,
  sharpe_ratio: null,
  sortino_ratio: null,
  max_drawdown: null,
  max_drawdown_duration_days: null,
  cumulative_return: null,
  max_win_streak: 0,
  max_loss_streak: 0,
  mae: null,
  mfe: null,
  r_multiple: null,
};

// ---------------------------------------------------------------------------
// Journal v2 helpers
// ---------------------------------------------------------------------------

describe("journal v2 API helpers (api-v2.ts)", () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("fetchDailyView — passes environment_id and date as query params", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      trading_date: "2026-05-04",
      summary: { trading_date: "2026-05-04", metrics: EMPTY_METRICS, closed_episode_count: 0, open_episode_count: 0, strategy_count: 0, notes_count: 0 },
      strategy_groups: [],
      open_episodes: [],
    });

    const result = await fetchDailyView({ environment_id: "env-1", date: "2026-05-04" });

    expect(result.trading_date).toBe("2026-05-04");
    expect(apiFetchMock).toHaveBeenCalledOnce();
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/daily?environment_id=env-1&date=2026-05-04",
    );
  });

  it("fetchDailyView — omits date param when not provided", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      trading_date: "2026-05-04",
      summary: {},
      strategy_groups: [],
      open_episodes: [],
    });

    await fetchDailyView({ environment_id: "env-1" });

    expect(apiFetchMock).toHaveBeenCalledWith("/api/journal/v2/daily?environment_id=env-1");
  });

  it("fetchPeriodView — builds correct URL with from/to/granularity", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      from_date: "2026-04-01",
      to_date: "2026-04-30",
      granularity: "week",
      summary: EMPTY_METRICS,
      buckets: [],
      strategies: [],
    });

    const result = await fetchPeriodView({
      environment_id: "env-1",
      from: "2026-04-01",
      to: "2026-04-30",
      granularity: "week",
    });

    expect(result.granularity).toBe("week");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/period?environment_id=env-1&from=2026-04-01&to=2026-04-30&granularity=week",
    );
  });

  it("fetchPeriodView — omits granularity when not provided", async () => {
    apiFetchMock.mockResolvedValueOnce({});

    await fetchPeriodView({ environment_id: "env-1", from: "2026-04-01", to: "2026-04-30" });

    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/period?environment_id=env-1&from=2026-04-01&to=2026-04-30",
    );
  });

  it("fetchEpisodeDetail — builds URL with episode_id in path", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      episode: { episode_id: "ep-42", status: "closed", opened_at: "2026-05-01T10:00:00Z", closed_at: null, strategy: null, direction: null, outcome: {}, fill_count: 0, leg_count: 0, notes: "" },
      legs: [],
      fills: [],
      timeline: [],
      notes: "test note",
    });

    const result = await fetchEpisodeDetail({ environment_id: "env-1", episode_id: "ep-42" });

    expect(result.notes).toBe("test note");
    expect(result.episode.episode_id).toBe("ep-42");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/episodes/ep-42?environment_id=env-1",
    );
  });

  it("fetchJournalStrategies — builds URL with period and date", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      period: "month",
      anchor_date: "2026-05-04",
      items: [],
    });

    const result = await fetchJournalStrategies({
      environment_id: "env-1",
      period: "month",
      date: "2026-05-04",
    });

    expect(result.period).toBe("month");
    expect(result.items).toHaveLength(0);
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/strategies?environment_id=env-1&period=month&date=2026-05-04",
    );
  });

  it("fetchJournalStrategies — omits optional params when absent", async () => {
    apiFetchMock.mockResolvedValueOnce({ environment: ENV_REF, period: "since_inception", anchor_date: null, items: [] });

    await fetchJournalStrategies({ environment_id: "env-1" });

    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/strategies?environment_id=env-1",
    );
  });

  it("patchEpisodeNotes — uses PATCH with notes in body", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      episode: { episode_id: "ep-42", notes: "updated note" },
      legs: [],
      fills: [],
      timeline: [],
      notes: "updated note",
    });

    const result = await patchEpisodeNotes({
      environment_id: "env-1",
      episode_id: "ep-42",
      notes: "updated note",
    });

    expect(result.notes).toBe("updated note");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/episodes/ep-42?environment_id=env-1",
      expect.objectContaining({
        method: "PATCH",
        json: { notes: "updated note" },
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// Analytics v1 helpers
// ---------------------------------------------------------------------------

describe("analytics v1 API helpers (analytics/api.ts)", () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("fetchAnalyticsSummary — passes environment_id, period, date", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      period: "month",
      anchor_date: "2026-05-04",
      metrics: EMPTY_METRICS,
      strategies: [],
    });

    const result = await fetchAnalyticsSummary({
      environment_id: "env-1",
      period: "month",
      date: "2026-05-04",
    });

    expect(result.period).toBe("month");
    expect(result.strategies).toHaveLength(0);
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/summary?environment_id=env-1&period=month&date=2026-05-04",
    );
  });

  it("fetchAnalyticsSummary — omits optional params", async () => {
    apiFetchMock.mockResolvedValueOnce({ environment: ENV_REF, period: "since_inception", anchor_date: null, metrics: {}, strategies: [] });

    await fetchAnalyticsSummary({ environment_id: "env-1" });

    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/summary?environment_id=env-1",
    );
  });

  it("fetchStrategyDeepDive — places template_id in URL path", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      period: "since_inception",
      anchor_date: null,
      strategy: { template_id: "tmpl-999", strategy_family: "options_strategy", template_key: null, display_name: null },
      metrics: EMPTY_METRICS,
      equity_curve: [],
    });

    const result = await fetchStrategyDeepDive({
      environment_id: "env-1",
      template_id: "tmpl-999",
    });

    expect(result.strategy.template_id).toBe("tmpl-999");
    expect(result.equity_curve).toHaveLength(0);
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/strategy/tmpl-999?environment_id=env-1",
    );
  });

  it("fetchStrategyDeepDive — passes period and date when provided", async () => {
    apiFetchMock.mockResolvedValueOnce({});

    await fetchStrategyDeepDive({
      environment_id: "env-1",
      template_id: "tmpl-1",
      period: "week",
      date: "2026-05-04",
    });

    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/strategy/tmpl-1?environment_id=env-1&period=week&date=2026-05-04",
    );
  });

  it("fetchEquityCurve — passes all optional params", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      period: "year",
      anchor_date: null,
      template_id: "tmpl-1",
      metrics: EMPTY_METRICS,
      points: [{ trading_date: "2026-01-01", realized_pnl: "100", total_charges: "5", ending_equity: "1100", starting_equity: "1000", return_pct: "10", benchmark_return_pct: null, excess_return_pct: null }],
    });

    const result = await fetchEquityCurve({
      environment_id: "env-1",
      period: "year",
      template_id: "tmpl-1",
    });

    expect(result.points).toHaveLength(1);
    expect(result.points[0].trading_date).toBe("2026-01-01");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/equity-curve?environment_id=env-1&period=year&template_id=tmpl-1",
    );
  });

  it("fetchEquityCurve — omits template_id when absent", async () => {
    apiFetchMock.mockResolvedValueOnce({ points: [] });

    await fetchEquityCurve({ environment_id: "env-1" });

    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/equity-curve?environment_id=env-1",
    );
  });

  it("fetchCostAnalysis — correct URL and passes params", async () => {
    apiFetchMock.mockResolvedValueOnce({
      environment: ENV_REF,
      period: "month",
      anchor_date: null,
      metrics: EMPTY_METRICS,
      cost_breakdown: {},
      strategies: [],
    });

    const result = await fetchCostAnalysis({ environment_id: "env-1", period: "month" });

    expect(result.strategies).toHaveLength(0);
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/cost-analysis?environment_id=env-1&period=month",
    );
  });

  it("fetchPaperLiveCompare — correct URL with all required params", async () => {
    apiFetchMock.mockResolvedValueOnce({
      template_id: "tmpl-1",
      period: "since_inception",
      anchor_date: null,
      paper_environment: null,
      live_environment: null,
      paper: EMPTY_METRICS,
      live: EMPTY_METRICS,
      delta: {},
      combined: null,
    });

    const result = await fetchPaperLiveCompare({
      template_id: "tmpl-1",
      paper_environment_id: "paper-env",
      live_environment_id: "live-env",
    });

    expect(result.template_id).toBe("tmpl-1");
    expect(result.combined).toBeNull();
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/compare?template_id=tmpl-1&paper_environment_id=paper-env&live_environment_id=live-env",
    );
  });

  it("fetchPaperLiveCompare — passes period and date when provided", async () => {
    apiFetchMock.mockResolvedValueOnce({});

    await fetchPaperLiveCompare({
      template_id: "tmpl-1",
      paper_environment_id: "paper-env",
      live_environment_id: "live-env",
      period: "month",
      date: "2026-05-01",
    });

    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/analytics/v1/compare?template_id=tmpl-1&paper_environment_id=paper-env&live_environment_id=live-env&period=month&date=2026-05-01",
    );
  });

  it("fetchPaperLiveCompare — delta is typed as record of ComparisonMetricDelta", async () => {
    apiFetchMock.mockResolvedValueOnce({
      template_id: "tmpl-1",
      period: "month",
      anchor_date: null,
      paper_environment: null,
      live_environment: null,
      paper: EMPTY_METRICS,
      live: EMPTY_METRICS,
      delta: {
        net_pnl: { paper: "100", live: "80", delta: "-20", deviation_pct: "-20" },
      },
      combined: null,
    });

    const result = await fetchPaperLiveCompare({
      template_id: "tmpl-1",
      paper_environment_id: "paper-env",
      live_environment_id: "live-env",
    });

    expect(result.delta["net_pnl"]).toMatchObject({ paper: "100", live: "80", delta: "-20" });
  });
});
