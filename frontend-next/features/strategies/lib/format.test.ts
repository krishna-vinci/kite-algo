import { ApiClientError } from "@/lib/api/client";
import { describe, expect, it } from "vitest";

import {
  hostedErrorMessage,
  jobStatusLabel,
  jobStatusTone,
  newIdempotencyKey,
  runNowMessage,
  stopStateLabel,
} from "./format";

describe("hosted strategy format helpers", () => {
  it("labels unknown statuses without inventing a state", () => {
    expect(jobStatusLabel("running")).toBe("Running");
    expect(jobStatusLabel("recovery_required")).toBe("Recovery required");
    expect(jobStatusLabel("made_up")).toBe("made_up");
    expect(jobStatusLabel(null)).toBe("Unknown");
  });

  it("keeps the cleanup-unresolved stop state distinct from confirmed", () => {
    expect(stopStateLabel("cleanup_unresolved")).toBe("Cleanup unresolved");
    expect(stopStateLabel("confirmed")).toBe("Stopped and process cleanup confirmed");
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
    expect(hostedErrorMessage(conflict)).toBe("IDEMPOTENCY_CONFLICT: different request");
    const blocked = new ApiClientError(409, {
      detail: { rejection_reason: "EXECUTION_QUIESCENCE_UNVERIFIED", blocking_reasons: ["STALE_LEDGER"] },
    });
    expect(hostedErrorMessage(blocked)).toBe("EXECUTION_QUIESCENCE_UNVERIFIED (STALE_LEDGER)");
    expect(hostedErrorMessage(new Error("boom"))).toBe("boom");
    expect(hostedErrorMessage("nope")).toBe("Request failed");
  });

  it("generates distinct idempotency keys", () => {
    expect(newIdempotencyKey()).not.toBe(newIdempotencyKey());
  });
});
