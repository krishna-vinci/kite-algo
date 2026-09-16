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

  it("reads a constant per-condition hysteresis release", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const conditions = stages[0].conditions as { all: Array<Record<string, unknown>> };
    conditions.all[0] = { ...conditions.all[0], hysteresis: { release: 2900 } };
    const result = documentToDraft(document);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.draft.conditions[0].hysteresis).toEqual({ release: 2900 });
  });

  it("refuses a condition carrying an unmodeled key instead of dropping it on save", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const conditions = stages[0].conditions as { all: Array<Record<string, unknown>> };
    conditions.all[0] = { ...conditions.all[0], note: "unmodeled" };
    const result = documentToDraft(document);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/does not model/i);
  });

  it("refuses a shorthand operand carrying an unmodeled attribute", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const conditions = stages[0].conditions as { all: Array<Record<string, unknown>> };
    conditions.all[0] = { ...conditions.all[0], left: { field: "close", source: "close" } };
    const result = documentToDraft(document);
    expect(result.ok).toBe(false);
  });

  it("refuses hysteresis carrying extra keys", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const conditions = stages[0].conditions as { all: Array<Record<string, unknown>> };
    conditions.all[0] = { ...conditions.all[0], hysteresis: { release: 2900, extra: 1 } };
    const result = documentToDraft(document);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/hysteresis/i);
  });

  it("refuses dynamic hysteresis, which is not implemented", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    const conditions = stages[0].conditions as { all: Array<Record<string, unknown>> };
    conditions.all[0] = { ...conditions.all[0], hysteresis: { release: { indicator: "rsi" } } };
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

  it("reads 'any'/'not' groups instead of refusing them", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    stages[0] = {
      ...stages[0],
      any_conditions: [{ op: "gt", left: { field: "close" }, right: 1 }],
      not_conditions: [{ op: "lt", left: { field: "volume" }, right: 100 }],
    };
    const result = documentToDraft(document);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.draft.anyConditions).toHaveLength(1);
      expect(result.draft.notConditions).toHaveLength(1);
    }
  });

  it("reads consecutive_bars into the draft", () => {
    const document = buildDocument(DRAFT);
    const stages = document.stages as Array<Record<string, unknown>>;
    stages[0] = { ...stages[0], consecutive_bars: 3 };
    const result = documentToDraft(document);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.draft.consecutiveBars).toBe(3);
  });

  it("handles a null document", () => {
    expect(documentToDraft(null).ok).toBe(false);
  });
});

/**
 * The backend STORES the canonical `to_document_dict()` form — conditions as a
 * list, verbose operands with `params`, instruments as `{symbol, exchange}`.
 * The editor must open that, not only the form-shaped shorthand its own tests
 * happen to produce.
 */
const CANONICAL: Record<string, unknown> = {
  version: 1,
  name: "canonical-alert",
  session: "nse_equity",
  instruments: [{ symbol: "S0001", exchange: "NSE" }],
  stages: [
    {
      id: "px",
      type: "signal",
      clock: "candle_close",
      timeframe: "15minute",
      input: null,
      conditions: [
        {
          left: { kind: "field", name: "close", value: null, params: {}, source: null, offset: null },
          op: "crosses_above",
          right: { kind: "value", name: null, value: 3000, params: {}, source: null, offset: null },
        },
      ],
      any_conditions: [],
      not_conditions: [],
      function: null,
      stage_params: {},
      source_field: null,
    },
  ],
  alerts: [
    {
      id: "a1",
      source: "px",
      trigger: "on_transition",
      reminder_interval_s: null,
      cooldown_s: 300,
      rearm_level: 2900,
      rearm_direction: "below",
      notify_if_already_true: false,
      expires_at: "2026-12-31T00:00:00Z",
      channels: ["ops"],
      message: "custom message",
      max_per_session: 5,
      session_cap_reset: "session",
    },
  ],
  data_policy: { missing: "exclude_and_report", insufficient_history: "wait", require_closed_candles: true },
};

describe("documentToDraft — canonical stored form", () => {
  it("opens a canonical document rather than refusing the list-form conditions", () => {
    const result = documentToDraft(CANONICAL);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.draft.instruments).toEqual(["NSE:S0001"]);
    expect(result.draft.conditions[0]).toEqual({
      left: { kind: "field", name: "close" },
      op: "crosses_above",
      right: { kind: "constant", value: 3000 },
    });
  });

  it("reads rearm level and direction instead of refusing them", () => {
    const result = documentToDraft(CANONICAL);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.draft.alert.rearm_level).toBe(2900);
    expect(result.draft.alert.rearm_direction).toBe("below");
  });

  it("keeps every field the editor does not model when saving (no-op is lossless)", () => {
    const conversion = documentToDraft(CANONICAL);
    expect(conversion.ok).toBe(true);
    if (!conversion.ok) return;
    const rebuilt = buildDocument(conversion.draft, CANONICAL);

    const alert = (rebuilt.alerts as Array<Record<string, unknown>>)[0];
    expect(alert.expires_at).toBe("2026-12-31T00:00:00Z");
    expect(alert.message).toBe("custom message");
    expect(alert.session_cap_reset).toBe("session");
    expect(alert.rearm_level).toBe(2900);
    expect(alert.rearm_direction).toBe("below");
    expect(alert.max_per_session).toBe(5);

    const stage = (rebuilt.stages as Array<Record<string, unknown>>)[0];
    expect(stage).toHaveProperty("input", null);
    expect(stage).toHaveProperty("any_conditions");
    expect(stage).toHaveProperty("stage_params");
    expect(stage).toHaveProperty("function", null);
    expect(rebuilt.data_policy).toEqual(CANONICAL.data_policy);
    expect(rebuilt.instruments).toEqual(["NSE:S0001"]);
  });

  it("removes an optional alert key when the editor clears it", () => {
    const conversion = documentToDraft(CANONICAL);
    expect(conversion.ok).toBe(true);
    if (!conversion.ok) return;
    const draft = { ...conversion.draft, alert: { ...conversion.draft.alert, cooldown_s: null } };
    const rebuilt = buildDocument(draft, CANONICAL);
    const alert = (rebuilt.alerts as Array<Record<string, unknown>>)[0];
    expect(alert).not.toHaveProperty("cooldown_s");
  });
});
