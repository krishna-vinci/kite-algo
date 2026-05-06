import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import JournalEpisodeDetailPage from "@/app/(app)/journal/episodes/[episodeId]/page";
import JournalDayPage from "@/app/(app)/journal/page";
import JournalWeekPage from "@/app/(app)/journal/week/page";
import JournalMonthPage from "@/app/(app)/journal/month/page";
import { EnvironmentSelector } from "@/components/journal/environment-selector";
import { JournalWorkspaceProvider, useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

const fetchJournalEnvironmentsMock = vi.hoisted(() => vi.fn());
const fetchJournalEpisodesMock = vi.hoisted(() => vi.fn());
const fetchJournalEpisodeMock = vi.hoisted(() => vi.fn());
const fetchDailyViewMock = vi.hoisted(() => vi.fn());
const fetchPeriodViewMock = vi.hoisted(() => vi.fn());
const fetchEpisodeDetailMock = vi.hoisted(() => vi.fn());
const patchEpisodeNotesMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/journal/api", () => ({
  fetchJournalEnvironments: fetchJournalEnvironmentsMock,
  fetchJournalEpisodes: fetchJournalEpisodesMock,
  fetchJournalEpisode: fetchJournalEpisodeMock,
}));

vi.mock("@/lib/journal/api-v2", () => ({
  fetchDailyView: fetchDailyViewMock,
  fetchPeriodView: fetchPeriodViewMock,
  fetchEpisodeDetail: fetchEpisodeDetailMock,
  patchEpisodeNotes: patchEpisodeNotesMock,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => window.location.pathname,
}));

describe("journal v2 pages", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");

    fetchJournalEnvironmentsMock.mockReset();
    fetchJournalEpisodesMock.mockReset();
    fetchJournalEpisodeMock.mockReset();
    fetchDailyViewMock.mockReset();
    fetchPeriodViewMock.mockReset();
    fetchEpisodeDetailMock.mockReset();
    patchEpisodeNotesMock.mockReset();

    fetchEpisodeDetailMock.mockResolvedValue({
      episode: {
        episode_id: "ep-1",
        environment_id: "env-1",
        status: "open",
        direction: null,
        opened_at: "2026-05-01T10:00:00Z",
        closed_at: null,
        fill_count: 0,
        leg_count: 0,
        strategy: null,
        outcome: null,
      },
      legs: [],
      fills: [],
      timeline: [],
      notes: "",
    });
    patchEpisodeNotesMock.mockResolvedValue({ notes: "" });

    fetchJournalEnvironmentsMock.mockResolvedValue([
      {
        id: "env-1",
        mode: "paper",
        account_scope: "kite:paper-e2e",
        display_name: "Paper E2E",
        broker_user_id: null,
        paper_account_key: "kite:paper-e2e",
        environment_epoch: 1,
        metadata: {},
      },
    ]);
    fetchJournalEpisodesMock.mockResolvedValue([]);
    fetchJournalEpisodeMock.mockResolvedValue({
      id: "ep-1",
      environment_id: "env-1",
      execution_context_id: "ctx-1",
      episode_seq: 1,
      status: "open",
      opened_at: "2026-05-01T10:00:00Z",
      closed_at: null,
      metadata: {},
    });
  });

  function WorkspaceEnvSelector() {
    const { environments, selectedEnvironmentId, setSelectedEnvironmentId } = useJournalWorkspace();
    return (
      <EnvironmentSelector
        environments={environments}
        selectedEnvironmentId={selectedEnvironmentId}
        onSelectEnvironment={setSelectedEnvironmentId}
      />
    );
  }

  it("preserves selected Journal environment across shared workspace renders", async () => {
    // Need ≥2 environments so EnvironmentSelector renders (it hides when ≤1)
    fetchJournalEnvironmentsMock.mockResolvedValue([
      {
        id: "env-1",
        mode: "paper",
        account_scope: "kite:paper-e2e",
        display_name: "Paper E2E",
        broker_user_id: null,
        paper_account_key: "kite:paper-e2e",
        environment_epoch: 1,
        metadata: {},
      },
      {
        id: "env-2",
        mode: "paper",
        account_scope: "kite:paper-b",
        display_name: "Paper B",
        broker_user_id: null,
        paper_account_key: "kite:paper-b",
        environment_epoch: 1,
        metadata: {},
      },
    ]);
    window.history.pushState({}, "", "/journal?environment_id=env-1");

    render(
      <JournalWorkspaceProvider>
        <WorkspaceEnvSelector />
        <WorkspaceEnvSelector />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalEnvironmentsMock).toHaveBeenCalled());
    const selectors = screen.getAllByRole("combobox", { name: /Environment/i });
    expect(selectors).toHaveLength(2);
    for (const selector of selectors) {
      expect(selector).toHaveTextContent("Paper E2E");
    }
  });

  it("shows day view KPI cards and episodes when environment is selected", async () => {
    window.history.pushState({}, "", "/journal?env=env-1&date=2026-05-01");
    fetchDailyViewMock.mockResolvedValue({
      date: "2026-05-01",
      environment: { environment_id: "env-1", name: "Paper 1", mode: "paper" },
      summary: {
        open_episode_count: 1,
        metrics: {
          net_pnl: "1200.50",
          gross_pnl: "1350.00",
          total_charges: "149.50",
          closed_episode_count: 2,
          win_rate: "66.7",
          cost_breakdown: {
            brokerage: "40.00",
            exchange_txn_charge: "30.00",
            stt: "50.00",
            stamp_duty: "10.00",
            sebi_charge: "0.50",
            gst: "9.00",
            total_taxes: "99.50",
            total_charges: "149.50",
          },
        },
      },
      open_episodes: [
        {
          episode_id: "open-ep-1",
          strategy: { template_id: "strat-a", template_key: "iron_condor", display_name: "Iron Condor" },
          direction: "neutral",
          current_pnl_estimate: "220.00",
          opened_at: "2026-05-01T09:30:00Z",
        },
      ],
      strategy_groups: [
        {
          strategy: { template_id: "strat-a", template_key: "iron_condor", display_name: "Iron Condor" },
          metrics: { net_pnl: "1200.50", gross_pnl: "1350.00", total_charges: "149.50", closed_episode_count: 2, win_rate: "66.7", cost_breakdown: { brokerage: "40.00", exchange_txn_charge: "30.00", stt: "50.00", stamp_duty: "10.00", sebi_charge: "0.50", gst: "9.00", total_taxes: "99.50", total_charges: "149.50" } },
          episodes: [
            {
              episode_id: "ep-closed-1",
              strategy: { template_id: "strat-a", template_key: "iron_condor", display_name: "Iron Condor" },
              direction: "neutral",
              outcome: { net_pnl: "800.00", gross_pnl: "900.00", total_charges: "100.00" },
              opened_at: "2026-05-01T09:30:00Z",
              closed_at: "2026-05-01T14:00:00Z",
            },
          ],
        },
      ],
    });

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalDayPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() =>
      expect(fetchDailyViewMock).toHaveBeenCalledWith(
        expect.objectContaining({ environment_id: "env-1", date: expect.any(String) }),
      ),
    );

    // KPI headings
    expect(screen.getByText(/Net P&L/i)).toBeInTheDocument();
    expect(screen.getByText(/Gross P&L/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Total Charges/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Episodes/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Win Rate/i)).toBeInTheDocument();

    // Open episodes section
    expect(screen.getByText(/Open Episodes/i)).toBeInTheDocument();
    // Closed episodes section
    expect(screen.getByText(/Closed Episodes/i)).toBeInTheDocument();
    // Cost breakdown
    expect(screen.getByText(/Cost Breakdown/i)).toBeInTheDocument();
  });

  // ── Week view ────────────────────────────────────────────────────────────

  const makePeriodResponse = (granularity: string) => ({
    environment: { environment_id: "env-1", mode: "paper", display_name: "Paper E2E", account_scope: "kite:paper-e2e", broker_user_id: null, paper_account_key: "kite:paper-e2e" },
    from_date: "2026-04-27",
    to_date: "2026-05-03",
    granularity,
    summary: {
      gross_pnl: "3200.00",
      net_pnl: "2950.00",
      total_charges: "250.00",
      realized_pnl: "2950.00",
      cost_breakdown: {
        brokerage: "80.00",
        exchange_txn_charge: "60.00",
        stt: "70.00",
        stamp_duty: "10.00",
        sebi_charge: "1.00",
        gst: "29.00",
        total_taxes: "170.00",
        total_charges: "250.00",
      },
      cost_ratio: "7.8",
      closed_episode_count: 5,
      hold_seconds_total: 7200,
      hold_seconds_avg: 1440,
      win_count: 3,
      loss_count: 2,
      win_rate: "60.0",
      average_win: "1200.00",
      average_loss: "-600.00",
      expectancy: "480.00",
      profit_factor: "3.0",
      sharpe_ratio: "1.4",
      sortino_ratio: "1.8",
      max_drawdown: "-500.00",
      max_drawdown_duration_days: 1,
      cumulative_return: "2.95",
      max_win_streak: 2,
      max_loss_streak: 1,
      mae: "-200.00",
      mfe: "1500.00",
      r_multiple: "1.6",
    },
    buckets: [
      {
        bucket_start: "2026-04-28",
        bucket_end: "2026-04-28",
        label: granularity === "week" ? "28 Apr – 03 May" : "",
        metrics: {
          gross_pnl: "1000.00",
          net_pnl: "900.00",
          total_charges: "100.00",
          realized_pnl: "900.00",
          cost_breakdown: {
            brokerage: "40.00",
            exchange_txn_charge: "30.00",
            stt: "20.00",
            stamp_duty: "5.00",
            sebi_charge: "0.50",
            gst: "4.50",
            total_taxes: "50.00",
            total_charges: "100.00",
          },
          cost_ratio: "10.0",
          closed_episode_count: 2,
          hold_seconds_total: 3600,
          hold_seconds_avg: 1800,
          win_count: 2,
          loss_count: 0,
          win_rate: "100.0",
          average_win: "500.00",
          average_loss: null,
          expectancy: "500.00",
          profit_factor: null,
          sharpe_ratio: null,
          sortino_ratio: null,
          max_drawdown: null,
          max_drawdown_duration_days: null,
          cumulative_return: null,
          max_win_streak: 2,
          max_loss_streak: 0,
          mae: null,
          mfe: null,
          r_multiple: null,
        },
        closed_episode_count: 2,
      },
    ],
    strategies: [
      {
        strategy: { template_id: "strat-a", strategy_family: "options", template_key: "iron_condor", display_name: "Iron Condor" },
        metrics: {
          gross_pnl: "3200.00",
          net_pnl: "2950.00",
          total_charges: "250.00",
          realized_pnl: "2950.00",
          cost_breakdown: {
            brokerage: "80.00",
            exchange_txn_charge: "60.00",
            stt: "70.00",
            stamp_duty: "10.00",
            sebi_charge: "1.00",
            gst: "29.00",
            total_taxes: "170.00",
            total_charges: "250.00",
          },
          cost_ratio: "7.8",
          closed_episode_count: 5,
          hold_seconds_total: 7200,
          hold_seconds_avg: 1440,
          win_count: 3,
          loss_count: 2,
          win_rate: "60.0",
          average_win: "1200.00",
          average_loss: "-600.00",
          expectancy: "480.00",
          profit_factor: "3.0",
          sharpe_ratio: "1.4",
          sortino_ratio: "1.8",
          max_drawdown: "-500.00",
          max_drawdown_duration_days: 1,
          cumulative_return: "2.95",
          max_win_streak: 2,
          max_loss_streak: 1,
          mae: "-200.00",
          mfe: "1500.00",
          r_multiple: "1.6",
        },
        episode_count: 5,
      },
    ],
  });

  it("week view: shows KPI cards, daily breakdown with day links, and strategy table", async () => {
    window.history.pushState({}, "", "/journal/week?env=env-1&date=2026-04-28");
    fetchPeriodViewMock.mockResolvedValue(makePeriodResponse("day"));

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalWeekPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() =>
      expect(fetchPeriodViewMock).toHaveBeenCalledWith(
        expect.objectContaining({
          environment_id: "env-1",
          granularity: "day",
          from: expect.any(String),
          to: expect.any(String),
        }),
      ),
    );

    // KPI labels
    expect(screen.getAllByText(/Net P&L/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Gross P&L/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Total Charges/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Win Rate/i).length).toBeGreaterThanOrEqual(1);

    // Daily breakdown section
    expect(screen.getByText(/Daily Breakdown/i)).toBeInTheDocument();

    // Day link points to /journal
    const dayLinks = screen.getAllByRole("link");
    const journalDayLink = dayLinks.find((l) =>
      (l.getAttribute("href") ?? "").startsWith("/journal?"),
    );
    expect(journalDayLink).toBeDefined();
    expect(journalDayLink!.getAttribute("href")).toContain("env=env-1");

    // Strategy table
    expect(screen.getByText(/Strategy Performance/i)).toBeInTheDocument();
    expect(screen.getByText(/Iron Condor/i)).toBeInTheDocument();

    // Cost breakdown
    expect(screen.getByText(/Cost Breakdown/i)).toBeInTheDocument();
  });

  it("week view: shows empty state when no buckets and no episodes", async () => {
    window.history.pushState({}, "", "/journal/week?env=env-1&date=2026-04-28");
    fetchPeriodViewMock.mockResolvedValue({
      environment: { environment_id: "env-1", mode: "paper", display_name: "Paper E2E", account_scope: "kite:paper-e2e", broker_user_id: null, paper_account_key: "kite:paper-e2e" },
      from_date: "2026-04-28",
      to_date: "2026-05-04",
      granularity: "day",
      summary: {
        gross_pnl: 0, net_pnl: 0, total_charges: 0, realized_pnl: 0,
        cost_breakdown: { brokerage: 0, exchange_txn_charge: 0, stt: 0, stamp_duty: 0, sebi_charge: 0, gst: 0, total_taxes: 0, total_charges: 0 },
        cost_ratio: null, closed_episode_count: 0, hold_seconds_total: 0, hold_seconds_avg: null,
        win_count: 0, loss_count: 0, win_rate: null, average_win: null, average_loss: null,
        expectancy: null, profit_factor: null, sharpe_ratio: null, sortino_ratio: null,
        max_drawdown: null, max_drawdown_duration_days: null, cumulative_return: null,
        max_win_streak: 0, max_loss_streak: 0, mae: null, mfe: null, r_multiple: null,
      },
      buckets: [],
      strategies: [],
    });

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalWeekPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchPeriodViewMock).toHaveBeenCalled());
    expect(screen.getByText(/No trading activity for/i)).toBeInTheDocument();
  });

  it("week view: shows error alert when fetch fails", async () => {
    window.history.pushState({}, "", "/journal/week?env=env-1&date=2026-04-28");
    fetchPeriodViewMock.mockRejectedValue(new Error("Network error"));

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalWeekPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() =>
      expect(screen.getByText(/Failed to load week view/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Network error/i)).toBeInTheDocument();
  });

  it("week view: shows placeholder when no environment is selected", () => {
    window.history.pushState({}, "", "/journal/week");
    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalWeekPage />
      </JournalWorkspaceProvider>,
    );

    expect(screen.getByText(/Select an environment/i)).toBeInTheDocument();
  });

  // ── Month view ───────────────────────────────────────────────────────────

  it("month view: shows KPI cards, week breakdown with week links, strategy table, and cost panel", async () => {
    window.history.pushState({}, "", "/journal/month?env=env-1&date=2026-04-15");
    fetchPeriodViewMock.mockResolvedValue(makePeriodResponse("week"));

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalMonthPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() =>
      expect(fetchPeriodViewMock).toHaveBeenCalledWith(
        expect.objectContaining({
          environment_id: "env-1",
          granularity: "week",
          from: "2026-04-01",
          to: "2026-04-30",
        }),
      ),
    );

    // KPI labels (extended grid)
    expect(screen.getAllByText(/Net P&L/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Profit Factor/i)).toBeInTheDocument();

    // Weekly breakdown section
    expect(screen.getByText(/Weekly Breakdown/i)).toBeInTheDocument();

    // Week link points to /journal/week
    const weekLinks = screen.getAllByRole("link");
    const weekLink = weekLinks.find((l) =>
      (l.getAttribute("href") ?? "").startsWith("/journal/week?"),
    );
    expect(weekLink).toBeDefined();
    expect(weekLink!.getAttribute("href")).toContain("env=env-1");

    // Strategy table
    expect(screen.getByText(/Strategy Performance/i)).toBeInTheDocument();
    expect(screen.getByText(/Iron Condor/i)).toBeInTheDocument();

    // Monthly cost breakdown
    expect(screen.getByText(/Monthly Cost Breakdown/i)).toBeInTheDocument();
    expect(screen.getByText(/Brokerage/i)).toBeInTheDocument();
  });

  it("month view: shows empty state for months with no activity", async () => {
    window.history.pushState({}, "", "/journal/month?env=env-1&date=2026-02-15");
    fetchPeriodViewMock.mockResolvedValue({
      environment: { environment_id: "env-1", mode: "paper", display_name: "Paper E2E", account_scope: "kite:paper-e2e", broker_user_id: null, paper_account_key: "kite:paper-e2e" },
      from_date: "2026-02-01",
      to_date: "2026-02-28",
      granularity: "week",
      summary: {
        gross_pnl: 0, net_pnl: 0, total_charges: 0, realized_pnl: 0,
        cost_breakdown: { brokerage: 0, exchange_txn_charge: 0, stt: 0, stamp_duty: 0, sebi_charge: 0, gst: 0, total_taxes: 0, total_charges: 0 },
        cost_ratio: null, closed_episode_count: 0, hold_seconds_total: 0, hold_seconds_avg: null,
        win_count: 0, loss_count: 0, win_rate: null, average_win: null, average_loss: null,
        expectancy: null, profit_factor: null, sharpe_ratio: null, sortino_ratio: null,
        max_drawdown: null, max_drawdown_duration_days: null, cumulative_return: null,
        max_win_streak: 0, max_loss_streak: 0, mae: null, mfe: null, r_multiple: null,
      },
      buckets: [],
      strategies: [],
    });

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalMonthPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchPeriodViewMock).toHaveBeenCalled());
    expect(screen.getByText(/No trading activity for/i)).toBeInTheDocument();
  });

  it("month view: shows error alert when fetch fails", async () => {
    window.history.pushState({}, "", "/journal/month?env=env-1&date=2026-04-15");
    fetchPeriodViewMock.mockRejectedValue(new Error("Server error"));

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalMonthPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() =>
      expect(screen.getByText(/Failed to load month view/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Server error/i)).toBeInTheDocument();
  });

  it("month view: shows placeholder when no environment is selected", () => {
    window.history.pushState({}, "", "/journal/month");
    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalMonthPage />
      </JournalWorkspaceProvider>,
    );

    expect(screen.getByText(/Select an environment/i)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // Episode detail page (v2)
  // ---------------------------------------------------------------------------

  const EPISODE_DETAIL_FIXTURE = {
    episode: {
      episode_id: "ep-uuid-001",
      environment_id: "env-1",
      status: "closed",
      direction: "long",
      opened_at: "2026-04-10T09:15:00Z",
      closed_at: "2026-04-10T15:30:00Z",
      fill_count: 2,
      leg_count: 1,
      strategy: {
        strategy_id: "strat-1",
        template_key: "momentum_v1",
        display_name: "Momentum V1",
        strategy_family: null,
      },
      outcome: {
        net_pnl: "1250.50",
        gross_pnl: "1400.00",
        total_charges: "149.50",
        realized_pnl: "1250.50",
        cost_breakdown: {
          brokerage: "40.00",
          exchange_txn_charge: "35.00",
          stt: "50.00",
          stamp_duty: "5.00",
          sebi_charge: "2.00",
          gst: "17.50",
          total_taxes: "109.50",
        },
      },
    },
    legs: [
      {
        leg_id: "leg-1",
        leg_seq: 1,
        tradingsymbol: "NIFTY25APR24500CE",
        exchange: "NFO",
        product: "MIS",
        direction: "buy",
        opened_quantity: 50,
        closed_quantity: 50,
        net_quantity: 0,
      },
    ],
    fills: [
      {
        fact_id: "fill-1",
        source_fact_key: "fill-src-1",
        fill_timestamp: "2026-04-10T09:15:30Z",
        side: "buy",
        quantity: 50,
        price: "120.00",
        gross_cash_flow: "-6000.00",
        fees_amount: "74.75",
        taxes_amount: "54.75",
        stt: "25.00",
        brokerage: "20.00",
        charges_status: "settled",
      },
    ],
    timeline: [
      {
        event_id: "evt-1",
        event_type: "episode_opened",
        occurred_at: "2026-04-10T09:15:00Z",
        channel: "zerodha",
        actor_type: "system",
      },
      {
        event_id: "evt-2",
        event_type: "episode_closed",
        occurred_at: "2026-04-10T15:30:00Z",
        channel: "zerodha",
        actor_type: "system",
      },
    ],
    notes: "Initial trade note.",
  };

  it("episode detail: shows placeholder when no environment_id in URL", () => {
    window.history.pushState({}, "", "/journal/episodes/ep-uuid-001");

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalEpisodeDetailPage params={{ episodeId: "ep-uuid-001" }} />
      </JournalWorkspaceProvider>,
    );

    expect(screen.getByText(/environment_id/i)).toBeInTheDocument();
    expect(fetchEpisodeDetailMock).not.toHaveBeenCalled();
  });

  it("episode detail: fetches and renders episode info", async () => {
    window.history.pushState(
      {},
      "",
      "/journal/episodes/ep-uuid-001?environment_id=env-1",
    );
    fetchEpisodeDetailMock.mockResolvedValue(EPISODE_DETAIL_FIXTURE);

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalEpisodeDetailPage params={{ episodeId: "ep-uuid-001" }} />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() =>
      expect(fetchEpisodeDetailMock).toHaveBeenCalledWith({
        environment_id: "env-1",
        episode_id: "ep-uuid-001",
      }),
    );

    // Strategy display name appears in eyebrow
    expect(await screen.findByText(/Momentum V1/i)).toBeInTheDocument();
    // Status badge — scoped to role="status" wrapper or use getAllByText
    expect(screen.getAllByText(/closed/i).length).toBeGreaterThan(0);
    // Direction badge
    expect(screen.getAllByText(/long/i).length).toBeGreaterThan(0);
    // Net P&L KPI
    expect(screen.getByText(/Net P&L/i)).toBeInTheDocument();
    expect(screen.getAllByText(/1,250\.50/).length).toBeGreaterThan(0);
    // Gross P&L
    expect(screen.getByText(/Gross P&L/i)).toBeInTheDocument();
    // Legs table symbol
    expect(screen.getByText(/NIFTY25APR24500CE/i)).toBeInTheDocument();
    // Fills table side badge
    expect(screen.getAllByText(/buy/i).length).toBeGreaterThan(0);
    // Timeline events
    expect(screen.getByText(/episode_opened/i)).toBeInTheDocument();
    expect(screen.getByText(/episode_closed/i)).toBeInTheDocument();
    // Notes pre-populated
    expect(screen.getByDisplayValue("Initial trade note.")).toBeInTheDocument();
    // Back link — check href contains the right path segment
    const backLink = screen.getByRole("link", { name: /← Episodes/i });
    expect(backLink.getAttribute("href")).toContain("/journal/episodes");
  });

  it("episode detail: shows error alert when fetch fails", async () => {
    window.history.pushState(
      {},
      "",
      "/journal/episodes/ep-uuid-001?environment_id=env-1",
    );
    fetchEpisodeDetailMock.mockRejectedValue(new Error("Network timeout"));

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalEpisodeDetailPage params={{ episodeId: "ep-uuid-001" }} />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() =>
      expect(screen.getByText(/Failed to load episode/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/Network timeout/i)).toBeInTheDocument();
  });

  it("episode detail: save button is disabled until notes are edited", async () => {
    window.history.pushState(
      {},
      "",
      "/journal/episodes/ep-uuid-001?environment_id=env-1",
    );
    fetchEpisodeDetailMock.mockResolvedValue(EPISODE_DETAIL_FIXTURE);

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalEpisodeDetailPage params={{ episodeId: "ep-uuid-001" }} />
      </JournalWorkspaceProvider>,
    );

    // Wait for notes textarea to appear
    const textarea = await screen.findByRole("textbox", { name: /episode notes/i });
    expect(textarea).toHaveValue("Initial trade note.");

    const saveBtn = screen.getByRole("button", { name: /save notes/i });
    expect(saveBtn).toBeDisabled();
  });

  it("episode detail: editing notes enables save button and calls patchEpisodeNotes", async () => {
    window.history.pushState(
      {},
      "",
      "/journal/episodes/ep-uuid-001?environment_id=env-1",
    );
    fetchEpisodeDetailMock.mockResolvedValue(EPISODE_DETAIL_FIXTURE);
    patchEpisodeNotesMock.mockResolvedValue({ notes: "Updated note." });

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalEpisodeDetailPage params={{ episodeId: "ep-uuid-001" }} />
      </JournalWorkspaceProvider>,
    );

    const textarea = await screen.findByRole("textbox", { name: /episode notes/i });
    fireEvent.change(textarea, { target: { value: "Updated note." } });

    const saveBtn = screen.getByRole("button", { name: /save notes/i });
    expect(saveBtn).not.toBeDisabled();

    fireEvent.click(saveBtn);

    await waitFor(() =>
      expect(patchEpisodeNotesMock).toHaveBeenCalledWith({
        environment_id: "env-1",
        episode_id: "ep-uuid-001",
        notes: "Updated note.",
      }),
    );

    // Save button returns to disabled (no dirty state)
    await waitFor(() => expect(saveBtn).toBeDisabled());
  });

  it("episode detail: shows error message when patchEpisodeNotes fails", async () => {
    window.history.pushState(
      {},
      "",
      "/journal/episodes/ep-uuid-001?environment_id=env-1",
    );
    fetchEpisodeDetailMock.mockResolvedValue(EPISODE_DETAIL_FIXTURE);
    patchEpisodeNotesMock.mockRejectedValue(new Error("Save failed"));

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalEpisodeDetailPage params={{ episodeId: "ep-uuid-001" }} />
      </JournalWorkspaceProvider>,
    );

    const textarea = await screen.findByRole("textbox", { name: /episode notes/i });
    fireEvent.change(textarea, { target: { value: "Some new content." } });
    fireEvent.click(screen.getByRole("button", { name: /save notes/i }));

    await waitFor(() =>
      expect(screen.getByText(/Save failed/i)).toBeInTheDocument(),
    );
  });
});
