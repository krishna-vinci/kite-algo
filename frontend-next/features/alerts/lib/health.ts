/**
 * Health vocabulary for the workflow detail page.
 *
 * `stale_reason` is a language, not a flag: each value implies a different
 * operator action (handoff §8). Collapsing it into "stale" would erase the
 * difference between a setup mistake, a quiet market, and a deliberate reset.
 */

import type { AlertsRuntimeHealth } from "@/features/alerts/types";

export type StaleReasonView = {
  label: string;
  detail: string;
  /** What the operator should actually do about it. */
  action: string;
};

const STALE_REASONS: Record<string, StaleReasonView> = {
  no_accepted_tick: {
    label: "never received data",
    detail: "Nothing has ever been evaluated for this subscription.",
    action: "Check that it is activated and wired to an instrument that trades.",
  },
  tick_age_exceeded: {
    label: "feed has gone quiet",
    detail: "It was evaluating, and the feed has since stopped delivering.",
    action: "This is a data problem, not a rule problem. Expect no signals until data resumes.",
  },
  continuity_invalidated: {
    label: "crossing state was reset",
    detail:
      "A silence was detected and the crossing state was deliberately invalidated so a recovery tick cannot fabricate a crossing.",
    action: "Not a bug. The alert needs a fresh crossing before it can fire again.",
  },
  not_an_ltp_subscription: {
    label: "candle clock",
    detail: "This subscription evaluates on completed candles, which have no tick age.",
    action: "Nothing — candle freshness is a different mechanism and no tick age is reported.",
  },
};

export function describeStaleReason(reason: string | null | undefined): StaleReasonView | null {
  if (!reason) return null;
  return (
    STALE_REASONS[reason] ?? {
      label: reason,
      detail: "The backend reported a stale reason this UI does not have copy for.",
      action: "Read the value as-is rather than assuming it means something else.",
    }
  );
}

/**
 * Runtime facts (quarantine, failure counts, task liveness) live in the
 * evaluation worker's memory, a different container from the API. When the
 * health file is unreadable they are UNKNOWN — and "0 quarantined" would tell
 * the operator the opposite of the truth, so presence is checked explicitly.
 */
export type RuntimeAvailability =
  | { kind: "available"; quarantined: number; failedSubscriptions: number; tasks: Record<string, unknown> }
  | { kind: "unknown"; reason: string; note: string };

export function readRuntimeAvailability(runtime: AlertsRuntimeHealth | undefined): RuntimeAvailability {
  if (!runtime || runtime.available !== true) {
    return {
      kind: "unknown",
      reason: runtime?.reason ?? "not_reported",
      note:
        (typeof runtime?.note === "string" && runtime.note) ||
        "The API could not read the evaluation worker's health, so quarantine and failure state are UNKNOWN — not zero.",
    };
  }
  const quarantined = runtime.quarantined ? Object.keys(runtime.quarantined).length : 0;
  const failedSubscriptions = runtime.subscription_failures
    ? Object.keys(runtime.subscription_failures).length
    : 0;
  return {
    kind: "available",
    quarantined,
    failedSubscriptions,
    tasks: (runtime.tasks as Record<string, unknown>) ?? {},
  };
}

/** Delivery status copy. Provider acceptance is NOT human receipt. */
export function deliveryStatusLabel(status: string): { label: string; tone: "positive" | "warning" | "danger" | "neutral" } {
  switch (status) {
    case "delivered":
      return { label: "provider accepted", tone: "positive" };
    case "pending":
      return { label: "pending", tone: "neutral" };
    case "failed":
      return { label: "failed", tone: "danger" };
    case "dropped":
      return { label: "dropped", tone: "danger" };
    default:
      return { label: status, tone: "neutral" };
  }
}
