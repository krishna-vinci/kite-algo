import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StrategyGroupsPanel } from "@/features/trading/components/strategy-groups-panel";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

vi.mock("@/features/trading/api", () => ({
  updatePaperStrategyRisk: vi.fn(),
}));

describe("StrategyGroupsPanel", () => {
  it("renders backend-driven risk schema fields in the editor", () => {
    renderWithQueryClient(
      <StrategyGroupsPanel
        strategies={[
          {
            strategyRunId: "run-1",
            strategyId: "run-1",
            displayName: "Short Straddle",
            strategyTag: "short_straddle",
            algoInstanceId: "algo-1",
            mode: "paper",
            status: "open",
            isOpen: true,
            openLegCount: 2,
            netQuantity: 0,
            realizedPnl: 0,
            unrealizedPnl: 500,
            marginInUse: 10000,
            lastUpdatedAt: null,
            summaryFields: [{ key: "combined_premium_target", label: "Premium target", value: 18, unit: "pts", group: "primary" }],
            capabilities: {
              canEditRisk: true,
              editRiskReason: null,
              canExitStrategy: true,
              exitReason: null,
              allowedActions: ["edit_risk", "exit_strategy"],
              riskSchema: [
                { key: "combined_premium_target", label: "Premium target", type: "number", unit: "pts", value: 18 },
                { key: "combined_premium_stoploss", label: "Premium stoploss", type: "number", unit: "pts", value: 32 },
              ],
            },
            positions: [],
            orders: [],
            trades: [],
            timeline: [],
          },
        ]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /edit risk/i }));

    const sheet = screen.getByTestId("risk-sheet");
    expect(sheet).toBeInTheDocument();
    expect(within(sheet).getByText(/premium target/i)).toBeInTheDocument();
    expect(within(sheet).getByText(/premium stoploss/i)).toBeInTheDocument();
    expect(within(sheet).getByDisplayValue("18")).toBeInTheDocument();
    expect(within(sheet).getByDisplayValue("32")).toBeInTheDocument();
    expect(screen.getByLabelText(/P&L \+500/i)).toBeInTheDocument();
  });

  it("hides the edit button when the run exposes no editable risk fields", () => {
    renderWithQueryClient(
      <StrategyGroupsPanel
        strategies={[
          {
            strategyRunId: "run-2",
            strategyId: "run-2",
            displayName: "Locked Run",
            strategyTag: "locked",
            algoInstanceId: null,
            mode: "paper",
            status: "open",
            isOpen: true,
            openLegCount: 1,
            netQuantity: 0,
            realizedPnl: 0,
            unrealizedPnl: 0,
            marginInUse: 0,
            lastUpdatedAt: null,
            summaryFields: [],
            capabilities: {
              canEditRisk: true,
              editRiskReason: null,
              canExitStrategy: true,
              exitReason: null,
              allowedActions: ["edit_risk", "exit_strategy"],
              riskSchema: [],
            },
            positions: [],
            orders: [],
            trades: [],
            timeline: [],
          },
        ]}
      />,
    );

    expect(screen.queryByRole("button", { name: /edit risk/i })).not.toBeInTheDocument();
  });
});
