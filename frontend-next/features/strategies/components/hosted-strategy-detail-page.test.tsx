import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedStrategyDetailPage } from "./hosted-strategy-detail-page";
import { ApiClientError } from "@/lib/api/client";
import type { HostedStrategy, HostedStrategyOptions, HostedJobSummary } from "@/lib/hosted-strategies/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/strategies/s-1",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchHostedOptions: vi.fn(),
  fetchHostedStrategies: vi.fn(),
  fetchHostedStrategy: vi.fn(),
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
  fetchHostedJobs,
  fetchHostedOptions,
  fetchHostedStrategy,
  fetchHostedVersions,
  runHostedStrategy,
} from "@/lib/hosted-strategies/api";
import { toast } from "sonner";

function renderPage(ui: ReactElement = <HostedStrategyDetailPage strategyId="s-1" />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function strategy(partial: Partial<HostedStrategy> = {}): HostedStrategy {
  return {
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
    ...partial,
  };
}

function job(partial: Partial<HostedJobSummary> = {}): HostedJobSummary {
  return {
    job_id: "j-1",
    strategy_id: "s-1",
    owner_id: "owner",
    attempt: 2,
    status: "running",
    desired_state: "started",
    execution_mode: "paper",
    account_scope: "kite:paper",
    run_id: "run-1",
    replacement_blocked: true,
    recovery_required_at: null,
    reconciled_at: null,
    created_at: null,
    updated_at: null,
    ...partial,
  };
}

function options(partial: Partial<HostedStrategyOptions> = {}): HostedStrategyOptions {
  return {
    account_scopes: ["kite:paper"],
    execution_modes: ["paper", "dry_run"],
    job_kinds: ["finite", "continuous"],
    stale_exit_policies: ["none"],
    hosted_execution_only: true,
    ...partial,
  };
}

const VERSION = {
  version_id: "v-1",
  strategy_id: "s-1",
  version: 1,
  source: "def main(ctx):\n    return 0\n",
  source_sha256: "a".repeat(64),
  parameters_schema: { type: "object", properties: {}, required: [] },
  capabilities_snapshot: {},
  created_by: "owner",
  created_at: null,
};

function prime(partial: { strategy?: Partial<HostedStrategy>; options?: Partial<HostedStrategyOptions>; jobs?: HostedJobSummary[] }) {
  vi.mocked(fetchHostedStrategy).mockResolvedValue(strategy(partial.strategy));
  vi.mocked(fetchHostedOptions).mockResolvedValue(options(partial.options));
  vi.mocked(fetchHostedVersions).mockResolvedValue({ versions: [VERSION] });
  vi.mocked(fetchHostedJobs).mockResolvedValue({ jobs: partial.jobs ?? [] });
}

describe("HostedStrategyDetailPage run-now gating", () => {
  beforeEach(() => {
    vi.mocked(fetchHostedStrategy).mockReset();
    vi.mocked(fetchHostedOptions).mockReset();
    vi.mocked(fetchHostedVersions).mockReset();
    vi.mocked(fetchHostedJobs).mockReset();
    vi.mocked(runHostedStrategy).mockReset();
    vi.mocked(toast.error).mockReset();
  });

  it("keeps a live strategy in live mode and disables Run now when the deployment does not offer live", async () => {
    prime({ strategy: { default_execution_mode: "live", default_account_scope: "kite:live" } });
    renderPage();

    expect(await screen.findByTestId("run-now-mode")).toHaveTextContent("Live");
    expect(await screen.findByText(/supported here: Paper, Dry run/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("run-now-blocked-reason")).toHaveTextContent(
        /Live execution is not enabled on this deployment/,
      ),
    );
    expect(screen.getByRole("button", { name: /run now/i })).toBeDisabled();
    // The stored live strategy stays visible; it was not rewritten to paper.
    expect(screen.getByText(/Live · finite/)).toBeInTheDocument();
  });

  it("enables Run now for a live strategy and labels the lanes the server supports", async () => {
    prime({
      strategy: { default_execution_mode: "live", default_account_scope: "kite:live" },
      options: {
        execution_modes: ["paper", "dry_run", "live"],
        live_lanes: ["cnc", "mis", "futures", "options"],
        live_requires_owner_approval: true,
      },
    });
    renderPage();

    expect(await screen.findByTestId("run-now-mode")).toHaveTextContent("Live");
    expect(
      await screen.findByText(/CNC \/ portfolio · MIS · Futures \/ rolls · Options/),
    ).toBeInTheDocument();
    expect(screen.getByText(/explicit approval of the strategy's plan/)).toBeInTheDocument();
    expect(screen.getByText(/never approves a plan automatically/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /run now/i })).toBeEnabled());
  });

  it("reports an unreported lane list as unknown instead of inventing one", async () => {
    prime({
      strategy: { default_execution_mode: "live", default_account_scope: "kite:live" },
      options: { execution_modes: ["paper", "dry_run", "live"] },
    });
    renderPage();

    expect(await screen.findByTestId("run-now-mode")).toHaveTextContent("Live");
    expect(await screen.findByText(/were not reported by this server/)).toBeInTheDocument();
  });

  it("leaves a paper strategy runnable while live is disabled", async () => {
    prime({});
    renderPage();

    expect(await screen.findByTestId("run-now-mode")).toHaveTextContent("Paper");
    await waitFor(() => expect(screen.getByRole("button", { name: /run now/i })).toBeEnabled());
    expect(screen.queryByTestId("run-now-blocked-reason")).not.toBeInTheDocument();
  });

  it("disables Run now for a running attempt and names the job status, not the capability", async () => {
    prime({ jobs: [job({ status: "running", replacement_blocked: true })] });
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("run-now-blocked-reason")).toHaveTextContent(
        /Attempt #2 is running/,
      ),
    );
    expect(screen.getByTestId("run-now-blocked-reason")).not.toHaveTextContent(
      /not enabled on this deployment/,
    );
    expect(screen.getByTestId("run-now-blocked-reason")).not.toHaveTextContent(
      /Waiting for this deployment's supported execution modes/,
    );
    expect(screen.getByRole("button", { name: /run now/i })).toBeDisabled();
  });

  it("does not change the pinned mode if the options refresh stops offering live", async () => {
    prime({
      strategy: { default_execution_mode: "live", default_account_scope: "kite:live" },
      options: {
        execution_modes: ["paper", "dry_run", "live"],
        live_lanes: ["cnc"],
        live_requires_owner_approval: true,
      },
    });
    const { rerender } = renderPage();
    expect(await screen.findByTestId("run-now-mode")).toHaveTextContent("Live");

    // The deployment flips live off and the options query refetches without it.
    vi.mocked(fetchHostedOptions).mockResolvedValue(options());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    rerender(
      <QueryClientProvider client={client}>
        <HostedStrategyDetailPage strategyId="s-1" />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("run-now-mode")).toHaveTextContent("Live"),
    );
    expect(screen.queryByTestId("run-now-mode")).not.toHaveTextContent("Paper");
  });

  it("still surfaces a server refusal on a launch that the client believed was offered", async () => {
    prime({
      strategy: { default_execution_mode: "live", default_account_scope: "kite:live" },
      options: {
        execution_modes: ["paper", "dry_run", "live"],
        live_lanes: ["cnc"],
        live_requires_owner_approval: true,
      },
    });
    vi.mocked(runHostedStrategy).mockRejectedValue(
      new ApiClientError(409, { detail: "STRATEGY_BLOCKED" }),
    );
    renderPage();

    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: /run now/i });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(vi.mocked(toast.error).mock.calls[0][0]).toMatch(/STRATEGY_BLOCKED/);
  });

  it("holds the launch when the mode capability could not be loaded", async () => {
    vi.mocked(fetchHostedStrategy).mockResolvedValue(strategy());
    vi.mocked(fetchHostedOptions).mockRejectedValue(new Error("options unavailable"));
    vi.mocked(fetchHostedVersions).mockResolvedValue({ versions: [VERSION] });
    vi.mocked(fetchHostedJobs).mockResolvedValue({ jobs: [] });
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("run-now-blocked-reason")).toHaveTextContent(
        /supported execution modes could not be loaded/,
      ),
    );
    expect(screen.getByRole("button", { name: /run now/i })).toBeDisabled();
  });
});
