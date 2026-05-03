import { screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import DashboardPage from "@/app/(app)/dashboard/page";
import StrategiesPage from "@/app/(app)/strategies/page";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

const replaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

class MockEventSource {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  constructor() {}
  close() {}
}

vi.mock("@/features/trading/hooks/use-trading-console-data", () => ({
  useTradingConsoleData: () => ({
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
        availableFunds: 82000,
        blockedFunds: 18000,
        realizedPnl: 0,
        unrealizedPnl: 1200,
        openPositionCount: 2,
      },
      activeStrategyCount: 1,
      strategies: [
        {
          strategyRunId: "run-1",
          strategyId: "run-1",
          displayName: "Short Straddle",
          strategyTag: "options_runtime",
          algoInstanceId: "algo-42",
          mode: "paper",
          status: "open",
          isOpen: true,
          openLegCount: 2,
          realizedPnl: 0,
          unrealizedPnl: 1200,
          marginInUse: 15000,
          lastUpdatedAt: "2026-04-16T09:00:00Z",
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
    control: { generatedAt: null, totals: { strategyCount: 0, openStrategyCount: 0, positionCount: 0, staleWorkerCount: 0, realizedPnl: 0, unrealizedPnl: 0, netPnl: 0 }, strategies: [], unattributed: { displayName: "Manual / unattributed broker exposure", positions: [], orders: [], realizedPnl: 0, unrealizedPnl: 0, netPnl: 0 } },
  }),
}));

beforeAll(() => {
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});

describe("reference pages", () => {
  it("renders the dashboard live operator content", () => {
    renderWithQueryClient(<DashboardPage />);

    expect(screen.getByRole("heading", { name: /operator overview/i })).toBeInTheDocument();
    expect(screen.getByText(/system health/i)).toBeInTheDocument();
    expect(screen.getAllByText(/active strategies/i).length).toBeGreaterThan(0);
  });

  it("renders the primary strategies operator workspace", () => {
    renderWithQueryClient(<StrategiesPage />);

    expect(screen.getByRole("heading", { name: /strategies/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /live/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /paper/i })).toBeInTheDocument();
  });

});
