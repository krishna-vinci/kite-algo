import { describe, expect, it } from "vitest";

import {
  describeStaleReason,
  describeSuppression,
  deliveryStatusLabel,
  readRuntimeAvailability,
} from "./health";
import type { AlertsRuntimeHealth } from "@/features/alerts/types";

describe("describeStaleReason", () => {
  it("is null when data is flowing", () => {
    expect(describeStaleReason(null)).toBeNull();
    expect(describeStaleReason(undefined)).toBeNull();
  });

  it("explains each value differently, because each needs a different action", () => {
    const setup = describeStaleReason("no_accepted_tick");
    const quiet = describeStaleReason("tick_age_exceeded");
    const reset = describeStaleReason("continuity_invalidated");
    expect(setup?.action).not.toBe(quiet?.action);
    expect(reset?.action).toContain("fresh crossing");
  });

  it("treats a candle subscription as informational, not a problem", () => {
    const candle = describeStaleReason("not_an_ltp_subscription");
    expect(candle?.label).toBe("candle clock");
    expect(candle?.action).toContain("Nothing");
  });

  it("passes through an unknown reason rather than inventing meaning", () => {
    const unknown = describeStaleReason("some_future_reason");
    expect(unknown?.label).toBe("some_future_reason");
  });
});

describe("readRuntimeAvailability", () => {
  it("is UNKNOWN when the worker health file cannot be read — not zero", () => {
    const runtime: AlertsRuntimeHealth = {
      available: false,
      reason: "health_file_absent",
      note: "UNKNOWN — this is not a report that they are zero or healthy",
    };
    const view = readRuntimeAvailability(runtime);
    expect(view.kind).toBe("unknown");
    if (view.kind === "unknown") {
      expect(view.reason).toBe("health_file_absent");
      expect(view.note).toContain("UNKNOWN");
    }
  });

  it("is unknown when runtime is missing entirely", () => {
    expect(readRuntimeAvailability(undefined).kind).toBe("unknown");
  });

  it("counts quarantined and failing subscriptions when available", () => {
    const runtime: AlertsRuntimeHealth = {
      available: true,
      quarantined: { sub1: "2026-09-11T12:00:00Z" },
      subscription_failures: { sub1: { failures: 3 }, sub2: { failures: 1 } },
      tasks: { evaluation: { alive: true, restarts: 0, backoff_s: null } },
    };
    const view = readRuntimeAvailability(runtime);
    expect(view.kind).toBe("available");
    if (view.kind === "available") {
      expect(view.quarantined).toBe(1);
      expect(view.failedSubscriptions).toBe(2);
    }
  });
});

describe("describeSuppression", () => {
  it("explains the session cap without leaking an internal code", () => {
    expect(describeSuppression("session_cap")).toMatch(/session/i);
  });

  it("passes an unknown reason through rather than inventing copy", () => {
    expect(describeSuppression("mystery_reason")).toBe("mystery_reason");
  });
});

describe("deliveryStatusLabel", () => {
  it("calls 'delivered' what it is: provider acceptance, not human receipt", () => {
    expect(deliveryStatusLabel("delivered").label).toBe("provider accepted");
  });

  it("marks failures as danger", () => {
    expect(deliveryStatusLabel("failed").tone).toBe("danger");
  });

  it("passes an unknown status through", () => {
    expect(deliveryStatusLabel("queued_forever").label).toBe("queued_forever");
  });
});
