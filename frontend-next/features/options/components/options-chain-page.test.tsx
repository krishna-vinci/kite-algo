import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OptionsChainPage } from "./options-chain-page";

vi.mock("@/lib/options/api", () => ({
  startOptionSessions: vi.fn().mockResolvedValue({ status: "ok", watchlist: ["NIFTY"] }),
  fetchOptionSession: vi.fn().mockResolvedValue({
    underlying: "NIFTY",
    expiries: ["2026-10-01"],
    spot_ltp: 25000,
    updated_at: "2026-09-26T09:15:00Z",
  }),
  fetchOptionExpiries: vi.fn().mockResolvedValue({
    underlying: "NIFTY",
    expiries: ["2026-10-01"],
    spot_ltp: 25000,
    updated_at: "2026-09-26T09:15:00Z",
  }),
  fetchOptionChain: vi.fn().mockResolvedValue({
    underlying: "NIFTY",
    expiry: "2026-10-01",
    spot_ltp: 25000,
    atm_strike: 25000,
    strikes: [24900, 25000, 25100],
    chain: [
      {
        strike: 24900,
        ce: { token: 1, tsym: "NIFTY24900CE", lot_size: 75, ltp: 150, iv: 12, oi: 1000, delta: 0.6, gamma: 0.001, theta: -2, vega: 5, rho: null, updated_at: null },
        pe: { token: 2, tsym: "NIFTY24900PE", lot_size: 75, ltp: 60, iv: 13, oi: 800, delta: -0.4, gamma: 0.001, theta: -1.5, vega: 4, rho: null, updated_at: null },
      },
      {
        strike: 25000,
        ce: { token: 3, tsym: "NIFTY25000CE", lot_size: 75, ltp: 100, iv: 12, oi: 1500, delta: 0.5, gamma: 0.001, theta: -2, vega: 5, rho: null, updated_at: null },
        pe: { token: 4, tsym: "NIFTY25000PE", lot_size: 75, ltp: 95, iv: 13, oi: 1400, delta: -0.5, gamma: 0.001, theta: -1.5, vega: 4, rho: null, updated_at: null },
      },
      {
        strike: 25100,
        ce: { token: 5, tsym: "NIFTY25100CE", lot_size: 75, ltp: 60, iv: 12, oi: 900, delta: 0.4, gamma: 0.001, theta: -2, vega: 5, rho: null, updated_at: null },
        pe: { token: 6, tsym: "NIFTY25100PE", lot_size: 75, ltp: 140, iv: 13, oi: 1100, delta: -0.6, gamma: 0.001, theta: -1.5, vega: 4, rho: null, updated_at: null },
      },
    ],
    updated_at: "2026-09-26T09:15:00Z",
  }),
  fetchOptionPcr: vi.fn().mockResolvedValue({ underlying: "NIFTY", expiry: "2026-10-01", value: 1.1, updated_at: null }),
  fetchOptionMaxPain: vi
    .fn()
    .mockResolvedValue({ underlying: "NIFTY", expiry: "2026-10-01", value: 25000, updated_at: null }),
}));

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OptionsChainPage />
    </QueryClientProvider>,
  );
}

describe("OptionsChainPage", () => {
  it("renders the chain with an ATM badge and lets you add a payoff leg", async () => {
    const user = (await import("@testing-library/user-event")).default.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("ATM")).toBeInTheDocument());
    expect(screen.getAllByText("25,000.00").length).toBeGreaterThan(0);

    const ceLtpButtons = screen.getAllByTitle("Add BUY leg");
    await user.click(ceLtpButtons[0]);

    await waitFor(() =>
      expect(screen.queryByText("No legs selected yet. Click an LTP in the chain above.")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Max profit")).toBeInTheDocument();
  });
});
