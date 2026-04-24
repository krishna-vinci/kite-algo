import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompactTradingDock } from "@/features/trading/components/compact-trading-dock";

describe("CompactTradingDock", () => {
  it("renders only live-derived counts and strategy names", () => {
    render(
      <CompactTradingDock
        workspace="/dashboard"
        paper={{
          accountScope: "default",
          account: {
            accountScope: "default",
            currency: "INR",
            startingBalance: 100000,
            availableFunds: 82000,
            blockedFunds: 18000,
            realizedPnl: 0,
            unrealizedPnl: 1500,
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
              unrealizedPnl: 1500,
              summaryFields: [],
              capabilities: { canEditRisk: true, editRiskReason: null, canExitStrategy: true, exitReason: null, allowedActions: ["edit_risk", "exit_strategy"], riskSchema: [] },
              positions: [],
              orders: [],
              trades: [],
              timeline: [],
            },
          ],
        }}
        broker={{ positions: [], activeCount: 0 }}
      />,
    );

    expect(screen.getByText(/short straddle/i)).toBeInTheDocument();
    expect(screen.getByText(/active strategies/i)).toBeInTheDocument();
  });
});
