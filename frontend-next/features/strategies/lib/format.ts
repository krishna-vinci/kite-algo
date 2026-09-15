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
    "This strategy already has an active or unreconciled attempt. Stop it and reconcile before starting another.",
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
  HOSTED_LEASE_EXPIRED: "The supervisor lease expired; the attempt must be fenced or recovered first.",
  HOSTED_ATTEMPT_FENCED: "The attempt is already fenced; replacement stays blocked until reconciliation.",
};

function withCopy(code: string): string {
  const copy = ERROR_COPY[code];
  return copy ? `${copy} (${code})` : code;
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
export function newIdempotencyKey(): string {
  return sharedIdempotencyKey("launch");
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
