import { describe, expect, it } from "vitest";

import { normalizePaperStrategySummary, normalizeRuntimeStatus } from "@/features/trading/api";

describe("normalizePaperStrategySummary", () => {
  it("keeps closed strategies out of active counts and preserves mode/capabilities", () => {
    const summary = normalizePaperStrategySummary({
      account: {
        account_scope: "default",
        currency: "INR",
        starting_balance: 100000,
        available_funds: 82000,
        blocked_funds: 18000,
        realized_pnl: 1200,
        unrealized_pnl: 350,
        open_position_count: 1,
      },
      strategies: [
        {
          strategy_run_id: "open-1",
          strategy_id: "open-1",
          display_name: "Short Straddle",
          mode: "paper",
          status: "open",
          is_open: true,
          open_leg_count: 2,
          summary_fields: [{ key: "combined_premium_target", label: "Premium target", value: 18, unit: "pts" }],
          capabilities: {
            can_edit_risk: true,
            can_exit_strategy: true,
            allowed_actions: ["edit_risk", "exit_strategy"],
            risk_schema: [{ key: "combined_premium_target", type: "number" }],
          },
          positions: [],
          orders: [],
          trades: [],
        },
        {
          strategy_run_id: "closed-1",
          strategy_id: "closed-1",
          display_name: "Closed Strangle",
          mode: "paper",
          status: "closed",
          is_open: false,
          open_leg_count: 0,
          summary_fields: [],
          capabilities: {
            can_edit_risk: false,
            can_exit_strategy: false,
            exit_reason: "Only open monitored paper strategies support strategy-level exit",
            allowed_actions: [],
            risk_schema: [],
          },
          positions: [],
          orders: [],
          trades: [],
        },
      ],
    });

    expect(summary.activeStrategyCount).toBe(1);
    expect(summary.account.availableFunds).toBe(82000);
    expect(summary.strategies[0].strategyRunId).toBe("open-1");
    expect(summary.strategies[0].summaryFields).toEqual([{ key: "combined_premium_target", label: "Premium target", value: 18, unit: "pts", group: null }]);
    expect(summary.strategies[0].capabilities.canEditRisk).toBe(true);
    expect(summary.strategies[0].capabilities.canExitStrategy).toBe(true);
    expect(summary.strategies[0].capabilities.allowedActions).toEqual(["edit_risk", "exit_strategy"]);
    expect(summary.strategies[0].capabilities.riskSchema).toEqual([
      {
        key: "combined_premium_target",
        label: "combined_premium_target",
        type: "number",
        unit: null,
        group: null,
        required: false,
        recommended: false,
        value: undefined,
      },
    ]);
    expect(summary.strategies[1].isOpen).toBe(false);
    expect(summary.strategies[1].capabilities.canExitStrategy).toBe(false);
  });
});

describe("normalizeRuntimeStatus", () => {
  it("normalizes auth, broker, and runtime health into the UI shape", () => {
    const status = normalizeRuntimeStatus({
      app: { authenticated: true },
      broker: { connected: true, status: "connected", mode: "system" },
      runtime: { websocket: { status: "connected" }, paper_runtime: { available: true } },
    });

    expect(status.appAuthenticated).toBe(true);
    expect(status.brokerStatus).toBe("connected");
    expect(status.paperAvailable).toBe(true);
  });
});
