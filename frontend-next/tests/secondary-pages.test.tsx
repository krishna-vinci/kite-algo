import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PaperPage from "@/app/(app)/paper/page";
import SettingsPage from "@/app/(app)/settings/page";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

vi.mock("@/features/trading/hooks/use-paper-strategy-summary", () => ({
  usePaperStrategySummary: () => ({
    data: {
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
          riskControls: {
            indexLowerBoundary: null,
            indexUpperBoundary: null,
            combinedPremiumTarget: 18,
            combinedPremiumStoploss: 32,
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
    isLoading: false,
    isFetching: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

describe("secondary reference pages", () => {
  it("renders the shared paper workspace", () => {
    renderWithQueryClient(<PaperPage />);

    expect(screen.getByRole("heading", { name: /paper account/i })).toBeInTheDocument();
    expect(screen.getByText(/strategy groups/i)).toBeInTheDocument();
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
