import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import AlgosPage from "@/app/(app)/algos/page";

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
          strategyId: "run-1",
          displayName: "Runtime Short Straddle",
          strategyTag: "options_runtime",
          algoInstanceId: "algo-42",
          mode: "paper",
          status: "open",
          isOpen: true,
          openLegCount: 2,
          netQuantity: 0,
          realizedPnl: 0,
          unrealizedPnl: 1200,
          marginInUse: 14000,
          lastUpdatedAt: "2026-04-16T09:00:00Z",
          riskControls: {
            indexLowerBoundary: null,
            indexUpperBoundary: null,
            combinedPremiumTarget: null,
            combinedPremiumStoploss: null,
            basketMtmTarget: null,
            basketMtmStoploss: null,
          },
          capabilities: { canEditRisk: true, editRiskReason: null },
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

describe("AlgosPage", () => {
  it("renders shared runtime and strategy monitoring panels", () => {
    render(<AlgosPage />);

    expect(screen.getByText(/algo runtime overview/i)).toBeInTheDocument();
    expect(screen.getByText(/runtime short straddle/i)).toBeInTheDocument();
    expect(screen.getByText(/open trading console/i)).toBeInTheDocument();
  });
});
