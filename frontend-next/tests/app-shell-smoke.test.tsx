import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/app-shell";
import { navigation } from "@/lib/navigation";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

vi.mock("@/features/trading/hooks/use-trading-console-data", () => ({
  useTradingConsoleData: () => ({
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
    quotes: [],
    paper: {
      accountScope: "default",
      account: {
        accountScope: "default",
        currency: "INR",
        startingBalance: 100000,
        availableFunds: 90000,
        blockedFunds: 10000,
        realizedPnl: 0,
        unrealizedPnl: 0,
        openPositionCount: 0,
      },
      activeStrategyCount: 0,
      strategies: [],
    },
    broker: { positions: [], activeCount: 0 },
  }),
}));

describe("AppShell", () => {
  it("renders the terminal shell chrome", () => {
    renderWithQueryClient(
      <AppShell navigation={navigation} activeHref="/custom-display">
        <section aria-label="smoke content">
          <h1>Custom Display</h1>
        </section>
      </AppShell>,
    );

    expect(screen.getByText("K")).toBeInTheDocument();
    expect(screen.getByTitle("Custom Display")).toHaveAttribute("href", "/custom-display");
    expect(screen.getByText("CUSTOM DISPLAY")).toBeInTheDocument();
    expect(screen.getByText(/no active strategies/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Custom Display" })).toBeInTheDocument();
  });

  it("includes Journal in the navigation rail", () => {
    renderWithQueryClient(
      <AppShell navigation={navigation} activeHref="/dashboard">
        <div>content</div>
      </AppShell>,
    );

    const journalLink = screen.getByTitle("Journal");
    expect(journalLink).toBeInTheDocument();
    expect(journalLink).toHaveAttribute("href", "/journal");
  });
});
