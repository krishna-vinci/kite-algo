import { describe, expect, it } from "vitest";

import { formatAge, formatAgeAgo, formatCount, formatTimestamp, summarizeList } from "./format";

describe("formatAge", () => {
  it("bands seconds, minutes, hours and days", () => {
    expect(formatAge(0)).toBe("0s");
    expect(formatAge(45)).toBe("45s");
    expect(formatAge(90)).toBe("1m");
    expect(formatAge(3 * 3600)).toBe("3h");
    expect(formatAge(2 * 86400)).toBe("2d");
  });

  it("returns null for absent or non-finite values rather than guessing zero", () => {
    expect(formatAge(null)).toBeNull();
    expect(formatAge(undefined)).toBeNull();
    expect(formatAge(Number.NaN)).toBeNull();
  });

  it("clamps negative ages to zero instead of rendering a negative gap", () => {
    expect(formatAge(-10)).toBe("0s");
  });
});

describe("formatAgeAgo", () => {
  it("says 'never' when the age is unknown", () => {
    expect(formatAgeAgo(null)).toBe("never");
  });

  it("appends 'ago' when the age is known", () => {
    expect(formatAgeAgo(90)).toBe("1m ago");
  });
});

describe("formatTimestamp", () => {
  it("returns null for absent or unparseable timestamps", () => {
    expect(formatTimestamp(null)).toBeNull();
    expect(formatTimestamp("")).toBeNull();
    expect(formatTimestamp("not-a-date")).toBeNull();
  });

  it("formats a valid ISO timestamp", () => {
    expect(formatTimestamp("2026-09-11T09:30:00Z")).not.toBeNull();
  });
});

describe("formatCount", () => {
  it("preserves null", () => {
    expect(formatCount(null)).toBeNull();
  });

  it("thousands-separates", () => {
    expect(formatCount(1234)).toBe("1,234");
  });
});

describe("summarizeList", () => {
  it("says 'none' for an empty list", () => {
    expect(summarizeList([])).toBe("none");
  });

  it("lists everything up to the visible bound", () => {
    expect(summarizeList(["A", "B"])).toBe("A, B");
  });

  it("truncates with a remainder count past the bound", () => {
    expect(summarizeList(["A", "B", "C", "D", "E"])).toBe("A, B, C +2 more");
  });
});
