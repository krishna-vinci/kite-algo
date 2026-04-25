import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DashboardPage from "@/app/(app)/dashboard/page";

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
  }),
}));

describe("DashboardPage", () => {
  it("does not render mock market snapshot text and surfaces live panels", () => {
    render(<DashboardPage />);
    expect(screen.queryByText(/mock market snapshot/i)).not.toBeInTheDocument();
    expect(screen.getByText(/operator overview/i)).toBeInTheDocument();
    expect(screen.getByText(/system health/i)).toBeInTheDocument();
  });
});
