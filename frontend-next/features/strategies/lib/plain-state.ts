/**
 * Five plain states for a hosted strategy.
 *
 * This is a pure mapper: strategy status is not read here (enable/disable is
 * shown separately), only the strategy's latest job/attempt, its execution
 * requests and its option runs. It exists so the list and the detail page can
 * show ONE plain sentence and ONE primary action instead of raw job/request
 * codes; the raw codes stay available behind a "details" toggle in the UI.
 *
 * Precedence (highest wins, top to bottom) — a strategy can have several
 * signals at once, and only the most urgent one is shown:
 *
 * 1. Error             — the last attempt itself failed.
 * 2. Needs attention    — the attempt is hung or unreconciled, a dispatch
 *                         outcome is unresolved, or an option run needs
 *                         governed repair. All of these need the owner to DO
 *                         something to a broken or stuck state.
 * 3. Waiting for you    — a live plan is awaiting approval, or an option run
 *                         has reached a cleanup point that needs a decision.
 *                         Nothing is broken; the owner's decision is what
 *                         moves it forward.
 * 4. Running            — the attempt is queued, starting, running or
 *                         winding down (fencing): it is active.
 * 5. Stopped            — no attempt is active and nothing above applies,
 *                         whether the last attempt finished cleanly or the
 *                         strategy has never run.
 */

export type PlainStateKind = "running" | "stopped" | "waiting_for_you" | "needs_attention" | "error";

export type PlainStateActionKind = "run_now" | "view_job" | "review_approvals" | "review";

export type PlainStateAction = {
  label: string;
  href?: string;
  kind?: PlainStateActionKind;
};

export type PlainState = {
  state: PlainStateKind;
  /** Display label matching `state` ("Running", "Stopped", …). */
  label: string;
  sentence: string;
  action?: PlainStateAction;
};

export const PLAIN_STATE_LABELS: Record<PlainStateKind, string> = {
  running: "Running",
  stopped: "Stopped",
  waiting_for_you: "Waiting for you",
  needs_attention: "Needs attention",
  error: "Error",
};

/** Job statuses the supervisor reports; anything else is treated as absent. */
const RUNNING_JOB_STATUSES = new Set(["queued", "starting", "running", "fencing"]);

export type PlainJobSignal = {
  status: string;
  job_id?: string | null;
};

export type PlainRequestSignal = {
  status: string;
};

export type PlainOptionRunSignal = {
  status?: string;
  repairable?: boolean;
};

export type PlainStateInput = {
  strategyId: string;
  /** The strategy's most recent attempt, or `null`/absent if it never ran. */
  job?: PlainJobSignal | null;
  /** All execution requests worth checking (recent ones are enough). */
  executionRequests?: readonly PlainRequestSignal[];
  /** All option runs worth checking. */
  optionRuns?: readonly PlainOptionRunSignal[];
};

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

export function plainStrategyState(input: PlainStateInput): PlainState {
  const jobStatus = String(input.job?.status ?? "").trim();
  const requests = input.executionRequests ?? [];
  const optionRuns = input.optionRuns ?? [];
  const jobHref = input.job?.job_id
    ? `/strategies/${input.strategyId}/jobs/${input.job.job_id}`
    : `/strategies/${input.strategyId}`;

  if (jobStatus === "failed") {
    return {
      state: "error",
      label: PLAIN_STATE_LABELS.error,
      sentence: "Error: the last attempt failed.",
      action: { label: "View job", href: jobHref, kind: "view_job" },
    };
  }

  const dispatchUnresolvedCount = requests.filter((row) => row.status === "dispatch_unresolved").length;
  const repairCount = optionRuns.filter((row) => row.repairable === true).length;
  if (jobStatus === "hung" || jobStatus === "recovery_required" || dispatchUnresolvedCount > 0 || repairCount > 0) {
    const reasons: string[] = [];
    if (jobStatus === "hung") reasons.push("the attempt stopped reporting progress");
    if (jobStatus === "recovery_required") reasons.push("the last attempt needs reconciliation");
    if (dispatchUnresolvedCount > 0) {
      reasons.push(`${plural(dispatchUnresolvedCount, "request")} with an unresolved outcome`);
    }
    if (repairCount > 0) reasons.push(`${plural(repairCount, "option run")} needing repair`);
    return {
      state: "needs_attention",
      label: PLAIN_STATE_LABELS.needs_attention,
      sentence: `Needs attention: ${reasons.join(", ")}.`,
      action: { label: "Review", href: jobHref, kind: "review" },
    };
  }

  const awaitingCount = requests.filter((row) => row.status === "awaiting_approval").length;
  const cleanupCount = optionRuns.filter((row) => row.status === "cleanup_required").length;
  if (awaitingCount > 0 || cleanupCount > 0) {
    const reasons: string[] = [];
    if (awaitingCount > 0) reasons.push(`approve ${plural(awaitingCount, "live plan")}`);
    if (cleanupCount > 0) reasons.push(`resolve ${plural(cleanupCount, "option run")} pending cleanup`);
    return {
      state: "waiting_for_you",
      label: PLAIN_STATE_LABELS.waiting_for_you,
      sentence: `Waiting for you: ${reasons.join(" and ")}.`,
      action:
        awaitingCount > 0
          ? { label: "Review approvals", href: "/strategies/approvals", kind: "review_approvals" }
          : { label: "Review", href: jobHref, kind: "review" },
    };
  }

  if (RUNNING_JOB_STATUSES.has(jobStatus)) {
    return {
      state: "running",
      label: PLAIN_STATE_LABELS.running,
      sentence: "Running: the attempt is active.",
      action: { label: "View job", href: jobHref, kind: "view_job" },
    };
  }

  return {
    state: "stopped",
    label: PLAIN_STATE_LABELS.stopped,
    sentence:
      jobStatus === "stopped" ? "Stopped: no attempt is running." : "Stopped: this strategy has not run yet.",
    action: { label: "Run now", href: `/strategies/${input.strategyId}`, kind: "run_now" },
  };
}
