import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedOptionsPanel, OptionsControls } from "./hosted-options-panel";
import { ApiClientError } from "@/lib/api/client";
import type {
  HostedJobSummary,
  OptionRun,
  OptionRunDetail,
  OptionRunList,
  OptionRunRepairAssessment,
} from "@/lib/hosted-strategies/types";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchHostedJobs: vi.fn(),
  stopHostedJob: vi.fn(),
  fetchOptionRuns: vi.fn(),
  fetchOptionRun: vi.fn(),
  fetchOptionRunRepair: vi.fn(),
  submitOptionRunRepair: vi.fn(),
}));

import {
  fetchHostedJobs,
  fetchOptionRun,
  fetchOptionRunRepair,
  fetchOptionRuns,
  submitOptionRunRepair,
} from "@/lib/hosted-strategies/api";

function optionRun(overrides: Partial<OptionRun> = {}): OptionRun {
  return {
    option_run_id: "opt_run_1",
    status: "partial_exit",
    structure_generation: 2,
    structure_digest: "sha-digest",
    underlying: "NIFTY",
    expiry: "2026-11-26",
    product: "NRML",
    protective_exit_unresolved: false,
    coverage: "known",
    legs: [
      {
        leg_id: "plan:1",
        tradingsymbol: "NIFTY26NOV22500CE",
        side: "SELL",
        role: "short",
        ratio: 1,
        quantity: 150,
        own_open_quantity: -150,
        state: "open",
      },
    ],
    repairable: true,
    protection_owner: null,
    ...overrides,
  };
}

function optionRunDetail(run: OptionRun): OptionRunDetail {
  return {
    run,
    edges: [{ plan_id: "plan-1", phase: "entry", created_at: "2026-09-25T10:00:00Z" }],
    frozen: { protection_policy: null, max_loss: null, expiry_policy: null },
    refusals: [],
    greeks: { available: false, reason: "no_reusable_read", delta: null, gamma: null, theta: null, vega: null },
    pnl: { available: false, reason: "no_reusable_read", premium: null, mtm: null },
  };
}

function repairAssessment(overrides: Partial<OptionRunRepairAssessment> = {}): OptionRunRepairAssessment {
  return {
    option_run_id: "opt_run_1",
    status: "partial_exit",
    state: "residual",
    reason_code: null,
    reasons: [],
    evidence_digest: "digest-abc",
    close_plan: [
      { tradingsymbol: "NIFTY26NOV22500CE", transaction_type: "BUY", quantity: 150, product: "NRML" },
    ],
    evidence: {},
    detail: {},
    ...overrides,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HostedOptionsPanel strategyId="s-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchHostedJobs).mockResolvedValue({ jobs: [] });
});

describe("hosted options panel: repair panel", () => {
  it("sends the assessment's evidence_digest and shows a 409 named refusal", async () => {
    const run = optionRun();
    vi.mocked(fetchOptionRuns).mockResolvedValue({
      strategy_id: "s-1",
      coverage: "known",
      coverage_reason: "",
      runs: [run],
    } satisfies OptionRunList);
    vi.mocked(fetchOptionRun).mockResolvedValue(optionRunDetail(run));
    vi.mocked(fetchOptionRunRepair).mockResolvedValue(repairAssessment());
    vi.mocked(submitOptionRunRepair).mockRejectedValue(
      new ApiClientError(
        409,
        { detail: { rejection_reason: "OPTION_RUN_REPAIR_EVIDENCE_CHANGED" } },
        "Conflict",
      ),
    );

    renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /details/i }));
    await screen.findByText(/close residual/i);

    await user.click(screen.getByRole("button", { name: /close residual/i }));
    await user.click(await screen.findByRole("button", { name: /^confirm$/i }));

    await waitFor(() =>
      expect(submitOptionRunRepair).toHaveBeenCalledWith("s-1", "opt_run_1", {
        action: "close_residual",
        evidence_digest: "digest-abc",
      }),
    );

    const error = await screen.findByTestId("option-repair-error");
    expect(error).toHaveTextContent(/evidence changed/i);
    expect(error).toHaveTextContent(/OPTION_RUN_REPAIR_EVIDENCE_CHANGED/);

    // Named 409 refusal re-reads the assessment.
    await waitFor(() => expect(fetchOptionRunRepair).toHaveBeenCalledTimes(2));
  });
});

describe("hosted options panel: coverage", () => {
  it("renders the incomplete-list warning under unknown coverage, not an empty state", async () => {
    vi.mocked(fetchOptionRuns).mockResolvedValue({
      strategy_id: "s-1",
      coverage: "unknown",
      coverage_reason: "reader_unavailable",
      runs: [],
    } satisfies OptionRunList);

    renderPanel();

    const warning = await screen.findByTestId("option-runs-coverage-warning");
    expect(warning).toHaveTextContent(/may be incomplete/i);
    expect(warning).toHaveTextContent(/reader_unavailable/);
    expect(screen.queryByText(/no option runs for this strategy/i)).not.toBeInTheDocument();
  });
});

describe("hosted options panel: controls", () => {
  function job(overrides: Partial<HostedJobSummary> = {}): HostedJobSummary {
    return {
      job_id: "job-1",
      strategy_id: "s-1",
      owner_id: "app:admin",
      attempt: 3,
      status: "running",
      desired_state: "running",
      execution_mode: "paper",
      account_scope: "kite:paper",
      run_id: "run-1",
      replacement_blocked: false,
      recovery_required_at: null,
      reconciled_at: null,
      created_at: null,
      updated_at: null,
      ...overrides,
    };
  }

  function renderControls(jobs: HostedJobSummary[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <OptionsControls strategyId="s-1" jobs={jobs} />
      </QueryClientProvider>,
    );
  }

  it("renders controls without a backend route as disabled with 'not available yet'", () => {
    renderControls([job()]);

    // The three controls with no owner-facing route stay disabled.
    for (const testId of [
      "option-control-cancel-pending",
      "option-control-exit-structure",
      "option-control-flatten",
    ]) {
      expect(screen.getByTestId(testId)).toBeDisabled();
    }
    expect(screen.getAllByText(/not available yet/i)).toHaveLength(3);

    // Stop evaluator has a real route (job stop) and an active job, so it is wired.
    expect(screen.getByTestId("option-control-stop-evaluator")).toBeEnabled();
  });

  it("disables stop evaluator too when there is no active attempt to stop", () => {
    renderControls([]);
    expect(screen.getByTestId("option-control-stop-evaluator")).toBeDisabled();
    expect(screen.getByText(/no active attempt to stop/i)).toBeInTheDocument();
  });
});
