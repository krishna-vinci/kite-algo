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
