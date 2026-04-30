import { screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import DashboardPage from "@/app/(app)/dashboard/page";
import OptionsPage from "@/app/(app)/options/page";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

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

vi.mock("@/lib/options/api", () => ({
  ensureOptionsSessions: vi.fn().mockResolvedValue(undefined),
  fetchOptionSession: vi.fn().mockResolvedValue({
    underlying: "NIFTY",
    spotLtp: 24300,
    atmStrike: 24300,
    expiries: ["2026-04-30"],
    perExpiry: {
      "2026-04-30": { forward: 24320, sigmaExpiry: null, atmStrike: 24300, strikes: [], rows: [] },
    },
    rows: [],
    updatedAt: null,
  }),
  fetchNifty50Impact: vi.fn().mockResolvedValue([]),
  loginToBroker: vi.fn().mockResolvedValue({ authenticated: true }),
  mergeOptionSessionSnapshot: vi.fn((_current, next) => next),
  normalizeOptionSessionSnapshot: vi.fn((payload) => payload),
  buildOptionsSessionSseUrl: vi.fn(() => "/api/sse/options/session/NIFTY"),
  previewOptionStrategy: vi.fn(),
  buildPositionDryRun: vi.fn(),
  executePaperOptionStrategy: vi.fn(),
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

  it("renders the productionized options workspace", () => {
    renderWithQueryClient(<OptionsPage />);

    expect(screen.getByRole("heading", { name: /options/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /strategy builder/i })).toBeInTheDocument();
    expect(screen.getByText(/canonical paper \+ broker state/i)).toBeInTheDocument();
  });

});
