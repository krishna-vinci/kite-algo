import { describe, expect, it } from "vitest";

import { normalizeControlPlaneSnapshot, normalizePaperStrategySummary, normalizeRuntimeStatus } from "@/features/trading/api";

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

describe("normalizeControlPlaneSnapshot", () => {
  it("normalizes grouped strategies, health, protection, actions, and unattributed exposure", () => {
    const snapshot = normalizeControlPlaneSnapshot({
      generated_at: "2026-04-25T12:00:00+00:00",
      totals: {
        strategy_count: 1,
        open_strategy_count: 1,
        position_count: 2,
        stale_worker_count: 1,
        realized_pnl: 100,
        unrealized_pnl: -20,
        net_pnl: 80,
      },
      strategies: [
        {
          strategy_run_id: "run-live-1",
          display_name: "Mean Reversion",
          source: "algo_worker",
          mode: "live",
          status: "open",
          health_status: "stale",
          heartbeat_age_sec: 95,
          is_open: true,
          realized_pnl: 100,
          unrealized_pnl: -20,
          net_pnl: 80,
          position_count: 1,
          open_order_count: 0,
          trade_count: 2,
          positions: [{ tradingsymbol: "INFY", net_quantity: 1 }],
          orders: [],
          trades: [],
          allowed_actions: ["exit_strategy"],
          action_reasons: { cancel_orders: "disabled" },
          protection: {
            source: "option_runtime",
            status: "active",
            summary: "Option protection active; 2 rule(s) configured",
            last_checked_at: "2026-04-25T12:00:00+00:00",
            details: { rule_count: 2, lifecycle_state: "running" },
          },
        },
      ],
      unattributed: {
        display_name: "Manual / unattributed broker exposure",
        positions: [{ tradingsymbol: "MANUAL", quantity: 25 }],
        orders: [],
        realized_pnl: 0,
        unrealized_pnl: 250,
        net_pnl: 250,
      },
    });

    expect(snapshot.totals.strategyCount).toBe(1);
    expect(snapshot.strategies[0].strategyRunId).toBe("run-live-1");
    expect(snapshot.strategies[0].healthStatus).toBe("stale");
    expect(snapshot.strategies[0].allowedActions).toEqual(["exit_strategy"]);
    expect(snapshot.strategies[0].protection.source).toBe("option_runtime");
    expect(snapshot.strategies[0].protection.details).toEqual({ rule_count: 2, lifecycle_state: "running" });
    expect(snapshot.unattributed.positions[0]).toEqual({ tradingsymbol: "MANUAL", quantity: 25 });
  });
});
