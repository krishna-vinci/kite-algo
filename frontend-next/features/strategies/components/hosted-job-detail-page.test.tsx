import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedJobDetailPage } from "./hosted-job-detail-page";
import type { HostedJobDetail } from "@/lib/hosted-strategies/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/strategies/s-1/jobs/j-1",
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
  fetchHostedJob,
  fetchHostedJobLogs,
  fetchHostedJobNotifications,
  inspectHostedReconciliation,
  stopHostedJob,
} from "@/lib/hosted-strategies/api";

function renderPage(ui: ReactElement = <HostedJobDetailPage strategyId="s-1" jobId="j-1" />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function job(partial: Partial<HostedJobDetail> = {}): HostedJobDetail {
  return {
    job_id: "j-1",
    strategy_id: "s-1",
    owner_id: "owner",
    attempt: 2,
    status: "stopped",
    desired_state: "stopped",
    execution_mode: "paper",
    account_scope: "kite:paper",
    run_id: "run-1",
    replacement_blocked: true,
    recovery_required_at: null,
    reconciled_at: null,
    created_at: null,
    updated_at: null,
    handoff_at: "2026-09-15T04:00:00Z",
    process_cleanup_state: "unconfirmed",
    process_cleanup_at: null,
    process_cleanup_actor: null,
    last_progress_at: null,
    version_id: "v-1",
    token_present: false,
    stop_requested_at: "2026-09-15T04:05:00Z",
    stop_requested_by: "owner",
    stop: {
      requested: true,
      state: "cleanup_unresolved",
      requested_at: "2026-09-15T04:05:00Z",
      requested_by: "owner",
      replacement_blocked: true,
      note: "Terminal, but child process cleanup is not confirmed. Unknown is not 'stopped'.",
    },
    logs_discarded: true,
    logs_source: "post_termination",
    ...partial,
  };
}

describe("HostedJobDetailPage", () => {
  beforeEach(() => {
    vi.mocked(fetchHostedJob).mockReset();
    vi.mocked(fetchHostedJobLogs).mockReset();
    vi.mocked(fetchHostedJobNotifications).mockReset();
    vi.mocked(inspectHostedReconciliation).mockReset();
    vi.mocked(stopHostedJob).mockReset();
    vi.mocked(fetchHostedJobNotifications).mockResolvedValue({ job_id: "j-1", run_id: "run-1", events: [] });
  });

  it("offers Stop for a queued (unlaunched) attempt", async () => {
    vi.mocked(fetchHostedJob).mockResolvedValue(
      job({
        status: "queued",
        desired_state: "started",
        handoff_at: null,
        run_id: null,
        replacement_blocked: true,
        process_cleanup_state: null,
        stop_requested_at: null,
        stop_requested_by: null,
        stop: {
          requested: false,
          state: "none",
          requested_at: null,
          requested_by: null,
          replacement_blocked: true,
          note: "Queued; no stop requested. Stop does not cancel orders or flatten.",
        },
      }),
    );
    vi.mocked(fetchHostedJobLogs).mockResolvedValue({
      job_id: "j-1",
      available: false,
      truncated: false,
      source: null,
      next_seq: 0,
      entries: [],
      notice: "No child was launched for this attempt; no logs exist.",
    });
    vi.mocked(stopHostedJob).mockResolvedValue({} as never);
    vi.mocked(inspectHostedReconciliation).mockResolvedValue({
      job_id: "j-1",
      strategy_id: "s-1",
      attempt: 2,
      replacement_blocked: true,
      assessment: {
        allowed: false,
        case: "blocked",
        reason_code: "HOSTED_JOB_ACTIVE",
        blocking_reasons: ["HOSTED_JOB_ACTIVE"],
        notes: [],
      },
      evidence: {},
      history: [],
    });
    renderPage();

    expect(await screen.findByText(/Queued; no stop requested/)).toBeInTheDocument();
    expect(screen.getByText(/never launched; stopping it prevents launch/)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /stop attempt #2/i }));
    await waitFor(() => expect(stopHostedJob).toHaveBeenCalledWith("s-1", "j-1", { attempt: 2 }));
  });

  it("shows unconfirmed cleanup as not confirmed stopped", async () => {
    vi.mocked(fetchHostedJob).mockResolvedValue(job());
    vi.mocked(fetchHostedJobLogs).mockResolvedValue({
      job_id: "j-1",
      available: false,
      truncated: false,
      source: null,
      next_seq: 0,
      entries: [],
      notice: "Logs not collected (the supervisor may be unavailable or the child produced no output).",
    });
    vi.mocked(inspectHostedReconciliation).mockResolvedValue({
      job_id: "j-1",
      strategy_id: "s-1",
      attempt: 2,
      replacement_blocked: true,
      assessment: {
        allowed: false,
        case: "blocked",
        reason_code: "EXECUTION_QUIESCENCE_UNVERIFIED",
        blocking_reasons: ["EXECUTION_QUIESCENCE_UNVERIFIED"],
        notes: [],
      },
      evidence: {},
      history: [],
    });
    renderPage();

    expect(await screen.findByText(/Cleanup unresolved/)).toBeInTheDocument();
    expect(screen.getByText(/Unknown cleanup is not confirmed stopped/)).toBeInTheDocument();
    expect(await screen.findByText(/Logs not collected/)).toBeInTheDocument();
  });

  it("surfaces discarded output and treats quiescence as a non-dismissible block", async () => {
    vi.mocked(fetchHostedJob).mockResolvedValue(job({ logs_discarded: true }));
    vi.mocked(fetchHostedJobLogs).mockResolvedValue({
      job_id: "j-1",
      available: true,
      truncated: true,
      source: "post_termination",
      next_seq: 3,
      entries: [
        { seq: 1, content: "line one\n", created_at: null },
        { seq: 2, content: "line two\n", created_at: null },
      ],
      notice: "Logs were collected after the child terminated.",
    });
    vi.mocked(inspectHostedReconciliation).mockResolvedValue({
      job_id: "j-1",
      strategy_id: "s-1",
      attempt: 2,
      replacement_blocked: true,
      assessment: {
        allowed: false,
        case: "blocked",
        reason_code: "EXECUTION_QUIESCENCE_UNVERIFIED",
        blocking_reasons: ["EXECUTION_QUIESCENCE_UNVERIFIED"],
        notes: [],
      },
      evidence: { execution: "unknown" },
      history: [],
    });
    renderPage();

    expect(await screen.findByText(/Output was truncated/)).toBeInTheDocument();
    expect(await screen.findByText(/not dismissible/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /request reconciliation/i })).toBeDisabled();
  });
});
