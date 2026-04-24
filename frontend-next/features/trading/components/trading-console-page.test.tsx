import { render, screen } from "@testing-library/react";
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
};

describe("TradingConsolePage", () => {
  it("renders the heading", () => {
    render(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    expect(screen.getByText("Trading console")).toBeInTheDocument();
  });

  it("renders strategy name", () => {
    render(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    expect(screen.getByText("Iron Condor NIFTY")).toBeInTheDocument();
  });

  it("renders broker positions section with active positions only", () => {
    render(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    // The active position should appear
    expect(screen.getByText("NIFTY24APR25300CE")).toBeInTheDocument();
    // The closed position (qty=0) should NOT appear
    expect(screen.queryByText("NIFTY24APR25400CE")).not.toBeInTheDocument();
  });

  it("shows market quotes", () => {
    render(<TradingConsolePage snapshot={MOCK_SNAPSHOT} />);
    expect(screen.getByTestId("market-quote-strip")).toBeInTheDocument();
    expect(screen.getByText("NIFTY")).toBeInTheDocument();
  });
});
