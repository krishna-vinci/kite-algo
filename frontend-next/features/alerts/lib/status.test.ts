import { describe, expect, it } from "vitest";

import { deriveFreshness, deriveLifecycle, worstWarningSeverity } from "./status";
import type { AlertsFreshness, AlertsWorkflowSummary } from "@/features/alerts/types";

function freshness(partial: Partial<AlertsFreshness> = {}): AlertsFreshness {
  return {
    last_evaluated_at: null,
    evaluation_age_s: null,
    subscription_count: 0,
    stale_subscriptions: 0,
    stale: null,
    stale_after_seconds: 300,
    ...partial,
  };
}

function workflow(partial: Partial<AlertsWorkflowSummary> = {}): AlertsWorkflowSummary {
  return {
    workflow_id: "wf-1",
    name: "Test alert",
    archived: false,
    archived_at: null,
    created_at: null,
    updated_at: null,
    latest_revision: null,
    active_revision: null,
    kind: "alert",
    instruments: [],
    instrument_summary: null,
    has_universe: false,
    alerts: [],
    channels: [],
    warnings: [],
    subscription_count: 0,
    freshness: freshness(),
    ...partial,
  };
}

describe("deriveFreshness", () => {
  it("treats missing freshness as unknown, not fresh", () => {
    expect(deriveFreshness(null)).toEqual({ state: "unknown", label: "unknown", tone: "neutral" });
  });

  it("renders stale === null as 'not evaluated yet' — NOT as fresh", () => {
    // This is the load-bearing case: the API returns null (not false) when a
    // workflow has never been evaluated, and showing that as healthy is exactly
    // the dead-alert-looks-alive failure the platform is built to avoid.
    const view = deriveFreshness(freshness({ stale: null }));
    expect(view.state).toBe("no_data");
    expect(view.label).toBe("not evaluated yet");
    expect(view.tone).not.toBe("positive");
  });

  it("marks a stale evaluation as danger with the gap", () => {
    const view = deriveFreshness(freshness({ stale: true, evaluation_age_s: 900 }));
    expect(view.state).toBe("stale");
    expect(view.tone).toBe("danger");
    expect(view.label).toContain("15m");
  });

  it("marks a fresh evaluation as positive", () => {
    const view = deriveFreshness(freshness({ stale: false, evaluation_age_s: 12 }));
    expect(view.state).toBe("fresh");
    expect(view.tone).toBe("positive");
  });
});

describe("deriveLifecycle", () => {
  it("is archived when the workflow is archived, even with an active revision", () => {
    expect(
      deriveLifecycle(
        workflow({
          archived: true,
          active_revision: {
            revision_id: "r1",
            revision: 1,
            status: "active",
            canonical_hash: "h",
            created_at: null,
            activated_at: null,
          },
        }),
      ),
    ).toBe("archived");
  });

  it("is draft when no revision is active", () => {
    expect(deriveLifecycle(workflow({ active_revision: null }))).toBe("draft");
  });

  it("is active when a revision is in force", () => {
    expect(
      deriveLifecycle(
        workflow({
          active_revision: {
            revision_id: "r1",
            revision: 1,
            status: "active",
            canonical_hash: "h",
            created_at: null,
            activated_at: null,
          },
        }),
      ),
    ).toBe("active");
  });

  it("is paused when a revision exists but is not active", () => {
    expect(
      deriveLifecycle(
        workflow({
          active_revision: {
            revision_id: "r1",
            revision: 1,
            status: "paused",
            canonical_hash: "h",
            created_at: null,
            activated_at: null,
          },
        }),
      ),
    ).toBe("paused");
  });
});

describe("worstWarningSeverity", () => {
  it("is null without warnings", () => {
    expect(worstWarningSeverity([])).toBeNull();
  });

  it("prefers an error over a warning", () => {
    expect(
      worstWarningSeverity([
        { where: "w", code: "c", message: "m", severity: "warning" },
        { where: "w", code: "c", message: "m", severity: "error" },
      ]),
    ).toBe("error");
  });

  it("reports a warning when no error is present", () => {
    expect(
      worstWarningSeverity([{ where: "w", code: "c", message: "m", severity: "warning" }]),
    ).toBe("warning");
  });
});
