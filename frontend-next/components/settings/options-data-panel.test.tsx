import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ReactElement } from "react";
import { OptionsDataPanel } from "./options-data-panel";

const apiMocks = vi.hoisted(() => ({
  getOptionsSettings: vi.fn(),
  updateOptionsSettings: vi.fn(),
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

const baseSettings = {
  always_on: ["NIFTY"],
  available_underlyings: ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "BANKEX"],
  cadence_sec: 5,
  tick_driven: true,
  min_interval_sec: 1.0,
  idle_stop_minutes: 15,
  source: "db" as const,
  updated_at: "2026-09-20T10:00:00Z",
  updated_by: "owner",
  sessions: [
    {
      underlying: "NIFTY",
      running: true,
      always_on: true,
      last_used_age_s: 3.2,
      updated_age_s: 0.8,
      desired_tokens: 250,
      cadence_sec: 5,
    },
    {
      underlying: "BANKNIFTY",
      running: false,
      always_on: false,
      last_used_age_s: 120,
      updated_age_s: 90,
      desired_tokens: 0,
      cadence_sec: 5,
    },
  ],
};

describe("OptionsDataPanel", () => {
  it("renders defaults from GET and shows the sessions table", async () => {
    apiMocks.getOptionsSettings.mockResolvedValue(baseSettings);

    renderWithQueryClient(<OptionsDataPanel />);

    expect(await screen.findByLabelText("Always run NIFTY")).toBeChecked();
    expect(screen.getByLabelText("Always run BANKNIFTY")).not.toBeChecked();
    expect(screen.getByLabelText("Chain refresh (seconds)")).toHaveValue(5);
    expect(screen.getByLabelText("Toggle update on ticks")).toBeChecked();
    expect(screen.getByLabelText("Min interval between tick-driven updates (seconds)")).toHaveValue(1);
    expect(screen.getByLabelText("Stop idle on-demand chains after (minutes)")).toHaveValue(15);

    // Sessions table
    const niftyRow = screen.getByRole("cell", { name: "NIFTY" }).closest("tr");
    expect(niftyRow).not.toBeNull();
    expect(within(niftyRow as HTMLElement).getByText("running")).toBeInTheDocument();
    expect(within(niftyRow as HTMLElement).getByText("250")).toBeInTheDocument();

    const bankniftyRow = screen.getByRole("cell", { name: "BANKNIFTY" }).closest("tr");
    expect(within(bankniftyRow as HTMLElement).getByText("stopped")).toBeInTheDocument();
  });

  it("save sends the exact PUT body", async () => {
    apiMocks.getOptionsSettings.mockResolvedValue(baseSettings);
    apiMocks.updateOptionsSettings.mockResolvedValue({
      ...baseSettings,
      always_on: ["NIFTY", "BANKNIFTY"],
    });
    const user = userEvent.setup();

    renderWithQueryClient(<OptionsDataPanel />);

    const bankniftyCheckbox = await screen.findByLabelText("Always run BANKNIFTY");
    await user.click(bankniftyCheckbox);

    const reasonField = screen.getByLabelText("Reason for this change (optional)");
    await user.type(reasonField, "adding banknifty hedge");

    const saveButton = screen.getByRole("button", { name: "Save options data settings" });
    expect(saveButton).not.toBeDisabled();
    await user.click(saveButton);

    expect(apiMocks.updateOptionsSettings).toHaveBeenCalledWith({
      always_on: ["NIFTY", "BANKNIFTY"],
      cadence_sec: 5,
      tick_driven: true,
      min_interval_sec: 1,
      idle_stop_minutes: 15,
      reason: "adding banknifty hedge",
    });
  });

  it("blocks cadence 11 and a min interval greater than cadence", async () => {
    apiMocks.getOptionsSettings.mockResolvedValue(baseSettings);
    const user = userEvent.setup();

    renderWithQueryClient(<OptionsDataPanel />);

    const cadenceInput = await screen.findByLabelText("Chain refresh (seconds)");
    await user.clear(cadenceInput);
    await user.type(cadenceInput, "11");

    expect(
      screen.getByText("Chain refresh must be between 1 and 10 seconds."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save options data settings" })).toBeDisabled();

    // Back to a valid cadence, but push min interval above it.
    await user.clear(cadenceInput);
    await user.type(cadenceInput, "5");
    const minIntervalInput = screen.getByLabelText("Min interval between tick-driven updates (seconds)");
    await user.clear(minIntervalInput);
    await user.type(minIntervalInput, "9.5");

    expect(
      screen.getByText("Min interval cannot be greater than the chain refresh interval."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save options data settings" })).toBeDisabled();
  });

  it("does not allow selecting a fourth always-on underlying", async () => {
    apiMocks.getOptionsSettings.mockResolvedValue({
      ...baseSettings,
      always_on: ["NIFTY", "BANKNIFTY", "SENSEX"],
    });
    const user = userEvent.setup();

    renderWithQueryClient(<OptionsDataPanel />);

    const finniftyCheckbox = await screen.findByLabelText("Always run FINNIFTY");
    await user.click(finniftyCheckbox);

    expect(finniftyCheckbox).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Save options data settings" })).toBeDisabled();
  });
});
