import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DashboardPage from "@/app/(app)/dashboard/page";
import type { TradingConsoleSnapshot } from "@/features/trading/types";

function createSnapshot(): TradingConsoleSnapshot {
  return {
  runtime: {
    brokerConnected: true,
    brokerStatus: "connected",
    brokerMode: "system",
    brokerLastSuccessAt: null,
    brokerLastFailureAt: null,
    brokerLastError: null,
    brokerNextRefreshAt: null,
    websocketStatus: "connected",
    paperAvailable: true,
    appAuthenticated: true,
  },
  quotes: [
    { symbol: "NIFTY", token: 256265, lastPrice: 24300, changePercent: 0.4, connected: true },
    { symbol: "BANKNIFTY", token: 260105, lastPrice: 52000, changePercent: -0.2, connected: true },
  ],
  paper: {
    accountScope: "default",
    account: {
      accountScope: "default",
      currency: "INR",
      startingBalance: 100000,
      availableFunds: 85000,
      blockedFunds: 5000,
      realizedPnl: 200,
      unrealizedPnl: 1200,
      openPositionCount: 2,
    },
    activeStrategyCount: 1,
    strategies: [
      {
        strategyRunId: "run-1",
        strategyId: "run-1",
        displayName: "Short Straddle",
        mode: "paper",
        status: "open",
        isOpen: true,
        openLegCount: 2,
        realizedPnl: 0,
        unrealizedPnl: 1200,
        summaryFields: [],
        capabilities: { canEditRisk: true, editRiskReason: null, canExitStrategy: true, exitReason: null, allowedActions: ["edit_risk", "exit_strategy"], riskSchema: [] },
        positions: [],
        orders: [],
        trades: [],
        timeline: [],
      },
    ],
  },
  broker: { positions: [], activeCount: 0 },
  control: {
    generatedAt: null,
    totals: {
      strategyCount: 1,
      openStrategyCount: 1,
      positionCount: 0,
      staleWorkerCount: 0,
      realizedPnl: 0,
      unrealizedPnl: 0,
      netPnl: 1250,
    },
    strategies: [
      {
        strategyRunId: "live-1",
        displayName: "Banknifty Iron Fly",
        source: "algo_worker",
        mode: "live",
        status: "open",
        healthStatus: "healthy",
        heartbeatAgeSec: 4,
        workerId: "worker-1",
        workerName: "worker-alpha",
        workerMetrics: {},
        isOpen: true,
        realizedPnl: 150,
        unrealizedPnl: 1100,
        netPnl: 1250,
        positionCount: 2,
        openOrderCount: 1,
        tradeCount: 4,
        positions: [],
        orders: [],
        trades: [],
        allowedActions: [],
        actionReasons: {},
        protection: {
          source: "backend_worker_protection",
          status: "active",
          summary: "Protection healthy",
          lastCheckedAt: null,
          details: {},
        },
        lastUpdatedAt: null,
      },
    ],
    unattributed: {
      displayName: "Manual / unattributed broker exposure",
      positions: [],
      orders: [],
      realizedPnl: 0,
      unrealizedPnl: 0,
      netPnl: 0,
    },
  },
};
}

let mockSnapshot: TradingConsoleSnapshot = createSnapshot();

vi.mock("@/features/trading/hooks/use-trading-console-data", () => ({
  useTradingConsoleData: () => mockSnapshot,
}));

describe("DashboardPage", () => {
  beforeEach(() => {
    mockSnapshot = createSnapshot();
  });

  it("surfaces both live and paper strategy watch items when both exist", () => {
    render(<DashboardPage />);
    const watchlist = screen.getByText(/watch and handoff/i).closest("section");
    expect(watchlist).not.toBeNull();
    expect(within(watchlist as HTMLElement).getByText(/banknifty iron fly/i)).toBeInTheDocument();
    expect(within(watchlist as HTMLElement).getByText(/short straddle/i)).toBeInTheDocument();
  });

  it("does not render mock market snapshot text and surfaces live panels", () => {
    render(<DashboardPage />);
    expect(screen.queryByText(/mock market snapshot/i)).not.toBeInTheDocument();
    expect(screen.getByText(/operator overview/i)).toBeInTheDocument();
    expect(screen.getByText(/live posture/i)).toBeInTheDocument();
    expect(screen.getByText(/system health/i)).toBeInTheDocument();
  });

  it("shows a loading-safe live control metric when control data is unavailable", () => {
    mockSnapshot = { ...mockSnapshot, control: null };
    render(<DashboardPage />);
    expect(screen.getByText(/control plane loading/i)).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
