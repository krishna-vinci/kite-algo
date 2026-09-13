import { describe, expect, it } from "vitest";

import {
  attachmentSupportsRankBands,
  buildScreenerDocument,
  defaultRankBand,
  documentToScreenerDraft,
  emptyScreenerDraft,
  screenerDraftIssues,
  type ScreenerDraft,
} from "./screener-authoring";

function draftWith(overrides: Partial<ScreenerDraft> = {}): ScreenerDraft {
  return { ...emptyScreenerDraft(), name: "scan", ...overrides };
}

describe("defaultRankBand", () => {
  // Pinned to the parser's own default (backend/workflows/parser.py): for
  // top_n=10 the exit band is 15. If the server rule changes, this test is the
  // tripwire that stops the form showing a band the server will not use.
  it("matches the parser's documented default for top_n=10", () => {
    expect(defaultRankBand(10)).toEqual({ entry_rank: 10, exit_rank: 15 });
  });

  it("always gives an exit band strictly above the entry band", () => {
    for (const topN of [1, 2, 3, 5, 20, 100, 1000]) {
      const band = defaultRankBand(topN);
      expect(band.exit_rank).toBeGreaterThan(band.entry_rank);
    }
  });

  it("clamps a nonsense value instead of emitting an invalid band", () => {
    expect(defaultRankBand(0).entry_rank).toBe(1);
    expect(defaultRankBand(100000).entry_rank).toBe(1000);
  });
});

describe("buildScreenerDocument", () => {
  it("emits the canonical screener keys", () => {
    const document = buildScreenerDocument(draftWith());
    const screener = document.screener as Record<string, unknown>;
    expect(Object.keys(screener).sort()).toEqual(
      ["freshness_limit_s", "rank", "schedule", "top_n"].sort(),
    );
    // Seconds, not a duration string: that is the canonical serialization.
    expect(screener.freshness_limit_s).toBe(3 * 24 * 3600);
    expect(screener.schedule).toEqual({
      every: "1d",
      calendar: "nse_equity",
      at: "session_close",
    });
  });

  it("emits a universe expression and leaves instruments empty", () => {
    const document = buildScreenerDocument(
      draftWith({
        targeting: "universe",
        universe: { union: [{ kind: "index", name: "Nifty50" }], exclude: [], deduplicate: true },
      }),
    );
    expect(document.instruments).toEqual([]);
    expect(document.universe).toEqual({
      union: [{ kind: "index", name: "Nifty50" }],
      deduplicate: true,
    });
  });

  it("omits the universe block entirely for an instrument list", () => {
    const document = buildScreenerDocument(
      draftWith({ targeting: "instruments", instruments: ["NSE:TCS"] }),
    );
    expect(document.universe).toBeNull();
    expect(document.instruments).toEqual(["NSE:TCS"]);
  });

  it("always emits initial_match so a baseline is explicit, not implied", () => {
    const document = buildScreenerDocument(
      draftWith({
        attachments: [
          {
            id: "a",
            trigger: "top_n",
            channels: ["ops"],
            top_n: 10,
            rank_delta: null,
            entry_rank: 10,
            exit_rank: 15,
            exit_after: null,
            initial_match: false,
            message: null,
          },
        ],
      }),
    );
    const attachments = (document.screener as Record<string, unknown>).attachments as Array<
      Record<string, unknown>
    >;
    expect(attachments[0].initial_match).toBe(false);
    expect(attachments[0].exit_rank).toBe(15);
    // Absent keys stay absent: emitting null would change the canonical hash.
    expect("rank_delta" in attachments[0]).toBe(false);
    expect("exit_after" in attachments[0]).toBe(false);
  });

  it("never emits an alerts block — screener documents notify via attachments", () => {
    expect(buildScreenerDocument(draftWith())).not.toHaveProperty("alerts");
  });
});

describe("screenerDraftIssues", () => {
  it("rejects an exit band at or below the entry band (the E-17 oscillation guard)", () => {
    const issues = screenerDraftIssues(
      draftWith({
        attachments: [
          {
            id: "a",
            trigger: "top_n",
            channels: ["ops"],
            top_n: 10,
            rank_delta: null,
            entry_rank: 10,
            exit_rank: 10,
            exit_after: null,
            initial_match: false,
            message: null,
          },
        ],
      }),
    );
    expect(issues.join(" ")).toMatch(/oscillat/i);
  });

  it("flags an attachment with no channel, because it could never notify", () => {
    const issues = screenerDraftIssues(
      draftWith({
        attachments: [
          {
            id: "a",
            trigger: "entry",
            channels: [],
            top_n: null,
            rank_delta: null,
            entry_rank: null,
            exit_rank: null,
            exit_after: null,
            initial_match: false,
            message: null,
          },
        ],
      }),
    );
    expect(issues.join(" ")).toMatch(/channel/i);
  });

  it("requires the trigger-specific threshold", () => {
    const issues = screenerDraftIssues(
      draftWith({
        attachments: [
          {
            id: "mover",
            trigger: "rank_delta",
            channels: ["ops"],
            top_n: null,
            rank_delta: null,
            entry_rank: null,
            exit_rank: null,
            exit_after: null,
            initial_match: false,
            message: null,
          },
        ],
      }),
    );
    expect(issues.join(" ")).toMatch(/rank_delta/);
  });

  it("rejects duplicate attachment ids", () => {
    const attachment = {
      id: "same",
      trigger: "entry" as const,
      channels: ["ops"],
      top_n: null,
      rank_delta: null,
      entry_rank: null,
      exit_rank: null,
      exit_after: null,
      initial_match: false,
      message: null,
    };
    const issues = screenerDraftIssues(draftWith({ attachments: [attachment, { ...attachment }] }));
    expect(issues.join(" ")).toMatch(/more than once/);
  });

  it("enforces the top_n and freshness bounds", () => {
    expect(screenerDraftIssues(draftWith({ top_n: 0 })).join(" ")).toMatch(/top_n/);
    expect(screenerDraftIssues(draftWith({ top_n: 5000 })).join(" ")).toMatch(/top_n/);
    expect(screenerDraftIssues(draftWith({ freshness_limit_s: 60 })).join(" ")).toMatch(/freshness/i);
  });

  it("has no complaints about a fresh default draft", () => {
    // The default draft targets a universe whose single ref is unnamed, which
    // IS a reported problem — so fill it in to assert a genuinely clean state.
    const clean = draftWith({
      universe: { union: [{ kind: "index", name: "Nifty50" }], exclude: [], deduplicate: true },
    });
    expect(screenerDraftIssues(clean)).toEqual([]);
  });
});

describe("documentToScreenerDraft", () => {
  it("round-trips a document the editor produced", () => {
    const draft = draftWith({
      targeting: "universe",
      universe: { union: [{ kind: "index", name: "Nifty50" }], exclude: [], deduplicate: true },
      attachments: [
        {
          id: "top10",
          trigger: "top_n",
          channels: ["ops"],
          top_n: 10,
          rank_delta: null,
          entry_rank: 10,
          exit_rank: 15,
          exit_after: 2,
          initial_match: false,
          message: null,
        },
      ],
    });
    const result = documentToScreenerDraft(buildScreenerDocument(draft));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.draft.name).toBe(draft.name);
      expect(result.draft.top_n).toBe(draft.top_n);
      expect(result.draft.universe.union).toEqual(draft.universe.union);
      expect(result.draft.attachments).toEqual(draft.attachments);
      expect(result.draft.schedule).toEqual(draft.schedule);
    }
  });

  it("refuses an alert document", () => {
    const result = documentToScreenerDraft({ version: 1, stages: [], alerts: [] });
    expect(result.ok).toBe(false);
  });

  it("refuses a screener with several scan stages", () => {
    const document = buildScreenerDocument(draftWith());
    const stages = document.stages as unknown[];
    const result = documentToScreenerDraft({ ...document, stages: [...stages, ...stages] });
    expect(result.ok).toBe(false);
  });

  it("refuses an intersected universe rather than dropping the restriction", () => {
    const document = buildScreenerDocument(
      draftWith({
        targeting: "universe",
        universe: { union: [{ kind: "index", name: "Nifty50" }], exclude: [], deduplicate: true },
      }),
    );
    const universe = { ...(document.universe as Record<string, unknown>) };
    universe.intersect = [{ kind: "universe", name: "liquid" }];
    const result = documentToScreenerDraft({ ...document, universe });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/intersect/i);
  });

  it("refuses an unknown attachment trigger", () => {
    const document = buildScreenerDocument(draftWith());
    const screener = { ...(document.screener as Record<string, unknown>) };
    screener.attachments = [{ id: "x", trigger: "explode", channels: ["ops"] }];
    const result = documentToScreenerDraft({ ...document, screener });
    expect(result.ok).toBe(false);
  });

  it("returns false for a null document", () => {
    expect(documentToScreenerDraft(null).ok).toBe(false);
  });
});

describe("attachmentSupportsRankBands", () => {
  it("offers rank bands only where the parser accepts them", () => {
    expect(attachmentSupportsRankBands("top_n")).toBe(true);
    expect(attachmentSupportsRankBands("entry")).toBe(true);
    expect(attachmentSupportsRankBands("exit")).toBe(true);
    expect(attachmentSupportsRankBands("rank_delta")).toBe(false);
  });
});
