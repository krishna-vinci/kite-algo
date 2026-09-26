import { describe, expect, it } from "vitest";

import { componentTone, componentTooltip, liveModeSummary } from "@/lib/platform/status";

describe("componentTone", () => {
  it("maps ok/connected to positive, stale to warning, down/expired to danger, and anything else to neutral", () => {
    expect(componentTone("ok")).toBe("positive");
    expect(componentTone("connected")).toBe("positive");
    expect(componentTone("stale")).toBe("warning");
    expect(componentTone("down")).toBe("danger");
    expect(componentTone("expired")).toBe("danger");
    expect(componentTone("unknown")).toBe("neutral");
    expect(componentTone(null)).toBe("neutral");
  });
});

describe("componentTooltip", () => {
  it("always tells the operator a reconnect is needed for an expired broker", () => {
    expect(componentTooltip("Broker", "expired")).toMatch(/reconnect needed/i);
  });

  it("includes the detail when one is given for a non-expired state", () => {
    expect(componentTooltip("Market data", "stale", "12s old")).toBe("Market data: stale (12s old)");
  });
});

describe("liveModeSummary", () => {
  it("shows PAPER when mode is not live regardless of lanes", () => {
    expect(liveModeSummary({ mode: "paper", live: { enabled: false, lanes_open: ["cnc"] } })).toBe("PAPER");
  });

  it("lists the open lanes when mode is live", () => {
    expect(liveModeSummary({ mode: "live", live: { enabled: true, lanes_open: ["cnc", "options"] } })).toBe(
      "LIVE · cnc, options",
    );
  });
});
