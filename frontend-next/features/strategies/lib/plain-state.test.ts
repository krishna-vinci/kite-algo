import { describe, expect, it } from "vitest";

import { plainStrategyState, type PlainStateInput, type PlainStateKind } from "./plain-state";

const BASE: PlainStateInput = { strategyId: "s-1" };

type Case = [string, PlainStateInput, PlainStateKind];

const cases: Case[] = [
  ["no job ever run", { ...BASE }, "stopped"],
  ["job queued", { ...BASE, job: { status: "queued", job_id: "j-1" } }, "running"],
  ["job starting", { ...BASE, job: { status: "starting", job_id: "j-1" } }, "running"],
  ["job running", { ...BASE, job: { status: "running", job_id: "j-1" } }, "running"],
  ["job fencing (winding down)", { ...BASE, job: { status: "fencing", job_id: "j-1" } }, "running"],
  ["job stopped cleanly", { ...BASE, job: { status: "stopped", job_id: "j-1" } }, "stopped"],
  ["job failed", { ...BASE, job: { status: "failed", job_id: "j-1" } }, "error"],
  ["job hung", { ...BASE, job: { status: "hung", job_id: "j-1" } }, "needs_attention"],
  [
    "job recovery_required (unreconciled)",
    { ...BASE, job: { status: "recovery_required", job_id: "j-1" } },
    "needs_attention",
  ],
  [
    "request awaiting_approval",
    { ...BASE, job: { status: "running", job_id: "j-1" }, executionRequests: [{ status: "awaiting_approval" }] },
    "waiting_for_you",
  ],
  [
    "request dispatch_unresolved",
    { ...BASE, job: { status: "running", job_id: "j-1" }, executionRequests: [{ status: "dispatch_unresolved" }] },
    "needs_attention",
  ],
  [
    "request queued/dispatching/executed/refused/rejected never override a running job",
    {
      ...BASE,
      job: { status: "running", job_id: "j-1" },
      executionRequests: [
        { status: "queued" },
        { status: "dispatching" },
        { status: "executed" },
        { status: "refused" },
        { status: "rejected" },
      ],
    },
    "running",
  ],
  [
    "option run repairable",
    { ...BASE, job: { status: "stopped", job_id: "j-1" }, optionRuns: [{ status: "partial_entry", repairable: true }] },
    "needs_attention",
  ],
  [
    "option run cleanup_required",
    { ...BASE, job: { status: "stopped", job_id: "j-1" }, optionRuns: [{ status: "cleanup_required" }] },
    "waiting_for_you",
  ],
  [
    "failed job wins over an awaiting approval",
    {
      ...BASE,
      job: { status: "failed", job_id: "j-1" },
      executionRequests: [{ status: "awaiting_approval" }],
    },
    "error",
  ],
  [
    "needs_attention wins over waiting_for_you",
    {
      ...BASE,
      job: { status: "hung", job_id: "j-1" },
      executionRequests: [{ status: "awaiting_approval" }],
    },
    "needs_attention",
  ],
  [
    "waiting_for_you wins over a merely running job",
    {
      ...BASE,
      job: { status: "queued", job_id: "j-1" },
      executionRequests: [{ status: "awaiting_approval" }],
    },
    "waiting_for_you",
  ],
];

describe("plainStrategyState", () => {
  it.each(cases)("%s -> %s", (_name, input, expected) => {
    const result = plainStrategyState(input);
    expect(result.state).toBe(expected);
    expect(result.sentence.length).toBeGreaterThan(0);
    expect(result.action?.label).toBeTruthy();
  });

  it("points the action at the job when one exists", () => {
    const result = plainStrategyState({ ...BASE, job: { status: "running", job_id: "j-42" } });
    expect(result.action?.href).toBe("/strategies/s-1/jobs/j-42");
  });

  it("points a never-run strategy's action at running it", () => {
    const result = plainStrategyState({ ...BASE });
    expect(result.action).toEqual({ label: "Run now", href: "/strategies/s-1", kind: "run_now" });
  });

  it("counts multiple awaiting approvals in the sentence", () => {
    const result = plainStrategyState({
      ...BASE,
      job: { status: "running", job_id: "j-1" },
      executionRequests: [{ status: "awaiting_approval" }, { status: "awaiting_approval" }],
    });
    expect(result.sentence).toBe("Waiting for you: approve 2 live plans.");
    expect(result.action).toEqual({
      label: "Review approvals",
      href: "/strategies/approvals",
      kind: "review_approvals",
    });
  });
});
