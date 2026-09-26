import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ReactElement } from "react";
import { LiveTradingPanel } from "./live-trading-panel";

const apiMocks = vi.hoisted(() => ({
  fetchPlatformLiveSettings: vi.fn(),
  updatePlatformLiveSettings: vi.fn(),
  fetchPlatformStatus: vi.fn(),
}));

vi.mock("@/lib/platform/api", () => apiMocks);

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function renderWithQueryClient(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("LiveTradingPanel", () => {
  it("asks for confirmation before turning a lane on, and never for turning one off", async () => {
    apiMocks.fetchPlatformLiveSettings.mockResolvedValue({
      live_enabled: true,
      lanes: { cnc: true, mis: false, futures: false, options: false },
      lanes_source: "db",
      account: { scope: "kite:XJJ***", allowed: true },
      updated_at: "2026-09-20T10:00:00Z",
      updated_by: "owner",
    });
    const user = userEvent.setup();

    renderWithQueryClient(<LiveTradingPanel />);

    expect(await screen.findByText("Set by the server")).toBeInTheDocument();

    // Turning MIS on must show the confirmation dialog with the exact copy.
    const misToggle = await screen.findByLabelText("Toggle MIS lane");
    await user.click(misToggle);
    expect(
      await screen.findByText("New live exposure will be allowed in MIS. Exits are never blocked."),
    ).toBeInTheDocument();

    // Cancelling leaves the lane untouched.
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/New live exposure will be allowed/)).not.toBeInTheDocument();

    // Turning CNC (already on) off must NOT open any confirmation dialog.
    const cncToggle = screen.getByLabelText("Toggle CNC lane");
    await user.click(cncToggle);
    expect(screen.queryByText(/New live exposure will be allowed/)).not.toBeInTheDocument();
  });
});
