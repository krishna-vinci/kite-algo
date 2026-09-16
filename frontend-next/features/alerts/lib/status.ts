/**
 * Lifecycle and freshness derivation — the two questions the UI must never
 * collapse into one.
 *
 * `lifecycle` answers "is this switched on". `freshness` answers "is fresh
 * data arriving". An alert can be active and receiving nothing, and a single
 * combined status is exactly how a dead alert gets mistaken for a working one
 * (handoff §8). These helpers keep the two apart in one place so no component
 * re-derives them inconsistently.
 */

import type {
  IssueSection,
  ValidationState,
} from "@/features/alerts/hooks/use-definition-validation";
import type { AlertsFreshness, AlertsWorkflowSummary } from "@/features/alerts/types";

export type LifecycleState = "active" | "paused" | "draft" | "archived";

export type StatusTone = "positive" | "warning" | "danger" | "neutral";

export type FreshnessView = {
  /** The band to render. `unknown` is a real state, not a fallback for "fine". */
  state: "fresh" | "stale" | "no_data" | "unknown";
  label: string;
  tone: StatusTone;
};

/**
 * Lifecycle from the workflow summary.
 *
 * A workflow with an active revision but no materialized (non-expired)
 * subscriptions is still "active" here — lifecycle is about the revision in
 * force, and the freshness view carries the "nothing is being evaluated" fact
 * separately.
 */
export function deriveLifecycle(workflow: AlertsWorkflowSummary): LifecycleState {
  if (workflow.archived) return "archived";
  // The server reports the EFFECTIVE state; fall back to the revision status for
  // responses that predate it. Pausing keeps the revision active, so reading the
  // revision status alone showed "active" for a paused workflow.
  const declared = (workflow as { lifecycle_state?: string | null }).lifecycle_state;
  if (declared === "archived" || declared === "draft" || declared === "paused" || declared === "active") {
    return declared;
  }
  const active = workflow.active_revision;
  if (!active) return "draft";
  return active.status === "active" ? "active" : "paused";
}

export const LIFECYCLE_TONE: Record<LifecycleState, StatusTone> = {
  active: "positive",
  paused: "warning",
  draft: "neutral",
  archived: "neutral",
};

/**
 * Freshness from the list `freshness` block.
 *
 * `stale === null` means nothing has ever been evaluated for this workflow: the
 * API explicitly reports null rather than false so this is rendered as
 * "not evaluated yet", never as "fresh".
 */
export function deriveFreshness(freshness: AlertsFreshness | null | undefined): FreshnessView {
  if (!freshness) {
    return { state: "unknown", label: "unknown", tone: "neutral" };
  }
  if (freshness.stale === null || freshness.stale === undefined) {
    return { state: "no_data", label: "not evaluated yet", tone: "neutral" };
  }
  if (freshness.stale) {
    return {
      state: "stale",
      label: `no data for ${formatGap(freshness.evaluation_age_s)}`,
      tone: "danger",
    };
  }
  return {
    state: "fresh",
    label: `data ${formatGap(freshness.evaluation_age_s)} old`,
    tone: "positive",
  };
}

function formatGap(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "unknown";
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/** Highest warning severity present, or null when there are none. */
export function worstWarningSeverity(
  warnings: AlertsWorkflowSummary["warnings"],
): "error" | "warning" | null {
  if (!warnings || warnings.length === 0) return null;
  if (warnings.some((w) => w.severity === "error")) return "error";
  if (warnings.some((w) => w.severity === "warning")) return "warning";
  return null;
}

export const WARNING_TONE: Record<"error" | "warning", StatusTone> = {
  error: "danger",
  warning: "warning",
};

// -- shared presentation of validation state and issue sections ------------
// Moved out of the editor so the rail and the save bar render one vocabulary.

export const VALIDATION_TONE: Record<string, "positive" | "warning" | "danger" | "neutral"> = {
  ready: "positive",
  crossed: "warning",
  checking: "neutral",
  incomplete: "neutral",
  invalid: "danger",
  unavailable: "warning",
  "no-data": "neutral",
};

export const VALIDATION_LABEL: Record<string, string> = {
  ready: "Valid",
  crossed: "Already past the level",
  checking: "Checking…",
  incomplete: "Waiting for the required fields",
  invalid: "Needs attention",
  unavailable: "Validation unavailable",
  "no-data": "No market data yet",
};

export const SECTION_META: Array<{
  section: IssueSection;
  label: string;
  anchor: string;
}> = [
  { section: "instrument", label: "Instrument", anchor: "section-instrument" },
  { section: "rule", label: "Condition", anchor: "section-rule" },
  { section: "evaluation", label: "Evaluation", anchor: "section-evaluation" },
  { section: "frequency", label: "Notification frequency", anchor: "section-frequency" },
  { section: "destinations", label: "Destinations", anchor: "section-destinations" },
  { section: "other", label: "Definition", anchor: "section-name" },
];
