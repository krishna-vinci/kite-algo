/**
 * Alert states and coverage summaries.
 *
 * The point of these strings is that they are never invented: an alert with no
 * evaluations is not "watching", a closed market is not "stale data", and a
 * multi-instrument alert never pretends one price speaks for it.
 */

import { describe, expect, it } from "vitest";

import {
  describeAlertState,
  describeCoverage,
  describeLastChecked,
} from "@/features/alerts/lib/alert-state";
import type { MarketQuote } from "@/features/alerts/lib/market-stream";

function quote(freshness: MarketQuote["freshness"], price = 100): MarketQuote {
  return {
    instrument_key: "MCX:GOLD26DECFUT",
    broker_token: 1,
    last_price: price,
    change_absolute: null,
    change_percent: null,
    ohlc: null,
    exchange_timestamp: null,
    received_at: null,
    server_time: null,
    age_ms: 1_000,
    session_state: freshness === "MARKET CLOSED" ? "closed" : "open",
    freshness,
    origin: "tick",
  };
}

describe("describeAlertState", () => {
  it("reports lifecycle before market conditions", () => {
    expect(describeAlertState({ lifecycle: "archived", quote: quote("LIVE") }).state).toBe("archived");
    expect(describeAlertState({ lifecycle: "draft" }).state).toBe("draft");
    expect(describeAlertState({ lifecycle: "paused", quote: quote("LIVE") }).state).toBe("paused");
  });

  it("puts an unfixable definition above the market states", () => {
    const view = describeAlertState({
      lifecycle: "active",
      hasErrorWarning: true,
      quote: quote("STALE"),
    });
    expect(view.state).toBe("needs-attention");
    expect(view.tone).toBe("danger");
  });

  it("distinguishes a closed market from stale data", () => {
    expect(describeAlertState({ lifecycle: "active", quote: quote("MARKET CLOSED") }).state).toBe(
      "market-closed",
    );
    expect(describeAlertState({ lifecycle: "active", quote: quote("STALE") }).state).toBe("stale");
  });

  it("says a crossing alert is waiting to reset when the price is already past the level", () => {
    const view = describeAlertState({
      lifecycle: "active",
      quote: quote("LIVE"),
      alreadyBeyond: true,
      lastEvaluatedAt: "2026-09-15T10:00:00+00:00",
    });
    expect(view.state).toBe("waiting-reset");
    expect(view.hint).toContain("already past the level");
  });

  it("does not claim an alert is watching before it has been evaluated", () => {
    const view = describeAlertState({ lifecycle: "active", quote: quote("LIVE"), lastEvaluatedAt: null });
    expect(view.state).toBe("waiting-first-crossing");
    expect(view.hint).toContain("silent");
  });

  it("says watching only with a live feed and a real evaluation", () => {
    expect(
      describeAlertState({
        lifecycle: "active",
        quote: quote("LIVE"),
        lastEvaluatedAt: "2026-09-15T10:00:00+00:00",
      }).state,
    ).toBe("watching");
    expect(
      describeAlertState({
        lifecycle: "active",
        quote: quote("DELAYED"),
        lastEvaluatedAt: "2026-09-15T10:00:00+00:00",
      }).state,
    ).toBe("watching");
  });
});

describe("describeLastChecked", () => {
  it("prefers the server's own age", () => {
    expect(describeLastChecked(1)).toBe("checked 1s ago");
    expect(describeLastChecked(120)).toBe("checked 2m ago");
    expect(describeLastChecked(7200)).toBe("checked 2h ago");
  });

  it("falls back to an absolute time rather than inventing an age", () => {
    const text = describeLastChecked(null, "2026-09-15T10:00:00+00:00");
    expect(text.startsWith("checked at ")).toBe(true);
  });

  it("says nothing has been evaluated when there is nothing", () => {
    expect(describeLastChecked(null, null)).toBe("not evaluated yet");
    expect(describeLastChecked(undefined, undefined)).toBe("not evaluated yet");
  });
});

describe("describeCoverage", () => {
  it("never presents one price for a universe", () => {
    const text = describeCoverage(["A:1", "A:2"], true, []);
    expect(text).toContain("universe");
    expect(text).not.toMatch(/₹|\d+\.\d\d$/);
  });

  it("counts what is streaming for a multi-instrument alert", () => {
    expect(describeCoverage(["A:1", "A:2"], false, [quote("LIVE"), undefined])).toBe(
      "2 instruments · 1 streaming (1 live)",
    );
    expect(describeCoverage(["A:1"], false, [undefined])).toBe("1 instruments · no price data yet");
  });
});
