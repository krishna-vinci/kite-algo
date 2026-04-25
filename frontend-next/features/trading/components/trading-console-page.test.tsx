import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, it, expect } from "vitest";
import { TradingConsolePage } from "./trading-console-page";
import type { TradingConsoleSnapshot } from "@/features/trading/types";

const MOCK_SNAPSHOT: TradingConsoleSnapshot = {
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
  quotes: [
    { symbol: "NIFTY", token: 256265, lastPrice: 25300, changePercent: 0.42, connected: true },
    { symbol: "BANKNIFTY", token: 260105, lastPrice: 51600, changePercent: -0.18, connected: true },
  ],
  paper: {
    accountScope: "default",
    account: {
      accountScope: "default",
      currency: "INR",
      startingBalance: 100000,
      availableFunds: 82000,
      blockedFunds: 18000,
      realizedPnl: 1200,
      unrealizedPnl: -350,
      openPositionCount: 1,
    },
    activeStrategyCount: 1,
    strategies: [
      {
        strategyRunId: "strat-1",
        strategyId: "strat-1",
        displayName: "Iron Condor NIFTY",
        mode: "paper",
        status: "active",
        isOpen: true,
        openLegCount: 4,
        realizedPnl: 1200,
        unrealizedPnl: -350,
        summaryFields: [
          { key: "index_lower_boundary", label: "Index lower boundary", value: 24800, unit: null, group: "emergency" },
          { key: "index_upper_boundary", label: "Index upper boundary", value: 25800, unit: null, group: "emergency" },
          { key: "combined_premium_target", label: "Premium target", value: 80, unit: "pts", group: "primary" },
          { key: "combined_premium_stoploss", label: "Premium stoploss", value: 160, unit: "pts", group: "primary" },
        ],
        capabilities: { canEditRisk: true, editRiskReason: null, canExitStrategy: true, exitReason: null, allowedActions: ["edit_risk", "exit_strategy"], riskSchema: [] },
        positions: [],
        orders: [],
        trades: [],
        timeline: [],
      },
    ],
  },
  broker: {
    positions: [
      {
        positionKey: "pos-1",
        tradingSymbol: "NIFTY24APR25300CE",
        exchange: "NFO",
        product: "MIS",
        quantity: -50,
        averagePrice: 120.5,
        lastPrice: 115.3,
        pnl: 260,
        realizedPnl: 0,
        unrealizedPnl: 260,
      },
      {
        positionKey: "pos-closed",
        tradingSymbol: "NIFTY24APR25400CE",
        exchange: "NFO",
        product: "MIS",
        quantity: 0,
        averagePrice: 90,
        lastPrice: 85,
        pnl: 250,
        realizedPnl: 250,
        unrealizedPnl: 0,
      },
    ],
    activeCount: 1,
  },
  control: {
    generatedAt: "2026-04-25T12:00:00+00:00",
    totals: {
      strategyCount: 1,
      openStrategyCount: 1,
      positionCount: 1,
      staleWorkerCount: 0,
      realizedPnl: 150,
      unrealizedPnl: -25,
      netPnl: 125,
    },
    strategies: [
      {
        strategyRunId: "control-1",
        displayName: "Mean Reversion",
        source: "algo_worker",
        mode: "live",
        status: "open",
        healthStatus: "healthy",
        heartbeatAgeSec: 10,
        workerId: "worker-1",
        workerName: "box-1",
        workerMetrics: {},
        isOpen: true,
        realizedPnl: 150,
        unrealizedPnl: -25,
        netPnl: 125,
        positionCount: 1,
        openOrderCount: 0,
        tradeCount: 1,
        positions: [{ tradingsymbol: "INFY", quantity: 1 }],
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
  },
};

function renderWithQueryClient(ui: ReactElement) {
  const client = new QueryClient();
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("TradingConsolePage", () => {
  it("renders the heading", () => {
    renderWithQueryClient(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    expect(screen.getByText("Trading console")).toBeInTheDocument();
  });

  it("renders strategy name", () => {
    renderWithQueryClient(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    expect(screen.getByText("Iron Condor NIFTY")).toBeInTheDocument();
  });

  it("renders broker positions section with active positions only", () => {
    renderWithQueryClient(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    // The active position should appear
    expect(screen.getByText("NIFTY24APR25300CE")).toBeInTheDocument();
    // The closed position (qty=0) should NOT appear
    expect(screen.queryByText("NIFTY24APR25400CE")).not.toBeInTheDocument();
  });

  it("shows market quotes", () => {
    renderWithQueryClient(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    expect(screen.getByTestId("market-quote-strip")).toBeInTheDocument();
    expect(screen.getByText("NIFTY")).toBeInTheDocument();
  });

  it("renders control plane without losing existing panels", () => {
    renderWithQueryClient(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);

    expect(screen.getByTestId("control-plane-panel")).toBeInTheDocument();
    expect(screen.getByText("Mean Reversion")).toBeInTheDocument();
    expect(screen.getByTestId("strategy-groups-panel")).toBeInTheDocument();
    expect(screen.getByTestId("broker-positions-panel")).toBeInTheDocument();
  });
});
