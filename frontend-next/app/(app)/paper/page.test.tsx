import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import PaperPage from "@/app/(app)/paper/page";

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
          netQuantity: 0,
          realizedPnl: 700,
          unrealizedPnl: 300,
          marginInUse: 19000,
          lastUpdatedAt: "2026-04-16T09:00:00Z",
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
          orders: [
            {
              order_id: "order-1",
              tradingsymbol: "NIFTY24APR24300CE",
              transaction_type: "SELL",
              quantity: 50,
              status: "COMPLETE",
              placed_at: "2026-04-16T09:00:00Z",
            },
          ],
          trades: [
            {
              trade_id: "trade-1",
              order_id: "order-1",
              transaction_type: "SELL",
              quantity: 50,
              price: 101.5,
              trade_timestamp: "2026-04-16T09:00:05Z",
            },
          ],
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

function renderWithQueryClient(ui: ReactElement) {
  const client = new QueryClient();
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("PaperPage", () => {
  it("renders canonical paper strategy groups and activity", () => {
    renderWithQueryClient(<PaperPage />);

    expect(screen.getByText(/strategy-centric paper book/i)).toBeInTheDocument();
    expect(screen.getAllByText(/paper iron condor/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/orders and fills/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /exit/i })).toBeInTheDocument();
  });
});
