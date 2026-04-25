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
          strategyRunId: "paper-1",
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
          summaryFields: [{ key: "combined_premium_target", label: "Premium target", value: 18, unit: "pts", group: "primary" }],
          capabilities: { canEditRisk: true, editRiskReason: null, canExitStrategy: true, exitReason: null, allowedActions: ["edit_risk", "exit_strategy"], riskSchema: [] },
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
        {
          strategyRunId: "manual:256265:MIS",
          strategyId: "manual:256265:MIS",
          displayName: "Manual basket · INFY",
          strategyTag: null,
          algoInstanceId: null,
          mode: "paper",
          status: "open",
          isOpen: true,
          openLegCount: 1,
          netQuantity: 1,
          realizedPnl: 0,
          unrealizedPnl: 125,
          marginInUse: 5000,
          lastUpdatedAt: "2026-04-16T09:05:00Z",
          summaryFields: [],
          capabilities: {
            canEditRisk: false,
            editRiskReason: "Manual paper activity does not support runtime risk edits",
            canExitStrategy: false,
            exitReason: "Strategy-level exit is unavailable for manual paper activity",
            allowedActions: [],
            riskSchema: [],
          },
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
    expect(screen.getAllByRole("button", { name: /exit/i }).length).toBeGreaterThan(0);
    const buttons = screen.getAllByRole("button", { name: /exit/i });
    expect(buttons.some((button) => !button.hasAttribute("disabled"))).toBe(true);
    expect(buttons.some((button) => button.hasAttribute("disabled"))).toBe(true);
  });
});
