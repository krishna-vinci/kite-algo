import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedOptionsPanel, OptionsControls, ResolveDeadSubmissionDialog } from "./hosted-options-panel";
import { ApiClientError } from "@/lib/api/client";
import type {
  DeadSubmissionEvidence,
  HostedJobSummary,
  HostedStrategy,
  OptionExitAssessment,
  OptionRun,
  OptionRunDetail,
  OptionRunList,
  OptionRunRepairAssessment,
  PendingWorkPreview,
} from "@/lib/hosted-strategies/types";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchHostedJobs: vi.fn(),
  fetchHostedStrategy: vi.fn(),
  stopHostedJob: vi.fn(),
  fetchOptionRuns: vi.fn(),
  fetchOptionRun: vi.fn(),
  fetchOptionRunRepair: vi.fn(),
  submitOptionRunRepair: vi.fn(),
  fetchPendingWork: vi.fn(),
  cancelPendingWork: vi.fn(),
  fetchDeadSubmission: vi.fn(),
  resolveDeadSubmission: vi.fn(),
  fetchOptionExit: vi.fn(),
  submitOptionExit: vi.fn(),
  fetchFlattenStatus: vi.fn(),
  submitFlatten: vi.fn(),
}));

import {
  cancelPendingWork,
  fetchDeadSubmission,
  fetchFlattenStatus,
  fetchHostedJobs,
  fetchHostedStrategy,
  fetchOptionExit,
  fetchOptionRun,
  fetchOptionRunRepair,
  fetchOptionRuns,
  fetchPendingWork,
  resolveDeadSubmission,
  submitFlatten,
  submitOptionExit,
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

function hostedStrategy(overrides: Partial<HostedStrategy> = {}): HostedStrategy {
  return {
    strategy_id: "s-1",
    owner_id: "app:admin",
    name: "Test Strategy",
    template_id: "template-1",
    description: null,
    default_execution_mode: "paper",
    default_job_kind: "continuous",
    default_account_scope: "kite:paper",
    max_duration_s: 3600,
    progress_deadline_s: 60,
    stale_exit_policy: "hold",
    status: "enabled",
    created_at: null,
    updated_at: null,
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
  vi.mocked(fetchHostedStrategy).mockResolvedValue(hostedStrategy());
  vi.mocked(fetchFlattenStatus).mockResolvedValue(null);
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

  it("renders a resolve trigger per unresolved step and opens it against that step's own ids", async () => {
    const run = optionRun();
    vi.mocked(fetchOptionRuns).mockResolvedValue({
      strategy_id: "s-1",
      coverage: "known",
      coverage_reason: "",
      runs: [run],
    } satisfies OptionRunList);
    vi.mocked(fetchOptionRun).mockResolvedValue(optionRunDetail(run));
    vi.mocked(fetchOptionRunRepair).mockResolvedValue(
      repairAssessment({
        state: "ambiguous",
        reason_code: "OPTION_RUN_REPAIR_AMBIGUOUS",
        reasons: ["adjust_in_flight"],
        unresolved_steps: [{ plan_id: "plan-adjust", step_no: 2, state: "submitted", order_id: null }],
      }),
    );
    vi.mocked(fetchDeadSubmission).mockResolvedValue({
      trail_state: "submitted",
      source: "paper_order",
      status: "submitted",
      filled_quantity: 0,
      remaining_quantity: 10,
      allowed_dispositions: [],
      evidence_digest: "dead-digest-step-2",
    });

    renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /details/i }));

    const section = await screen.findByTestId("option-resolve-dead-submission-opt_run_1");
    expect(section).toHaveTextContent("plan-adjust");
    expect(section).toHaveTextContent(/step 2/);
    expect(section).toHaveTextContent("submitted");

    const trigger = screen.getByTestId("option-resolve-dead-submission-trigger-plan-adjust-2");
    await user.click(trigger);

    await waitFor(() => expect(fetchDeadSubmission).toHaveBeenCalledWith("s-1", "plan-adjust", 2));
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

  function renderControls(jobs: HostedJobSummary[], strategyName = "Test Strategy") {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <OptionsControls strategyId="s-1" strategyName={strategyName} jobs={jobs} />
      </QueryClientProvider>,
    );
  }

  it("renders exit structure as an informational per-run pointer, and flatten as an enabled destructive control", () => {
    renderControls([job()]);

    // Exit structure now works per run (see the per-run panel), so the
    // top-level card is informational, not a disabled control.
    const exitCard = screen.getByTestId("option-control-exit-structure");
    expect(exitCard.tagName).not.toBe("BUTTON");
    expect(exitCard).toHaveTextContent(/details/i);
    expect(exitCard).toHaveTextContent(/exit structure/i);

    // Flatten (B2.6b S3) is wired: it opens the strong confirm dialog.
    expect(screen.getByTestId("option-control-flatten")).toBeEnabled();

    // Stop evaluator has a real route (job stop) and an active job, so it is wired.
    expect(screen.getByTestId("option-control-stop-evaluator")).toBeEnabled();

    // Cancel pending work (B2.6b §1) is wired: it opens the preview dialog.
    expect(screen.getByTestId("option-control-cancel-pending")).toBeEnabled();
  });

  it("disables stop evaluator too when there is no active attempt to stop", () => {
    renderControls([]);
    expect(screen.getByTestId("option-control-stop-evaluator")).toBeDisabled();
    expect(screen.getByText(/no active attempt to stop/i)).toBeInTheDocument();
  });

  function pendingWorkPreview(overrides: Partial<PendingWorkPreview> = {}): PendingWorkPreview {
    return {
      coverage: "known",
      evidence_digest: "pending-digest-1",
      items: [
        {
          plan_id: "plan-1",
          step_no: 1,
          order_id: "order-1",
          remaining_quantity: 50,
          eligibility: "eligible",
          reason_code: null,
        },
        {
          plan_id: "plan-2",
          step_no: 1,
          order_id: "order-2",
          remaining_quantity: 25,
          eligibility: "ineligible",
          reason_code: "CANCEL_PROTECTIVE_ORDER_FORBIDDEN",
        },
      ],
      ...overrides,
    };
  }

  it("cancel-pending preview: sends the preview's evidence_digest and separates the ineligible hedge", async () => {
    vi.mocked(fetchPendingWork).mockResolvedValue(pendingWorkPreview());
    vi.mocked(cancelPendingWork).mockResolvedValue({
      status: "accepted",
      action_id: "act-1",
      evidence_digest: "pending-digest-1",
      items: [],
    });

    renderControls([job()]);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("option-control-cancel-pending"));

    // The ineligible hedge is shown separated from eligible work, with its reason.
    const ineligibleRow = await screen.findByTestId("pending-work-ineligible-row");
    expect(ineligibleRow).toHaveTextContent("plan-2");
    expect(ineligibleRow).toHaveTextContent(/protective/i);
    expect(ineligibleRow).toHaveTextContent("CANCEL_PROTECTIVE_ORDER_FORBIDDEN");
    expect(screen.getByTestId("pending-work-eligible-row")).toHaveTextContent("plan-1");

    await user.click(screen.getByTestId("cancel-pending-confirm"));

    await waitFor(() =>
      expect(cancelPendingWork).toHaveBeenCalledWith("s-1", {
        evidence_digest: "pending-digest-1",
        reason: "owner_cancel",
      }),
    );
  });

  it("cancel-pending preview: shows a 409 CANCEL_EVIDENCE_CHANGED refusal inline with a refresh prompt", async () => {
    vi.mocked(fetchPendingWork).mockResolvedValue(pendingWorkPreview());
    vi.mocked(cancelPendingWork).mockRejectedValue(
      new ApiClientError(409, { detail: { rejection_reason: "CANCEL_EVIDENCE_CHANGED" } }, "Conflict"),
    );

    renderControls([job()]);
    const user = userEvent.setup();

    await user.click(screen.getByTestId("option-control-cancel-pending"));
    await screen.findByTestId("pending-work-eligible-row");

    await user.click(screen.getByTestId("cancel-pending-confirm"));

    const error = await screen.findByTestId("cancel-pending-error");
    expect(error).toHaveTextContent(/CANCEL_EVIDENCE_CHANGED/);
    expect(screen.getByTestId("cancel-pending-refresh")).toBeInTheDocument();

    // The refusal re-reads the preview.
    await waitFor(() => expect(fetchPendingWork).toHaveBeenCalledTimes(2));
  });
});

describe("hosted options panel: dead-submission disposition", () => {
  function deadSubmissionEvidence(
    overrides: Partial<DeadSubmissionEvidence> = {},
  ): DeadSubmissionEvidence {
    return {
      trail_state: "submitted",
      source: "paper_order",
      status: "cancelled",
      filled_quantity: 3,
      remaining_quantity: 0,
      allowed_dispositions: ["cancelled"],
      evidence_digest: "dead-digest-1",
      ...overrides,
    };
  }

  function renderDialog() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onOpenChange = vi.fn();
    render(
      <QueryClientProvider client={client}>
        <ResolveDeadSubmissionDialog
          strategyId="s-1"
          planId="plan-9"
          stepNo={2}
          open
          onOpenChange={onOpenChange}
        />
      </QueryClientProvider>,
    );
    return { onOpenChange };
  }

  it("offers only the server-allowed dispositions and posts the digest", async () => {
    vi.mocked(fetchDeadSubmission).mockResolvedValue(
      deadSubmissionEvidence({ allowed_dispositions: ["cancelled", "failed_residual_abandoned"] }),
    );
    vi.mocked(resolveDeadSubmission).mockResolvedValue({
      status: "complete",
      action_id: "act-2",
      evidence_digest: "dead-digest-1",
    });

    renderDialog();
    const user = userEvent.setup();

    await screen.findByTestId("dead-submission-disposition-cancelled");
    expect(screen.getByTestId("dead-submission-disposition-failed_residual_abandoned")).toBeInTheDocument();
    // Only the two server-named outcomes are offered, never every possible one.
    expect(screen.queryByTestId("dead-submission-disposition-filled")).not.toBeInTheDocument();
    expect(screen.queryByTestId("dead-submission-disposition-rejected")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("dead-submission-disposition-cancelled"));
    await user.type(screen.getByLabelText(/reason/i), "confirmed terminal cancel");
    await user.click(screen.getByTestId("dead-submission-confirm"));

    await waitFor(() =>
      expect(resolveDeadSubmission).toHaveBeenCalledWith("s-1", "plan-9", 2, {
        evidence_digest: "dead-digest-1",
        disposition: "cancelled",
        reason: "confirmed terminal cancel",
      }),
    );
  });
});

describe("hosted options panel: exit structure (B2.6b S2)", () => {
  function exitAssessment(overrides: Partial<OptionExitAssessment> = {}): OptionExitAssessment {
    return {
      adjust_owner_state: "finished",
      protective_stage_state: "resolved",
      state: "residual",
      close_plan: [
        { tradingsymbol: "NIFTY26NOV22500CE", transaction_type: "BUY", quantity: 150, product: "NRML" },
      ],
      evidence_digest: "exit-digest-1",
      reasons: [],
      reason_code: null,
      ...overrides,
    };
  }

  async function openExitDialog(run: OptionRun) {
    vi.mocked(fetchOptionRuns).mockResolvedValue({
      strategy_id: "s-1",
      coverage: "known",
      coverage_reason: "",
      runs: [run],
    } satisfies OptionRunList);
    vi.mocked(fetchOptionRun).mockResolvedValue(optionRunDetail(run));

    renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /details/i }));
    await user.click(
      await screen.findByTestId(`option-exit-structure-trigger-${run.option_run_id}`),
    );
    // Wait for the assessment to load (the close plan renders) before the
    // caller clicks confirm, so the button is not still disabled mid-fetch.
    await screen.findByText(/close plan \(shorts first\)/i);
    await waitFor(() => expect(screen.getByRole("button", { name: /^confirm exit$/i })).toBeEnabled());
    return user;
  }

  it("POST carries the digest read from the GET assessment", async () => {
    const run = optionRun({ repairable: false });
    vi.mocked(fetchOptionExit).mockResolvedValue(exitAssessment());
    vi.mocked(submitOptionExit).mockResolvedValue({
      status: "complete",
      action_id: "act-9",
      evidence_digest: "exit-digest-1",
      items: [],
    });

    const user = await openExitDialog(run);
    await user.click(screen.getByRole("button", { name: /^confirm exit$/i }));

    await waitFor(() =>
      expect(submitOptionExit).toHaveBeenCalledWith("s-1", "opt_run_1", {
        evidence_digest: "exit-digest-1",
        reason: "owner_exit",
      }),
    );
    expect(await screen.findByTestId("option-exit-structure-complete-message")).toHaveTextContent(
      /run exited/i,
    );
  });

  it("shows a 409 OPTION_RUN_ADJUST_IN_FLIGHT refusal inline and re-reads the assessment", async () => {
    const run = optionRun({ repairable: false });
    vi.mocked(fetchOptionExit).mockResolvedValue(exitAssessment());
    vi.mocked(submitOptionExit).mockRejectedValue(
      new ApiClientError(409, { detail: { rejection_reason: "OPTION_RUN_ADJUST_IN_FLIGHT" } }, "Conflict"),
    );

    const user = await openExitDialog(run);
    await user.click(screen.getByRole("button", { name: /^confirm exit$/i }));

    const error = await screen.findByTestId("option-exit-structure-error-opt_run_1");
    expect(error).toHaveTextContent(/adjustment is still in flight/i);
    expect(error).toHaveTextContent("OPTION_RUN_ADJUST_IN_FLIGHT");

    await waitFor(() => expect(fetchOptionExit).toHaveBeenCalledTimes(2));
  });

  it("'accepted' shows the multi-stage message, not an exited run", async () => {
    const run = optionRun({ repairable: false });
    vi.mocked(fetchOptionExit).mockResolvedValue(exitAssessment());
    vi.mocked(submitOptionExit).mockResolvedValue({
      status: "accepted",
      action_id: "act-10",
      evidence_digest: "exit-digest-1",
      items: exitAssessment().close_plan,
    });

    const user = await openExitDialog(run);
    await user.click(screen.getByRole("button", { name: /^confirm exit$/i }));

    const stageMessage = await screen.findByTestId("option-exit-structure-stage-message");
    expect(stageMessage).toHaveTextContent(/stays open/i);
    expect(screen.queryByTestId("option-exit-structure-complete-message")).not.toBeInTheDocument();
    expect(screen.queryByText(/run exited/i)).not.toBeInTheDocument();
  });
});

describe("hosted options panel: flatten (B2.6b S3)", () => {
  function renderControls(jobs: HostedJobSummary[] = []) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <OptionsControls strategyId="s-1" strategyName="Test Strategy" jobs={jobs} />
      </QueryClientProvider>,
    );
  }

  it("refuses until the strategy name is typed exactly, then POSTs {reason, stop_evaluator: true}", async () => {
    vi.mocked(fetchFlattenStatus).mockResolvedValue(null);
    vi.mocked(submitFlatten).mockResolvedValue({ status: "accepted", action_id: "flat-1", items: [] });

    renderControls();
    const user = userEvent.setup();

    await user.click(screen.getByTestId("option-control-flatten"));
    const input = await screen.findByTestId("flatten-confirm-name-input");

    // Wrong text keeps the destructive action refused — no POST fires.
    await user.type(input, "not the name");
    expect(screen.getByTestId("flatten-confirm")).toBeDisabled();
    await user.click(screen.getByTestId("flatten-confirm"));
    expect(submitFlatten).not.toHaveBeenCalled();

    await user.clear(input);
    await user.type(input, "Test Strategy");
    expect(screen.getByTestId("flatten-confirm")).toBeEnabled();

    await user.click(screen.getByTestId("flatten-confirm"));

    await waitFor(() =>
      expect(submitFlatten).toHaveBeenCalledWith("s-1", { reason: "owner_flatten", stop_evaluator: true }),
    );
  });

  it("renders a blocked manifest item's own reason (label derived from kind + detail) and offers Resume flatten", async () => {
    vi.mocked(fetchFlattenStatus).mockResolvedValue(null);
    vi.mocked(submitFlatten).mockResolvedValue({
      status: "blocked",
      action_id: "flat-2",
      items: [
        {
          kind: "option_exit",
          key: "option_exit:opt_run_1",
          state: "blocked",
          reason_code: "OPTION_RUN_EVIDENCE_AMBIGUOUS",
          detail: { option_run_id: "opt_run_1", rejection_reason: "OPTION_RUN_EVIDENCE_AMBIGUOUS" },
        },
        {
          kind: "cancel_pending",
          key: "cancel:plan-entry:1",
          state: "done",
          reason_code: null,
          detail: { plan_id: "plan-entry", step_no: 1, order_id: "o-1" },
        },
      ],
    });

    renderControls();
    const user = userEvent.setup();

    await user.click(screen.getByTestId("option-control-flatten"));
    await user.type(await screen.findByTestId("flatten-confirm-name-input"), "Test Strategy");
    await user.click(screen.getByTestId("flatten-confirm"));

    // Label is derived from kind + detail, not a server-sent label field.
    expect(await screen.findByText("Option run opt_run_1")).toBeInTheDocument();
    expect(screen.getByText("Pending entry plan-entry step 1")).toBeInTheDocument();

    const blocked = screen.getByTestId("flatten-item-blocked-option_exit:opt_run_1");
    expect(blocked).toHaveTextContent(/cannot read this run's own fills/i);
    expect(blocked).toHaveTextContent("OPTION_RUN_EVIDENCE_AMBIGUOUS");
    expect(screen.getByTestId("flatten-resume")).toBeInTheDocument();
    // A resume does not require retyping the strategy name.
    expect(screen.queryByTestId("flatten-confirm-name-input")).not.toBeInTheDocument();
  });

  it("shows a 409 FLATTEN_EVALUATION_ACTIVE refusal inline", async () => {
    vi.mocked(fetchFlattenStatus).mockResolvedValue(null);
    vi.mocked(submitFlatten).mockRejectedValue(
      new ApiClientError(409, { detail: { rejection_reason: "FLATTEN_EVALUATION_ACTIVE" } }, "Conflict"),
    );

    renderControls();
    const user = userEvent.setup();

    await user.click(screen.getByTestId("option-control-flatten"));
    await user.type(await screen.findByTestId("flatten-confirm-name-input"), "Test Strategy");
    await user.click(screen.getByTestId("flatten-confirm"));

    const error = await screen.findByTestId("flatten-error");
    expect(error).toHaveTextContent(/active evaluation could not be proven stopped/i);
    expect(error).toHaveTextContent("FLATTEN_EVALUATION_ACTIVE");
  });

  it("reads the DEAD_SUBMISSION_UNRESOLVED 409's own steps and protective_stages, not unresolved_steps", async () => {
    vi.mocked(fetchFlattenStatus).mockResolvedValue(null);
    vi.mocked(submitFlatten).mockRejectedValue(
      new ApiClientError(
        409,
        {
          detail: {
            rejection_reason: "DEAD_SUBMISSION_UNRESOLVED",
            strategy_id: "s-1",
            steps: [{ plan_id: "plan-adjust", step_no: 2 }],
            protective_stages: [{ stage_id: "stage-1", state: "sending" }],
            waiting: [],
            message: "Resolve before flatten.",
          },
        },
        "Conflict",
      ),
    );

    renderControls();
    const user = userEvent.setup();

    await user.click(screen.getByTestId("option-control-flatten"));
    await user.type(await screen.findByTestId("flatten-confirm-name-input"), "Test Strategy");
    await user.click(screen.getByTestId("flatten-confirm"));

    const stepsBlock = await screen.findByTestId("flatten-unresolved-steps");
    expect(stepsBlock).toHaveTextContent("plan-adjust");
    expect(stepsBlock).toHaveTextContent(/step 2/);
    expect(screen.getByTestId("flatten-protective-stages")).toHaveTextContent("stage-1");
  });

  it("shows the done-conditions checklist, what's missing, and the stop state from the response envelope", async () => {
    vi.mocked(fetchFlattenStatus).mockResolvedValue(null);
    vi.mocked(submitFlatten).mockResolvedValue({
      status: "blocked",
      action_id: "flat-3",
      items: [],
      stop: {
        requested: true,
        state: "confirmed",
        jobs: ["job-1"],
        approvals: [],
        requested_by: "app:admin",
        reason: "owner_flatten",
      },
      missing: ["option_runs_flat", "books_zero"],
      done_conditions: {
        no_qualifying_pending_entry: true,
        no_live_unresolved_submission: true,
        option_runs_flat: false,
        books_zero: false,
        no_live_evaluation_authority: true,
        no_in_flight_governed_work: true,
      },
    });

    renderControls();
    const user = userEvent.setup();

    await user.click(screen.getByTestId("option-control-flatten"));
    await user.type(await screen.findByTestId("flatten-confirm-name-input"), "Test Strategy");
    await user.click(screen.getByTestId("flatten-confirm"));

    const stopState = await screen.findByTestId("flatten-stop-state");
    expect(stopState).toHaveTextContent(/requested/i);
    expect(stopState).toHaveTextContent("confirmed");

    const conditions = screen.getByTestId("flatten-done-conditions");
    expect(conditions).toHaveTextContent(/every option run's own fills are flat/i);

    expect(screen.getByTestId("flatten-missing")).toHaveTextContent(/every option run's own fills are flat/i);
  });
});

describe("hosted options panel: protection owner", () => {
  it("renders the unknown-ownership warning instead of the old placeholder", async () => {
    const run = optionRun({ protection_owner: { state: "unknown" } });
    vi.mocked(fetchOptionRuns).mockResolvedValue({
      strategy_id: "s-1",
      coverage: "known",
      coverage_reason: "",
      runs: [run],
    } satisfies OptionRunList);

    renderPanel();

    const warning = await screen.findByTestId(`protection-owner-unknown-${run.option_run_id}`);
    expect(warning).toHaveTextContent(/could not read who owns/i);
    expect(warning).toHaveTextContent(/new exposure is blocked/i);
  });

  it("renders the neutral 'no protection owner' line when there is none", async () => {
    const run = optionRun({ protection_owner: null });
    vi.mocked(fetchOptionRuns).mockResolvedValue({
      strategy_id: "s-1",
      coverage: "known",
      coverage_reason: "",
      runs: [run],
    } satisfies OptionRunList);

    renderPanel();

    expect(await screen.findByTestId(`protection-owner-none-${run.option_run_id}`)).toHaveTextContent(
      /no protection owner/i,
    );
  });
});
