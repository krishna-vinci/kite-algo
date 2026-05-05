import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AnalyticsDashboardPage from "@/app/(app)/analytics/page";
import AnalyticsEquityPage from "@/app/(app)/analytics/equity/page";
import AnalyticsCostsPage from "@/app/(app)/analytics/costs/page";
import StrategyDeepDivePage from "@/app/(app)/analytics/strategies/[templateId]/page";
import { WorkspaceProvider } from "@/components/workspace/workspace-provider";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

// ---------------------------------------------------------------------------
// Hoisted mocks
// ---------------------------------------------------------------------------

const fetchJournalEnvironmentsMock = vi.hoisted(() => vi.fn());
const fetchAnalyticsSummaryMock = vi.hoisted(() => vi.fn());
const fetchEquityCurveMock = vi.hoisted(() => vi.fn());
const fetchCostAnalysisMock = vi.hoisted(() => vi.fn());
const fetchStrategyDeepDiveMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/journal/api", () => ({
  fetchJournalEnvironments: fetchJournalEnvironmentsMock,
}));

vi.mock("@/lib/analytics/api", () => ({
  fetchAnalyticsSummary: fetchAnalyticsSummaryMock,
  fetchEquityCurve: fetchEquityCurveMock,
  fetchCostAnalysis: fetchCostAnalysisMock,
  fetchStrategyDeepDive: fetchStrategyDeepDiveMock,
  fetchPaperLiveCompare: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => window.location.pathname,
  useParams: () => ({ templateId: "tmpl-1" }),
}));

// lightweight-charts creates a canvas — stub it in jsdom
vi.mock("lightweight-charts", () => ({
  createChart: () => ({
    addSeries: () => ({ setData: vi.fn() }),
    applyOptions: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: vi.fn(),
  }),
  LineSeries: {},
  ColorType: { Solid: "solid" },
  LineStyle: { Dashed: 1, Dotted: 2 },
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ENV_FIXTURE = {
  id: "env-1",
  mode: "paper",
  account_scope: "kite:paper-e2e",
  display_name: "Paper E2E",
  broker_user_id: null,
  paper_account_key: "kite:paper-e2e",
  environment_epoch: 1,
  metadata: {},
};

const METRICS_FIXTURE = {
  gross_pnl: "45000.00",
  net_pnl: "42000.00",
  total_charges: "3000.00",
  realized_pnl: "42000.00",
  cost_breakdown: {
    brokerage: "800.00",
    exchange_txn_charge: "600.00",
    stt: "1000.00",
    stamp_duty: "200.00",
    sebi_charge: "10.00",
    gst: "390.00",
    total_taxes: "2200.00",
    total_charges: "3000.00",
  },
  cost_ratio: "6.7",
  closed_episode_count: 25,
  hold_seconds_total: 90000,
  hold_seconds_avg: 3600,
  win_count: 16,
  loss_count: 9,
  win_rate: "64.0",
  average_win: "3500.00",
  average_loss: "-1200.00",
  expectancy: "1700.00",
  profit_factor: "2.91",
  sharpe_ratio: "1.82",
  sortino_ratio: "2.10",
  max_drawdown: "-8.4",
  max_drawdown_duration_days: 3,
  cumulative_return: "4.2",
  max_win_streak: 5,
  max_loss_streak: 3,
  mae: "-500.00",
  mfe: "5200.00",
  r_multiple: "2.1",
};

const SUMMARY_FIXTURE = {
  environment: { environment_id: "env-1", mode: "paper", display_name: "Paper E2E", account_scope: "kite:paper-e2e", broker_user_id: null, paper_account_key: "kite:paper-e2e" },
  period: "month",
  anchor_date: null,
  metrics: METRICS_FIXTURE,
  strategies: [
    {
      strategy: { template_id: "tmpl-1", strategy_family: "options_strategy", template_key: "iron_condor", display_name: "Iron Condor" },
      metrics: METRICS_FIXTURE,
    },
  ],
};

const EQUITY_FIXTURE = {
  environment: { environment_id: "env-1", mode: "paper", display_name: "Paper E2E", account_scope: "kite:paper-e2e", broker_user_id: null, paper_account_key: "kite:paper-e2e" },
  period: "month",
  anchor_date: null,
  template_id: null,
  metrics: METRICS_FIXTURE,
  points: [
    { trading_date: "2026-04-01", realized_pnl: "1200.00", total_charges: "80.00", ending_equity: null, starting_equity: null, return_pct: "0.4", benchmark_return_pct: "0.2", excess_return_pct: "0.2" },
    { trading_date: "2026-04-02", realized_pnl: "2100.00", total_charges: "120.00", ending_equity: null, starting_equity: null, return_pct: "0.7", benchmark_return_pct: "0.3", excess_return_pct: "0.4" },
  ],
};

const DEEP_DIVE_FIXTURE = {
  environment: { environment_id: "env-1", mode: "paper", display_name: "Paper E2E", account_scope: "kite:paper-e2e", broker_user_id: null, paper_account_key: "kite:paper-e2e" },
  period: "month",
  anchor_date: null,
  strategy: { template_id: "tmpl-1", strategy_family: "options_strategy", template_key: "iron_condor", display_name: "Iron Condor" },
  metrics: METRICS_FIXTURE,
  equity_curve: [
    { trading_date: "2026-04-01", realized_pnl: "1200.00", total_charges: "80.00", ending_equity: null, starting_equity: null, return_pct: "0.4", benchmark_return_pct: "0.2", excess_return_pct: "0.2" },
    { trading_date: "2026-04-02", realized_pnl: "2100.00", total_charges: "120.00", ending_equity: null, starting_equity: null, return_pct: "0.7", benchmark_return_pct: "0.3", excess_return_pct: "0.4" },
  ],
};

const COST_ANALYSIS_FIXTURE = {
  environment: { environment_id: "env-1", mode: "paper", display_name: "Paper E2E", account_scope: "kite:paper-e2e", broker_user_id: null, paper_account_key: "kite:paper-e2e" },
  period: "month",
  anchor_date: null,
  metrics: METRICS_FIXTURE,
  cost_breakdown: METRICS_FIXTURE.cost_breakdown,
  strategies: [
    {
      strategy: { template_id: "tmpl-1", strategy_family: "options_strategy", template_key: "iron_condor", display_name: "Iron Condor" },
      cost_breakdown: METRICS_FIXTURE.cost_breakdown,
      total_charges: "3000.00",
      cost_ratio: "6.7",
      closed_episode_count: 25,
    },
  ],
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function withWorkspace(ui: React.ReactElement) {
  return renderWithQueryClient(<WorkspaceProvider>{ui}</WorkspaceProvider>);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("analytics pages", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");

    fetchJournalEnvironmentsMock.mockReset();
    fetchAnalyticsSummaryMock.mockReset();
    fetchEquityCurveMock.mockReset();
    fetchCostAnalysisMock.mockReset();
    fetchStrategyDeepDiveMock.mockReset();

    fetchJournalEnvironmentsMock.mockResolvedValue([ENV_FIXTURE]);
  });

  // ── Dashboard ────────────────────────────────────────────────────────────

  it("dashboard: shows placeholder when no environment selected", () => {
    window.history.pushState({}, "", "/analytics");
    withWorkspace(<AnalyticsDashboardPage />);

    expect(screen.getByText(/Select an environment/i)).toBeInTheDocument();
    expect(fetchAnalyticsSummaryMock).not.toHaveBeenCalled();
  });

  it("dashboard: fetches summary and renders KPI cards", async () => {
    window.history.pushState({}, "", "/analytics?env=env-1&period=month");
    fetchAnalyticsSummaryMock.mockResolvedValue(SUMMARY_FIXTURE);

    withWorkspace(<AnalyticsDashboardPage />);

    await waitFor(() =>
      expect(fetchAnalyticsSummaryMock).toHaveBeenCalledWith(
        expect.objectContaining({ environment_id: "env-1", period: "month" }),
      ),
    );

    await waitFor(() => expect(screen.getAllByText(/Net P&L/i).length).toBeGreaterThanOrEqual(1));
    expect(screen.getAllByText(/Win Rate/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Sharpe Ratio/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Max Drawdown/i).length).toBeGreaterThanOrEqual(1);
  });

  it("dashboard: renders strategy breakdown table with formatted values", async () => {
    window.history.pushState({}, "", "/analytics?env=env-1&period=month");
    fetchAnalyticsSummaryMock.mockResolvedValue(SUMMARY_FIXTURE);

    withWorkspace(<AnalyticsDashboardPage />);

    await waitFor(() => expect(fetchAnalyticsSummaryMock).toHaveBeenCalled());

    await waitFor(() => expect(screen.getByText(/Iron Condor/i)).toBeInTheDocument());
    expect(screen.getByText(/Strategy Breakdown/i)).toBeInTheDocument();
    // win rate formatted
    expect(screen.getAllByText(/64\.0%/).length).toBeGreaterThanOrEqual(1);
  });

  it("dashboard: shows error state when fetch fails", async () => {
    window.history.pushState({}, "", "/analytics?env=env-1&period=month");
    fetchAnalyticsSummaryMock.mockRejectedValue(new Error("Server error"));

    withWorkspace(<AnalyticsDashboardPage />);

    await waitFor(() =>
      expect(screen.getByText(/Failed to load analytics/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Server error/i)).toBeInTheDocument();
  });

  // ── Equity curve ─────────────────────────────────────────────────────────

  it("equity: shows placeholder when no environment selected", () => {
    window.history.pushState({}, "", "/analytics/equity");
    withWorkspace(<AnalyticsEquityPage />);

    expect(screen.getByText(/Select an environment/i)).toBeInTheDocument();
    expect(fetchEquityCurveMock).not.toHaveBeenCalled();
  });

  it("equity: fetches data and renders chart container", async () => {
    window.history.pushState({}, "", "/analytics/equity?env=env-1&period=month");
    fetchEquityCurveMock.mockResolvedValue(EQUITY_FIXTURE);

    withWorkspace(<AnalyticsEquityPage />);

    await waitFor(() =>
      expect(fetchEquityCurveMock).toHaveBeenCalledWith(
        expect.objectContaining({ environment_id: "env-1", period: "month" }),
      ),
    );

    await waitFor(() => expect(screen.getByText(/Equity Curve/i)).toBeInTheDocument());
  });

  it("equity: shows error state when fetch fails", async () => {
    window.history.pushState({}, "", "/analytics/equity?env=env-1&period=month");
    fetchEquityCurveMock.mockRejectedValue(new Error("Timeout"));

    withWorkspace(<AnalyticsEquityPage />);

    await waitFor(() =>
      expect(screen.getByText(/Failed to load equity curve/i)).toBeInTheDocument(),
    );
  });

  // ── Costs ─────────────────────────────────────────────────────────────────

  it("costs: shows placeholder when no environment selected", () => {
    window.history.pushState({}, "", "/analytics/costs");
    withWorkspace(<AnalyticsCostsPage />);

    expect(screen.getByText(/Select an environment/i)).toBeInTheDocument();
    expect(fetchCostAnalysisMock).not.toHaveBeenCalled();
  });

  it("costs: fetches data and renders cost breakdown table", async () => {
    window.history.pushState({}, "", "/analytics/costs?env=env-1&period=month");
    fetchCostAnalysisMock.mockResolvedValue(COST_ANALYSIS_FIXTURE);

    withWorkspace(<AnalyticsCostsPage />);

    await waitFor(() =>
      expect(fetchCostAnalysisMock).toHaveBeenCalledWith(
        expect.objectContaining({ environment_id: "env-1", period: "month" }),
      ),
    );

    await waitFor(() => expect(screen.getByText(/Environment Total/i)).toBeInTheDocument());
    expect(screen.getByText(/Brokerage/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Per Strategy/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Iron Condor/i)).toBeInTheDocument();
  });

  it("costs: shows error state when fetch fails", async () => {
    window.history.pushState({}, "", "/analytics/costs?env=env-1&period=month");
    fetchCostAnalysisMock.mockRejectedValue(new Error("Network error"));

    withWorkspace(<AnalyticsCostsPage />);

    await waitFor(() =>
      expect(screen.getByText(/Failed to load cost analysis/i)).toBeInTheDocument(),
    );
  });

  it("costs: shows cost ratio per strategy", async () => {
    window.history.pushState({}, "", "/analytics/costs?env=env-1&period=month");
    fetchCostAnalysisMock.mockResolvedValue(COST_ANALYSIS_FIXTURE);

    withWorkspace(<AnalyticsCostsPage />);

    await waitFor(() => expect(fetchCostAnalysisMock).toHaveBeenCalled());

    await waitFor(() => expect(screen.getByText(/6\.7%/)).toBeInTheDocument());
  });

  // ── Strategy deep-dive ────────────────────────────────────────────────────

  it("strategy: shows placeholder when no environment selected", () => {
    window.history.pushState({}, "", "/analytics/strategies/tmpl-1");
    withWorkspace(<StrategyDeepDivePage />);

    expect(screen.getByText(/Select an environment/i)).toBeInTheDocument();
    expect(fetchStrategyDeepDiveMock).not.toHaveBeenCalled();
  });

  it("strategy: fetches data and renders strategy name + metrics", async () => {
    window.history.pushState({}, "", "/analytics/strategies/tmpl-1?env=env-1&period=month");
    fetchStrategyDeepDiveMock.mockResolvedValue(DEEP_DIVE_FIXTURE);

    withWorkspace(<StrategyDeepDivePage />);

    await waitFor(() =>
      expect(fetchStrategyDeepDiveMock).toHaveBeenCalledWith(
        expect.objectContaining({ environment_id: "env-1", template_id: "tmpl-1", period: "month" }),
      ),
    );

    await waitFor(() => expect(screen.getByText(/Iron Condor/i)).toBeInTheDocument());
    // metric sections rendered
    expect(screen.getByText(/Performance Breakdown/i)).toBeInTheDocument();
    expect(screen.getByText(/Win \/ Loss/i)).toBeInTheDocument();
    expect(screen.getByText(/Equity Curve/i)).toBeInTheDocument();
  });

  it("strategy: shows error state when fetch fails", async () => {
    window.history.pushState({}, "", "/analytics/strategies/tmpl-1?env=env-1&period=month");
    fetchStrategyDeepDiveMock.mockRejectedValue(new Error("Strategy not found"));

    withWorkspace(<StrategyDeepDivePage />);

    await waitFor(() =>
      expect(screen.getByText(/Failed to load strategy data/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Strategy not found/i)).toBeInTheDocument();
  });

  it("dashboard: strategy row links to deep-dive page with params", async () => {
    window.history.pushState({}, "", "/analytics?env=env-1&mode=paper&period=month");
    fetchAnalyticsSummaryMock.mockResolvedValue(SUMMARY_FIXTURE);

    withWorkspace(<AnalyticsDashboardPage />);

    await waitFor(() => expect(screen.getByText(/Iron Condor/i)).toBeInTheDocument());

    const link = screen.getByRole("link", { name: /Iron Condor/i });
    expect(link).toHaveAttribute("href", expect.stringContaining("/analytics/strategies/tmpl-1"));
    expect(link).toHaveAttribute("href", expect.stringContaining("env=env-1"));
    expect(link).toHaveAttribute("href", expect.stringContaining("period=month"));
  });
});
