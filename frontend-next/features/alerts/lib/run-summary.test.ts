/**
 * A run that evaluated nothing because the data was missing must not read like
 * "no instruments matched" — that is the distinction this pins.
 */

import { describe, expect, it } from "vitest";

import { readScreenerRun } from "@/features/alerts/lib/run-summary";

describe("readScreenerRun", () => {
  it("says a completed scan ranked its qualifiers", () => {
    const reading = readScreenerRun({
      status: "complete",
      coverage: { expected: 5, evaluated: 5, qualifying: 5, unavailable: 0 },
    });
    expect(reading.ranked).toBe(true);
    expect(reading.dataLimited).toBe(false);
    expect(reading.summary).toContain("5 qualified");
  });

  it("distinguishes zero matches WITH data from zero evaluated WITHOUT data", () => {
    const noMatches = readScreenerRun({
      status: "complete",
      coverage: { expected: 4, evaluated: 4, qualifying: 0, unavailable: 0 },
    });
    expect(noMatches.summary).toContain("none met the qualification");
    expect(noMatches.dataLimited).toBe(false);

    const noData = readScreenerRun({
      status: "failed",
      coverage: {
        expected: 5,
        evaluated: 0,
        qualifying: 0,
        unavailable: 5,
        candle_warming: { status: "unavailable", skipped: 0, unavailable: 5, members: [] },
      },
    });
    expect(noData.dataLimited).toBe(true);
    expect(noData.summary).toContain("missing data, not a market result");
    expect(noData.summary).toContain("without daily candles");
  });

  it("reports symbols still warming as such, not as unavailable", () => {
    const reading = readScreenerRun({
      status: "failed",
      coverage: {
        expected: 30,
        evaluated: 0,
        unavailable: 20,
        candle_warming: { status: "skipped", skipped: 10, unavailable: 0, members: [] },
      },
    });
    expect(reading.dataLimited).toBe(true);
    expect(reading.summary).toContain("10 symbol(s) still warming");
  });

  it("explains a partial run without pretending it is complete", () => {
    const reading = readScreenerRun({
      status: "partial",
      coverage: { expected: 10, evaluated: 7, qualifying: 3, unavailable: 3 },
    });
    expect(reading.summary).toContain("Scanned 7 of 10");
    expect(reading.summary).toContain("not a");
    expect(reading.dataLimited).toBe(true);
  });

  it("never claims a ranked result while the scan is running", () => {
    const reading = readScreenerRun({ status: "running", coverage: {} });
    expect(reading.ranked).toBe(false);
    expect(reading.summary).toContain("Scanning");
  });

  it("surfaces the server failure reason for a real failure", () => {
    const reading = readScreenerRun({
      status: "failed",
      coverage: { expected: 5, evaluated: 0, unavailable: 0 },
      failure_reason: "universe_resolution_failed",
    });
    expect(reading.dataLimited).toBe(false);
    expect(reading.summary).toContain("universe_resolution_failed");
  });
});
