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
          strategy_id: "open-1",
          display_name: "Short Straddle",
          mode: "paper",
          status: "open",
          is_open: true,
          open_leg_count: 2,
          risk_controls: { combined_premium_target: 18, combined_premium_stoploss: 32 },
          capabilities: { can_edit_risk: true },
          positions: [],
          orders: [],
          trades: [],
        },
        {
          strategy_id: "closed-1",
          display_name: "Closed Strangle",
          mode: "paper",
          status: "closed",
          is_open: false,
          open_leg_count: 0,
          risk_controls: {},
          capabilities: { can_edit_risk: false },
          positions: [],
          orders: [],
          trades: [],
        },
      ],
    });

    expect(summary.activeStrategyCount).toBe(1);
    expect(summary.strategies[0].capabilities.canEditRisk).toBe(true);
    expect(summary.strategies[1].isOpen).toBe(false);
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
