import { describe, expect, it } from "vitest";

import { buildDocument, documentToDraft, emptyDraft, type AlertDraft } from "./authoring";

const DRAFT: AlertDraft = {
  ...emptyDraft(),
  name: "reliance-breakout",
  session: "nse_equity",
  clock: "candle_close",
  timeframe: "15minute",
  instruments: ["NSE:RELIANCE"],
  conditions: [
    { left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 3000 } },
  ],
  alert: {
    id: "a1",
    trigger: "on_transition",
    channels: ["ops"],
    cooldown_s: 300,
    rearm_level: null,
    rearm_direction: null,
    reminder_interval_s: null,
    notify_if_already_true: false,
    max_per_session: 5,
  },
};

describe("documentToDraft", () => {
  it("round-trips a document the editor produced", () => {
    const result = documentToDraft(buildDocument(DRAFT));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.draft).toEqual(DRAFT);
    }
  });

  it("round-trips an indicator operand with its period", () => {
    const draft: AlertDraft = {
      ...DRAFT,
      conditions: [
        { left: { kind: "indicator", name: "rsi", period: 14 }, op: "lt", right: { kind: "constant", value: 70 } },
      ],
    };
    const result = documentToDraft(buildDocument(draft));
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.draft.conditions[0].left).toEqual({ kind: "indicator", name: "rsi", period: 14 });
  });

  it("reads a universe-targeted alert instead of dropping the universe", () => {
    // Superseded an earlier refusal: the editor now models universe targeting,
    // so this document is editable rather than read-only. See
    // universe-targeting.test.ts for the expression cases (and the `intersect`
    // form, which is still refused because there is no control for it).
    const document = {
      ...buildDocument(DRAFT),
      instruments: [],
      universe: { union: [{ kind: "universe", name: "top100" }] },
    };
    const result = documentToDraft(document);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.draft.targeting).toBe("universe");
      expect(result.draft.universe.union).toEqual([{ kind: "universe", name: "top100" }]);
    }
  });

  it("refuses a screener", () => {
    const result = documentToDraft({ ...buildDocument(DRAFT), screener: { schedule: { every: "1d" } } });
    expect(result.ok).toBe(false);
  });

  it("refuses advanced conditions it does not model, rather than discarding them", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    stages[0] = {
      ...stages[0],
      sequence: { first: { all: [] }, then: { all: [] }, within_bars: 5 },
    };
    const result = documentToDraft(document);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/sequence|advanced/i);
  });

  it("refuses per-condition hysteresis", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const conditions = stages[0].conditions as { all: Array<Record<string, unknown>> };
    conditions.all[0] = { ...conditions.all[0], hysteresis: { release: 2900 } };
    const result = documentToDraft(document);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/hysteresis/i);
  });

  it("refuses a pair operand", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const conditions = stages[0].conditions as { all: Array<Record<string, unknown>> };
    conditions.all[0] = {
      left: { pair_ratio: { instrument: "NSE:A", reference: "NSE:B" } },
      op: "gt",
      right: 1.05,
    };
    const result = documentToDraft(document);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/pair|expression/i);
  });

  it("refuses multiple stages", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const result = documentToDraft({ ...document, stages: [...stages, stages[0]] });
    expect(result.ok).toBe(false);
  });

  it("refuses 'any'/'not' groups", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    stages[0] = { ...stages[0], any_conditions: [{ op: "gt", left: { field: "close" }, right: 1 }] };
    const result = documentToDraft(document);
    expect(result.ok).toBe(false);
  });

  it("handles a null document", () => {
    expect(documentToDraft(null).ok).toBe(false);
  });
});
