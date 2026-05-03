import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import SettingsPage from "@/app/(app)/settings/page";
import StrategiesPage from "@/app/(app)/strategies/page";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

const replaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

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
    quotes: [],
    paper: {
      accountScope: "default",
      account: {
        accountScope: "default",
        currency: "INR",
        startingBalance: 100000,
        availableFunds: 81000,
        blockedFunds: 19000,
        realizedPnl: 700,
        unrealizedPnl: 300,
        openPositionCount: 2,
      },
      activeStrategyCount: 1,
      strategies: [
        {
          strategyRunId: "paper-1",
          strategyId: "paper-1",
          displayName: "Paper Iron Condor",
          strategyTag: "options_runtime",
          algoInstanceId: "algo-paper-1",
          mode: "paper",
          status: "open",
          isOpen: true,
          openLegCount: 4,
          realizedPnl: 700,
          unrealizedPnl: 300,
          marginInUse: 19000,
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
    control: { generatedAt: null, totals: { strategyCount: 1, openStrategyCount: 1, positionCount: 0, staleWorkerCount: 0, realizedPnl: 0, unrealizedPnl: 0, netPnl: 0 }, strategies: [], unattributed: { displayName: "Manual / unattributed broker exposure", positions: [], orders: [], realizedPnl: 0, unrealizedPnl: 0, netPnl: 0 } },
  }),
}));

describe("secondary reference pages", () => {
  it("renders the paper tab inside the shared strategies workspace", () => {
    window.history.replaceState({}, "", "/strategies?mode=paper");
    renderWithQueryClient(<StrategiesPage />);

    expect(screen.getByRole("heading", { name: /paper account/i })).toBeInTheDocument();
    expect(screen.getByText(/paper iron condor/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /exit/i })).toBeInTheDocument();
  });

  it("renders the settings workspace", () => {
    renderWithQueryClient(<SettingsPage />);

    expect(screen.getByRole("navigation", { name: /settings sections/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /index baselines/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /settings apis/i })).toBeInTheDocument();
    expect(screen.getByText(/static placeholder values have been removed/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /configuration apis/i })).toHaveAttribute("href", "#configuration-apis");
  });

});
