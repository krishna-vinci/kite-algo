import { newIdempotencyKey as sharedIdempotencyKey } from "@/lib/ids";

/**
 * Presentation helpers for the hosted-strategy operator surface.
 *
 * These encode the product semantics verbatim so the UI cannot quietly soften
 * them: a Run-now success means *queued*, an unknown cleanup is *not* confirmed
 * stopped, and provider acceptance is not proof of receipt.
 */

import { ApiClientError } from "@/lib/api/client";

export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  starting: "Starting",
  running: "Running",
  fencing: "Stopping (fencing)",
  recovery_required: "Recovery required",
  stopped: "Stopped",
  failed: "Failed",
  hung: "Hung",
};

export function jobStatusLabel(status: string | null | undefined): string {
  const key = String(status ?? "").trim();
  return JOB_STATUS_LABELS[key] ?? (key ? key : "Unknown");
}

/**
 * Audit outcomes for an attempt's block. ``continuation`` is the server-side
 * automatic handover of a finished FINITE evaluation that deliberately left a
 * held book: it is NOT a manual reconciliation and it is NOT a flatness claim.
 */
export const BLOCK_OUTCOME_LABELS: Record<string, string> = {
  reconciled: "Reconciled by an operator",
  blocked: "Blocked",
  continuation: "Continued automatically (book held, not flat)",
};

export function blockOutcomeLabel(outcome: string | null | undefined): string {
  const key = String(outcome ?? "").trim();
  return BLOCK_OUTCOME_LABELS[key] ?? (key ? key : "Unknown");
}

export const STOP_STATE_LABELS: Record<string, string> = {
  none: "No stop requested",
  requested: "Stop requested (queued — not yet launched)",
  stopping: "Stopping",
  confirmed: "Stopped and process cleanup confirmed",
  cleanup_unresolved: "Cleanup unresolved",
};

export function stopStateLabel(state: string | null | undefined, launched = true): string {
  const key = String(state ?? "").trim();
  // A never-launched attempt has no child process, so "process cleanup
  // confirmed" would overstate what actually happened.
  if (key === "confirmed" && !launched) return "Stopped before launch";
  return STOP_STATE_LABELS[key] ?? (key ? key : "Unknown");
}

export function runNowMessage(idempotent: boolean): string {
  return idempotent
    ? "Queued — this retry replayed the original launch (no new job)."
    : "Queued. The process has not started yet.";
}

export type ErrorLike = { detail?: unknown; body?: unknown; message?: string };

/**
 * Operator-facing copy for the machine-readable codes the hosted API returns.
 * The raw code is always kept alongside the copy so support and docs still line
 * up with the server.
 */
const ERROR_COPY: Record<string, string> = {
  STRATEGY_BLOCKED:
    "This strategy already has an active or unreconciled attempt. A healthy finite run clears itself automatically; anything else must be stopped and reconciled before another attempt.",
  STRATEGY_DISABLED: "This strategy is disabled. Enable it before running an attempt.",
  REPLACEMENT_CONFLICT: "This launch request conflicts with an existing job for this strategy.",
  IDEMPOTENCY_CONFLICT:
    "This idempotency key was already used with different launch inputs. Use “New key” for a new launch.",
  STOP_RACE_LOST: "The attempt changed state before the stop was applied. Reload and try again.",
  STALE_ATTEMPT: "This stop targeted a superseded attempt. Reload and stop the current attempt.",
  STALE_LEASE_EPOCH: "The attempt's lease moved on. Reload before stopping.",
  EVIDENCE_CHANGED:
    "The settlement evidence changed between inspection and commit, so nothing was unblocked. Re-inspect.",
  RECONCILE_RACE_LOST: "Another reconciliation won the race; nothing further was unblocked.",
  EXECUTION_QUIESCENCE_UNVERIFIED:
    "Execution quiescence is unverified, so trading-capable reconciliation stays blocked.",
  HOSTED_JOB_ACTIVE: "The attempt is still active; stop it before reconciling.",
  HOSTED_ATTEMPT_FENCED: "The attempt is already fenced; replacement stays blocked until reconciliation.",
  // Phase 2: governed authorization and dispatch refusals.
  GRANT_REQUIRED:
    "Automatic trading needs an active authorization for this exact version, account and environment. Issue one, or switch to review-first.",
  GRANT_REVOKED: "This authorization was revoked, so automatic trading stopped. Issue a new one to resume.",
  GRANT_SUPERSEDED: "A newer authorization replaced this one. The refusal names the grant that replaced it.",
  GRANT_EXPIRED: "This authorization expired. Issue a new one to resume automatic trading.",
  GRANT_VERSION_MISMATCH: "The authorization is bound to a different version of this strategy.",
  GRANT_SOURCE_CHANGED: "The strategy's source changed after the authorization was issued.",
  GRANT_POLICY_CHANGED:
    "The recorded limits or run protection changed, so the authorization no longer applies. Review the limits and issue a new authorization.",
  GRANT_ACCOUNT_MISMATCH: "The authorization was issued for a different account.",
  AUTHORIZATION_MODE_NOT_AUTONOMOUS:
    "This strategy is set to review trades first, so an automatic request cannot dispatch.",
  POLICY_CHANGED: "The admission limits changed after the request was recorded.",
  ADMISSION_REFUSED: "The plan was refused by the strategy's own admission limits.",
  PLAN_ALREADY_EXECUTED: "This plan was already executed once; it will not be dispatched again.",
  OWNER_REJECTED: "You rejected this request.",
  DISPATCH_OUTCOME_UNKNOWN:
    "The platform could not prove whether the broker received this request. It stays unresolved and is not retried automatically.",
  ORDER_REJECTED: "The broker rejected the order.",
  NO_OP: "No order was needed for this plan.",
  TRANSPORT_UNCERTAIN:
    "The connection to the broker failed mid-request. The outcome is unknown and is not retried automatically.",
  EXECUTION_OUTCOME_UNKNOWN: "The execution outcome is unknown; nothing further was sent.",
  HOSTED_ATTEMPT_STOPPED: "The attempt was stopped before this request could dispatch.",
  HOSTED_ATTEMPT_REPLACED: "A newer attempt superseded the one that made this request.",
  HOSTED_ATTEMPT_UNKNOWN: "The attempt that made this request is no longer known to the platform.",
  HOSTED_LEASE_EXPIRED: "The attempt's lease expired before this request could dispatch.",
  HOSTED_RAW_MUTATION_FORBIDDEN:
    "Hosted strategies place orders through a proposal and an execution request, not by sending raw orders.",
  HOSTED_OWNER_POLICY_MUTATION_FORBIDDEN:
    "Limits belong to you: the strategy cannot change the allocation or risk policy.",
  TARGET_KIND_UNKNOWN: "The proposal used a plan type this deployment does not support.",
  INSTRUMENT_UNRESOLVED:
    "The instrument in the proposal could not be matched to the current instrument catalogue.",
  PAYLOAD_INVALID: "The proposal payload was missing or malformed.",
  AUTHORITY_MISMATCH:
    "The proposal's strategy or account did not match this run's own binding, so it was refused.",
  // B2.6a: owner-facing option-run refusals.
  OPTION_STRUCTURE_ALREADY_OPEN:
    "This strategy already owns an open option structure; a new entry is refused until it resolves.",
  OPTION_STRUCTURE_UNRESOLVED:
    "This strategy's option run is not provably finished, so a new entry is refused until it resolves.",
  OPTION_ADJUSTMENT_STALE_BASIS:
    "This adjustment was computed against a basis that is no longer current. Re-check the run and retry.",
  OPTION_ADJUSTMENT_WOULD_UNHEDGE:
    "This adjustment would leave a short leg without its hedge, so it was refused.",
  OPTION_ADJUSTMENT_PROTECTION_ACTIVE:
    "An active protective order owns this run's risk right now, so the adjustment was refused.",
  OPTION_RUN_ADJUST_IN_FLIGHT: "An adjustment is still in flight for this run, so this request was refused.",
  OPTION_PROTECTIVE_EXIT_UNRESOLVED: "A protective exit for this run has not resolved yet.",
  OPTION_RUN_REPAIR_AMBIGUOUS:
    "The platform cannot explain this run's own holdings well enough to repair it automatically; it needs manual review.",
  OPTION_RUN_REPAIR_EVIDENCE_CHANGED:
    "This run's evidence changed since this repair was assessed. Re-check before repairing.",
  OPTION_RUN_REPAIR_LIVE_UNSUPPORTED: "A live residual close has no governed submission path yet.",
  OPTION_RUN_NOT_REPAIRABLE: "This run's status is not one the repair path covers.",
  OPTION_RUN_REPAIR_ACTION_MISMATCH: "That repair action is not supported for this run.",
  OPTION_RUN_REPAIR_STATE_CHANGED:
    "This run's state changed before the repair could commit. Re-check before repairing.",
  OPTION_RUN_REPAIR_AUDIT_UNAVAILABLE: "This run has no hosted job to record the repair against.",
  // B2.6b: owner-facing safe actions (cancel pending work, exit, flatten,
  // dead-submission disposition).
  CANCEL_EVIDENCE_CHANGED:
    "This pending-work preview changed since it was read. Refresh the preview before cancelling.",
  CANCEL_ORDER_NOT_OWNED:
    "This order could not be proven to belong to this strategy, account and environment, so it was refused.",
  CANCEL_PROTECTIVE_ORDER_FORBIDDEN:
    "This is protective (hedge) work. This action never cancels protective orders.",
  CANCEL_REDUCTION_FORBIDDEN:
    "This is exit, reduction, adjust, roll or square-off work. This action never cancels risk-reducing work.",
  OPTION_RUN_STATE_CHANGED: "This run's state changed before the request could commit. Re-check before retrying.",
  OPTION_RUN_EVIDENCE_AMBIGUOUS:
    "The platform cannot read this run's own fills well enough to act automatically; it needs manual review.",
  DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE:
    "The platform has no readable evidence for this step yet, so no disposition can be recorded.",
  DEAD_SUBMISSION_EVIDENCE_CHANGED:
    "This step's evidence changed since it was read. Re-check before resolving it.",
  DEAD_SUBMISSION_OPEN_REMAINDER: "This step still has an open remainder, so it is not a dead submission yet.",
  DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN:
    "This is staged protective exit work; it resolves through the protective exit, not this disposition.",
  FLATTEN_EVALUATION_ACTIVE: "An active evaluation could not be proven stopped, so flatten was refused.",
  FLATTEN_LIVE_NONOPTION_UNSUPPORTED:
    "Live flatten for non-option positions is not supported yet; option work already completed is still reported as done.",
};

function withCopy(code: string): string {
  const copy = ERROR_COPY[code];
  return copy ? `${copy} (${code})` : code;
}

/**
 * Operator copy for one machine-readable code. The raw code is kept alongside
 * the sentence so support and the server agree on what was refused.
 */
export function withRefusalCopy(code: string | null | undefined): string {
  const key = String(code ?? "").trim();
  return key ? withCopy(key) : "";
}

/**
 * Extract a human-readable error from an ApiClientError (or anything), pulling
 * the structured `rejection_reason`/`message` the backend returns when present.
 */
export function hostedErrorMessage(error: unknown): string {
  if (error instanceof ApiClientError) {
    const body = error.body as unknown;
    if (body && typeof body === "object") {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string") return withCopy(detail);
      if (detail && typeof detail === "object") {
        const record = detail as Record<string, unknown>;
        const reason = record.rejection_reason;
        const message = record.message;
        if (typeof reason === "string") {
          const blocking = Array.isArray(record.blocking_reasons)
            ? (record.blocking_reasons as unknown[]).filter((item): item is string => typeof item === "string")
            : [];
          const base = blocking.length > 0 && !ERROR_COPY[reason]
            ? `${reason} (${blocking.join(", ")})`
            : withCopy(reason);
          return typeof message === "string" && message ? `${base} — ${message}` : base;
        }
        if (typeof message === "string") return message;
      }
    }
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return "Request failed";
}

/**
 * The machine-readable refusal code from a 409 `ApiClientError`, or null.
 * Used where the UI must react to ONE specific named refusal (e.g. prompting
 * a preview refresh on `CANCEL_EVIDENCE_CHANGED`) rather than just showing
 * `hostedErrorMessage`'s prose.
 */
export function hostedRefusalCode(error: unknown): string | null {
  if (!(error instanceof ApiClientError)) return null;
  const body = error.body as unknown;
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const reason = (detail as Record<string, unknown>).rejection_reason;
    if (typeof reason === "string") return reason;
  }
  return null;
}

/**
 * A stable idempotency key for one launch request (retries reuse it).
 *
 * Delegates to the shared helper: `crypto.randomUUID()` is secure-context only,
 * so the previous `Math.random()` fallback was both unreachable in the browsers
 * that need a fallback and unsuitable for an identity.
 */
export function newIdempotencyKey(prefix = "launch"): string {
  return sharedIdempotencyKey(prefix);
}

/**
 * A stored timestamp for a table cell.
 *
 * An ISO string is one unbreakable token ~30 characters long, which both reads
 * badly and widens a narrow layout; this is the same short local form the rest
 * of the app uses for stored times (`en-IN`, 24-hour, no seconds).
 */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Tailwind classes for a job status badge. */
export function jobStatusTone(status: string | null | undefined): string {
  switch (status) {
    case "running":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400";
    case "queued":
    case "starting":
      return "border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400";
    case "fencing":
      return "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400";
    case "recovery_required":
    case "hung":
      return "border-orange-500/40 bg-orange-500/10 text-orange-600 dark:text-orange-400";
    case "failed":
      return "border-destructive/40 bg-destructive/10 text-destructive";
    default:
      return "border-border bg-muted/40 text-muted-foreground";
  }
}

// ---------------------------------------------------------------------------
// Governed execution requests (Phase 2 contract)
// ---------------------------------------------------------------------------

/**
 * A request status is about the REQUEST, never about the trade. "Queued" means
 * the platform has not started; "dispatched" means the platform sent work, not
 * that the broker accepted it or that anything filled.
 */
export const EXECUTION_REQUEST_STATUS_LABELS: Record<string, string> = {
  requested: "Recorded",
  awaiting_approval: "Waiting for your decision",
  queued: "Queued (not started)",
  dispatching: "Dispatching now",
  executed: "Dispatched — check the outcome",
  refused: "Refused",
  rejected: "Rejected by you",
  dispatch_unresolved: "Outcome unresolved",
};

export function executionRequestStatusLabel(status: string | null | undefined): string {
  const key = String(status ?? "").trim();
  return EXECUTION_REQUEST_STATUS_LABELS[key] ?? (key ? key : "Unknown");
}

export function executionRequestStatusTone(status: string | null | undefined): string {
  switch (String(status ?? "")) {
    case "executed":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400";
    case "queued":
    case "requested":
      return "border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400";
    case "awaiting_approval":
    case "dispatching":
      return "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400";
    case "dispatch_unresolved":
      return "border-orange-500/40 bg-orange-500/10 text-orange-600 dark:text-orange-400";
    case "refused":
    case "rejected":
      return "border-destructive/40 bg-destructive/10 text-destructive";
    default:
      return "border-border bg-muted/40 text-muted-foreground";
  }
}

/**
 * The executor's own word about what the broker did. Preserved verbatim from
 * the server, because "dispatched" is not "filled".
 */
export const OUTCOME_STATE_LABELS: Record<string, string> = {
  submitted: "Sent to the broker — acceptance not yet confirmed",
  accepted: "Accepted by the broker — fill not yet confirmed",
  filled: "Filled",
  partial: "Partially filled",
  rejected: "Rejected by the broker",
  no_op: "No order needed",
  uncertain: "Transport result unknown",
  failed: "Failed",
};

export function outcomeStateLabel(outcome: string | null | undefined): string {
  const key = String(outcome ?? "").trim();
  return OUTCOME_STATE_LABELS[key] ?? (key ? key : "");
}

/** Occurrences the scheduler itself wrote: missed and expired are not silence. */
export const OCCURRENCE_STATUS_LABELS: Record<string, string> = {
  pending: "Waiting to run",
  fired: "Ran",
  skipped: "Skipped",
  expired: "Missed",
};

export function occurrenceStatusLabel(status: string | null | undefined): string {
  const key = String(status ?? "").trim();
  return OCCURRENCE_STATUS_LABELS[key] ?? (key ? key : "Unknown");
}

export const SCHEDULE_KIND_LABELS: Record<string, string> = {
  daily: "Every day",
  weekly: "Every week",
  monthly: "Every month",
  calendar: "On chosen dates",
};

export function scheduleKindLabel(kind: string | null | undefined): string {
  const key = String(kind ?? "").trim();
  return SCHEDULE_KIND_LABELS[key] ?? (key ? key : "Unknown");
}

const WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function weekdayLabel(weekday: number | null | undefined): string | null {
  if (weekday === null || weekday === undefined) return null;
  return WEEKDAY_LABELS[weekday] ?? String(weekday);
}

/** Plain wording for one schedule's timing, without exposing cron language. */
export function scheduleCadenceLabel(schedule: {
  schedule_kind: string;
  at_time: string;
  weekday?: number | null;
  day_of_month?: number | null;
  calendar_dates?: string[];
  timezone: string;
}): string {
  const time = `${schedule.at_time} ${schedule.timezone}`;
  switch (String(schedule.schedule_kind)) {
    case "weekly": {
      const day = weekdayLabel(schedule.weekday) ?? "a weekday";
      return `Every ${day} at ${time}`;
    }
    case "monthly":
      return `Day ${schedule.day_of_month ?? 1} of every month at ${time}`;
    case "calendar": {
      const dates = schedule.calendar_dates ?? [];
      return dates.length > 0 ? `On ${dates.join(", ")} at ${time}` : `On chosen dates at ${time}`;
    }
    default:
      return `Every day at ${time}`;
  }
}

/**
 * What the platform does when an occurrence is due while the previous one is
 * still unresolved, and how late a missed run may still fire. Reported from the
 * server's own values.
 */
// ---------------------------------------------------------------------------
// B2.6a: owner-facing option-run operations
// ---------------------------------------------------------------------------

export const OPTION_RUN_STATUS_LABELS: Record<string, string> = {
  created: "Created",
  entry_previewed: "Entry previewed",
  entering: "Entering",
  entered: "Entered",
  partial_entry: "Partial entry",
  cleanup_required: "Cleanup required",
  adjusting: "Adjusting",
  exit_previewed: "Exit previewed",
  exiting: "Exiting",
  partial_exit: "Partial exit",
  exited: "Exited",
  settled: "Settled",
  unknown: "Unknown",
};

export function optionRunStatusLabel(status: string | null | undefined): string {
  const key = String(status ?? "").trim();
  return OPTION_RUN_STATUS_LABELS[key] ?? (key ? key : "Unknown");
}

/** Tailwind classes for an option-run status badge. Text always carries the label too. */
export function optionRunStatusTone(status: string | null | undefined): string {
  switch (String(status ?? "")) {
    case "entered":
    case "exited":
    case "settled":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400";
    case "created":
    case "entry_previewed":
    case "entering":
    case "exit_previewed":
    case "exiting":
      return "border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400";
    case "adjusting":
      return "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400";
    case "partial_entry":
    case "partial_exit":
      return "border-orange-500/40 bg-orange-500/10 text-orange-600 dark:text-orange-400";
    case "cleanup_required":
      return "border-destructive/40 bg-destructive/10 text-destructive";
    case "unknown":
    default:
      return "border-border bg-muted/40 text-muted-foreground";
  }
}

export const OPTION_LEG_STATE_LABELS: Record<string, string> = {
  open: "Open",
  pending: "Pending",
  failed: "Failed",
  flat: "Flat",
};

export function optionLegStateLabel(state: string | null | undefined): string {
  const key = String(state ?? "").trim();
  return OPTION_LEG_STATE_LABELS[key] ?? (key ? key : "Unknown");
}

export function optionLegStateTone(state: string | null | undefined): string {
  switch (String(state ?? "")) {
    case "open":
      return "text-emerald-600 dark:text-emerald-400";
    case "pending":
      return "text-amber-600 dark:text-amber-400";
    case "failed":
      return "text-destructive";
    case "flat":
      return "text-muted-foreground";
    default:
      return "text-muted-foreground";
  }
}

// ---------------------------------------------------------------------------
// B2.6b: owner-facing safe actions (cancel pending work, dead-submission)
// ---------------------------------------------------------------------------

/** The ONLY outcomes the dead-submission disposition ever accepts. */
export const DEAD_SUBMISSION_DISPOSITION_LABELS: Record<string, string> = {
  filled: "Filled — proven fills cover the full requested quantity",
  rejected: "Rejected — broker/paper terminal rejection, zero filled",
  cancelled: "Cancelled — terminal cancellation, any proven fill preserved",
  failed_never_submitted: "Never submitted — durable records prove it never reached the order path",
  failed_residual_abandoned: "Residual abandoned — terminal order, zero remaining, fills recorded",
};

export function deadSubmissionDispositionLabel(value: string | null | undefined): string {
  const key = String(value ?? "").trim();
  return DEAD_SUBMISSION_DISPOSITION_LABELS[key] ?? (key ? key : "Unknown");
}

export function schedulePolicyCopy(schedule: {
  misfire_grace_seconds: number;
  overlap_policy: string;
}): string {
  const minutes = Math.max(0, Math.round(schedule.misfire_grace_seconds / 60));
  const grace =
    minutes >= 60
      ? `${Math.round(minutes / 60)} hour${Math.round(minutes / 60) === 1 ? "" : "s"}`
      : `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const overlap =
    schedule.overlap_policy === "defer_until_resolved"
      ? "a new run waits while the previous one is still unresolved"
      : `overlap policy: ${schedule.overlap_policy}`;
  return `A missed run still starts if it is within ${grace}; ${overlap}.`;
}
