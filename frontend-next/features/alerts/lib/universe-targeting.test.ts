import { describe, expect, it } from "vitest";

import {
  buildDocument,
  documentToDraft,
  emptyDraft,
  emptyUniverseDraft,
  universeDraftIssues,
  universeRefToDocument,
  type AlertDraft,
} from "./authoring";

function draftWith(overrides: Partial<AlertDraft> = {}): AlertDraft {
  return {
    ...emptyDraft(),
    name: "watch-nifty",
    session: "nse_equity",
    conditions: [
      { left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 100 } },
    ],
    alert: {
      id: "a1",
      trigger: "on_transition",
      channels: ["ops"],
      cooldown_s: null,
      rearm_level: null,
      rearm_direction: null,
      reminder_interval_s: null,
      notify_if_already_true: false,
      max_per_session: null,
    },
    ...overrides,
  };
}

describe("universe targeting", () => {
  it("uses the typed reference form, not the shorthand", () => {
    expect(universeRefToDocument({ kind: "index", name: "Nifty50" })).toEqual({
      kind: "index",
      name: "Nifty50",
    });
  });

  it("emits a universe block and an empty instrument list when targeting a universe", () => {
    const document = buildDocument(
      draftWith({
        targeting: "universe",
        instruments: ["NSE:TCS"],
        universe: {
          union: [{ kind: "universe", name: "top100" }],
          exclude: [{ kind: "universe", name: "illiquid" }],
          deduplicate: true,
        },
      }),
    );
    expect(document.universe).toEqual({
      union: [{ kind: "universe", name: "top100" }],
      exclude: [{ kind: "universe", name: "illiquid" }],
      deduplicate: true,
    });
    // The instrument list is dropped, not merged: the document carries one
    // coverage mechanism, so which one wins is never ambiguous.
    expect(document.instruments).toEqual([]);
  });

  it("omits exclude when there is nothing excluded", () => {
    const document = buildDocument(
      draftWith({
        targeting: "universe",
        universe: { union: [{ kind: "index", name: "Nifty50" }], exclude: [], deduplicate: true },
      }),
    );
    expect(document.universe).not.toHaveProperty("exclude");
  });

  it("falls back to the instrument list when a universe ref is blank", () => {
    const document = buildDocument(
      draftWith({
        targeting: "universe",
        instruments: ["NSE:TCS"],
        universe: { union: [{ kind: "universe", name: "   " }], exclude: [], deduplicate: true },
      }),
    );
    expect(document.universe).toBeNull();
    expect(document.instruments).toEqual(["NSE:TCS"]);
  });

  it("leaves an instrument-targeted document byte-identical to before", () => {
    const document = buildDocument(draftWith({ instruments: ["NSE:TCS"] }));
    expect(document.universe).toBeNull();
    expect(document.instruments).toEqual(["NSE:TCS"]);
  });

  it("round-trips a universe-targeted document back into the draft", () => {
    const draft = draftWith({
      targeting: "universe",
      universe: {
        union: [{ kind: "index", name: "Nifty50" }, { kind: "universe", name: "screener-out" }],
        exclude: [{ kind: "watchlist", name: "never" }],
        deduplicate: false,
      },
    });
    const result = documentToDraft(buildDocument(draft));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.draft.targeting).toBe("universe");
      expect(result.draft.universe).toEqual(draft.universe);
    }
  });

  it("reads the shorthand form too, so an SDK-authored document still opens", () => {
    const result = documentToDraft({
      version: 1,
      name: "x",
      session: "nse_equity",
      instruments: [],
      universe: { union: [{ index: "Nifty50" }] },
      stages: [
        {
          id: "px",
          type: "signal",
          clock: "candle_close",
          timeframe: "day",
          conditions: { all: [{ left: { field: "close" }, op: "gt", right: 1 }] },
        },
      ],
      alerts: [{ id: "a1", source: "px", trigger: "on_transition", channels: [] }],
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.draft.targeting).toBe("universe");
      expect(result.draft.universe.union).toEqual([{ kind: "index", name: "Nifty50" }]);
    }
  });

  it("refuses an intersect expression rather than widening it on save", () => {
    const document = buildDocument(
      draftWith({
        targeting: "universe",
        universe: { union: [{ kind: "universe", name: "top100" }], exclude: [], deduplicate: true },
      }),
    );
    const universe = { ...(document.universe as Record<string, unknown>) };
    universe.intersect = [{ kind: "universe", name: "liquid" }];
    const result = documentToDraft({ ...document, universe });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/intersect/i);
  });
});

describe("universeDraftIssues", () => {
  it("requires at least one named reference", () => {
    expect(universeDraftIssues(emptyUniverseDraft()).join(" ")).toMatch(/at least one reference/);
  });

  it("rejects a blank reference — an empty name is not a wildcard", () => {
    const issues = universeDraftIssues({
      union: [{ kind: "universe", name: "ok" }, { kind: "universe", name: "  " }],
      exclude: [],
      deduplicate: true,
    });
    expect(issues.join(" ")).toMatch(/needs a name/);
  });

  it("accepts a fully specified expression", () => {
    expect(
      universeDraftIssues({
        union: [{ kind: "index", name: "Nifty50" }],
        exclude: [{ kind: "universe", name: "illiquid" }],
        deduplicate: true,
      }),
    ).toEqual([]);
  });
});
