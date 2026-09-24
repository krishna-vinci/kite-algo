import { ApiClientError } from "@/lib/api/client";
import { describe, expect, it } from "vitest";

import {
  formatTimestamp,
  hostedErrorMessage,
  jobStatusLabel,
  jobStatusTone,
  newIdempotencyKey,
  runNowMessage,
  stopStateLabel,
} from "./format";

describe("hosted strategy format helpers", () => {
  it("renders a stored time as a short local value, never a raw ISO token", () => {
    const rendered = formatTimestamp("2026-09-23T13:05:00+00:00");
    expect(rendered).toMatch(/23 Sep/);
    expect(rendered).not.toContain("T");
    expect(formatTimestamp(null)).toBe("—");
    expect(formatTimestamp("not a time")).toBe("—");
  });

  it("labels unknown statuses without inventing a state", () => {
    expect(jobStatusLabel("running")).toBe("Running");
    expect(jobStatusLabel("recovery_required")).toBe("Recovery required");
    expect(jobStatusLabel("made_up")).toBe("made_up");
    expect(jobStatusLabel(null)).toBe("Unknown");
  });

  it("keeps the cleanup-unresolved stop state distinct from confirmed", () => {
    expect(stopStateLabel("cleanup_unresolved")).toBe("Cleanup unresolved");
    expect(stopStateLabel("confirmed")).toBe("Stopped and process cleanup confirmed");
    // A never-launched attempt must not claim process cleanup.
    expect(stopStateLabel("confirmed", false)).toBe("Stopped before launch");
  });

  it("frames a successful run-now response as queued, and a replay as a replay", () => {
    expect(runNowMessage(false)).toMatch(/Queued/);
    expect(runNowMessage(false)).toMatch(/not started/);
    expect(runNowMessage(true)).toMatch(/replayed/);
  });

  it("renders failure/warning tones distinctly", () => {
    expect(jobStatusTone("running")).not.toBe(jobStatusTone("failed"));
    expect(jobStatusTone("recovery_required")).not.toBe(jobStatusTone("queued"));
  });

  it("extracts structured rejection reasons", () => {
    const conflict = new ApiClientError(409, {
      detail: { rejection_reason: "IDEMPOTENCY_CONFLICT", message: "different request" },
    });
    expect(hostedErrorMessage(conflict)).toContain("different request");
    expect(hostedErrorMessage(conflict)).toContain("IDEMPOTENCY_CONFLICT");
    const blocked = new ApiClientError(409, {
      detail: { rejection_reason: "EXECUTION_QUIESCENCE_UNVERIFIED", blocking_reasons: ["STALE_LEDGER"] },
    });
    expect(hostedErrorMessage(blocked)).toContain("EXECUTION_QUIESCENCE_UNVERIFIED");
    // Known codes get operator copy; the raw code stays alongside it.
    const strategyBlocked = new ApiClientError(409, { detail: "STRATEGY_BLOCKED" });
    expect(hostedErrorMessage(strategyBlocked)).toContain("active or unreconciled attempt");
    expect(hostedErrorMessage(strategyBlocked)).toContain("STRATEGY_BLOCKED");
    // An unknown reason keeps the server's blocking reasons.
    const unknown = new ApiClientError(409, {
      detail: { rejection_reason: "SOMETHING_NEW", blocking_reasons: ["A", "B"] },
    });
    expect(hostedErrorMessage(unknown)).toBe("SOMETHING_NEW (A, B)");
    expect(hostedErrorMessage(new Error("boom"))).toBe("boom");
    expect(hostedErrorMessage("nope")).toBe("Request failed");
  });

  it("generates distinct idempotency keys", () => {
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey());
  });
});
