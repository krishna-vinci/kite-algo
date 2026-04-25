import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReactElement } from "react";
import type { ControlPlaneSnapshot } from "@/features/trading/types";
import { ControlPlanePanel } from "./control-plane-panel";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const snapshot: ControlPlaneSnapshot = {
  generatedAt: "2026-04-25T12:00:00+00:00",
  totals: {
    strategyCount: 1,
    openStrategyCount: 1,
    positionCount: 2,
    staleWorkerCount: 1,
    realizedPnl: 100,
    unrealizedPnl: -20,
    netPnl: 80,
  },
  strategies: [
    {
      strategyRunId: "run-live-1",
      displayName: "Mean Reversion",
      source: "algo_worker",
      mode: "live",
      status: "open",
      healthStatus: "stale",
      heartbeatAgeSec: 95,
      workerId: "w-1",
      workerName: "ml-box-worker",
      workerMetrics: { machine_id: "ml-box-01" },
      isOpen: true,
      realizedPnl: 100,
      unrealizedPnl: -20,
      netPnl: 80,
      positionCount: 1,
      openOrderCount: 0,
      tradeCount: 2,
      positions: [{ tradingsymbol: "INFY", net_quantity: 1 }],
      orders: [],
      trades: [],
      allowedActions: ["exit_strategy"],
      actionReasons: { cancel_orders: "Strategy-scoped cancel is disabled" },
      protection: {
        source: "option_runtime",
        status: "active",
        summary: "Option protection active; 2 rule(s) configured",
        lastCheckedAt: "2026-04-25T12:00:00+00:00",
        details: { rule_count: 2, lifecycle_state: "running" },
      },
      lastUpdatedAt: null,
    },
  ],
  unattributed: {
    displayName: "Manual / unattributed broker exposure",
    positions: [{ tradingsymbol: "MANUAL", quantity: 25 }],
    orders: [],
    realizedPnl: 0,
    unrealizedPnl: 250,
    netPnl: 250,
  },
};

function renderWithQueryClient(ui: ReactElement) {
  const client = new QueryClient();
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("ControlPlanePanel", () => {
  it("renders grouped exposure, worker health, actions, protection, and unattributed positions", () => {
    renderWithQueryClient(<ControlPlanePanel snapshot={snapshot} onRefresh={vi.fn()} />);

    expect(screen.getByText("Control plane")).toBeInTheDocument();
    expect(screen.getByText("Mean Reversion")).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByText("Exit strategy")).toBeInTheDocument();
    expect(screen.getByText("option_runtime · active")).toBeInTheDocument();
    expect(screen.getByText(/2 rule/)).toBeInTheDocument();
    expect(screen.getByText("Manual / unattributed broker exposure")).toBeInTheDocument();
    expect(screen.getByText(/MANUAL/)).toBeInTheDocument();
  });
});
