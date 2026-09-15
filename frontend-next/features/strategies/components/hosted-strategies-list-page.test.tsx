import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedStrategiesListPage } from "./hosted-strategies-list-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/strategies",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchHostedOptions: vi.fn(),
  fetchHostedStrategies: vi.fn(),
  createHostedStrategy: vi.fn(),
  updateHostedStrategy: vi.fn(),
  createHostedVersion: vi.fn(),
  fetchHostedVersions: vi.fn(),
  fetchHostedJobs: vi.fn(),
  fetchHostedJob: vi.fn(),
  runHostedStrategy: vi.fn(),
  stopHostedJob: vi.fn(),
  fetchHostedJobLogs: vi.fn(),
  fetchHostedJobNotifications: vi.fn(),
  inspectHostedReconciliation: vi.fn(),
  reconcileHostedJob: vi.fn(),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import {
  createHostedStrategy,
  fetchHostedOptions,
  fetchHostedStrategies,
  updateHostedStrategy,
} from "@/lib/hosted-strategies/api";

function renderPage(ui: ReactElement = <HostedStrategiesListPage />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const OPTIONS = {
  account_scopes: ["kite:paper"],
  execution_modes: ["paper", "dry_run"],
  job_kinds: ["finite", "continuous"],
  stale_exit_policies: ["none", "flat"],
  hosted_execution_only: true,
};

describe("HostedStrategiesListPage", () => {
  beforeEach(() => {
    vi.mocked(fetchHostedOptions).mockReset();
    vi.mocked(fetchHostedStrategies).mockReset();
    vi.mocked(createHostedStrategy).mockReset();
    vi.mocked(updateHostedStrategy).mockReset();
  });

  it("submits the server-authorized account scope for a new strategy", async () => {
    vi.mocked(fetchHostedOptions).mockResolvedValue(OPTIONS);
    vi.mocked(fetchHostedStrategies).mockResolvedValue({ strategies: [] });
    vi.mocked(createHostedStrategy).mockResolvedValue({} as never);
    renderPage();
    expect(await screen.findByText("No hosted strategies yet")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/^name$/i), "NIFTY trend");
    await user.click(screen.getByRole("button", { name: /create strategy/i }));
    await waitFor(() => expect(createHostedStrategy).toHaveBeenCalled());
    const payload = vi.mocked(createHostedStrategy).mock.calls[0][0];
    expect(payload.account_scope).toBe("kite:paper");
    expect(payload.execution_mode).toBe("paper");
    expect(payload.job_kind).toBe("finite");
  });

  it("toggles enabled/disabled through the update endpoint", async () => {
    vi.mocked(fetchHostedOptions).mockResolvedValue(OPTIONS);
    vi.mocked(fetchHostedStrategies).mockResolvedValue({
      strategies: [
        {
          strategy_id: "s-1",
          owner_id: "owner",
          name: "NIFTY trend",
          template_id: "python-strategy",
          description: null,
          default_execution_mode: "paper",
          default_job_kind: "finite",
          default_account_scope: "kite:paper",
          max_duration_s: 21600,
          progress_deadline_s: 600,
          stale_exit_policy: "none",
          status: "active",
          created_at: null,
          updated_at: null,
        },
      ],
    });
    vi.mocked(updateHostedStrategy).mockResolvedValue({} as never);
    renderPage();
    expect(await screen.findByText("NIFTY trend")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /disable/i }));
    await waitFor(() =>
      expect(updateHostedStrategy).toHaveBeenCalledWith("s-1", { status: "disabled" }),
    );
  });
});
