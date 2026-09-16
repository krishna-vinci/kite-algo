import { describe, expect, it } from "vitest";

import {
  buildDocument,
  effectiveTimeframe,
  emptyDraft,
  exchangeOf,
  incompatibleInstruments,
  isLevelOnly,
  levelOnlyWarning,
  operandToDocument,
  operatorGroup,
  sessionAcceptsExchange,
  type AlertDraft,
} from "./authoring";
import type { AlertsCapabilities } from "@/features/alerts/types";

const OPERATORS: AlertsCapabilities["operators"] = {
  gt: "level",
  lt: "level",
  crosses_above: "crossing",
  crosses_below: "crossing",
  rises_pct: "pct",
};

const SESSION_EXCHANGES: AlertsCapabilities["session_exchanges"] = {
  nse_equity: ["NSE"],
  mcx_commodity: ["MCX"],
  currency: ["CDS", "BCD"],
};

describe("operandToDocument", () => {
  it("emits a bare number for a constant", () => {
    expect(operandToDocument({ kind: "constant", value: 3000 })).toBe(3000);
  });

  it("emits the field shorthand", () => {
    expect(operandToDocument({ kind: "field", name: "close" })).toEqual({ field: "close" });
  });

  it("emits the indicator shorthand, with period only when set", () => {
    expect(operandToDocument({ kind: "indicator", name: "rsi" })).toEqual({ indicator: "rsi" });
    expect(operandToDocument({ kind: "indicator", name: "rsi", period: 14 })).toEqual({
      indicator: "rsi",
      period: 14,
    });
  });
});

describe("operatorGroup", () => {
  it("reads the group from capabilities rather than a local table", () => {
    expect(operatorGroup("gt", OPERATORS)).toBe("level");
    expect(operatorGroup("crosses_above", OPERATORS)).toBe("crossing");
  });

  it("falls back to 'other' for an unknown operator", () => {
    expect(operatorGroup("mystery", OPERATORS)).toBe("other");
  });
});

describe("isLevelOnly / levelOnlyWarning", () => {
  const levelConditions = [
    { left: { kind: "field" as const, name: "close" }, op: "gt", right: { kind: "constant" as const, value: 100 } },
  ];

  it("detects a rule that can only report current truth", () => {
    expect(isLevelOnly(levelConditions, OPERATORS)).toBe(true);
  });

  it("is false when a crossing operator is present", () => {
    expect(
      isLevelOnly(
        [{ left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 100 } }],
        OPERATORS,
      ),
    ).toBe(false);
  });

  it("warns for a transition trigger and explains the fix", () => {
    const warning = levelOnlyWarning(levelConditions, "on_transition", OPERATORS);
    expect(warning).toContain("never notify");
    expect(warning).toContain("crossing operator");
  });

  it("stays silent for the reminder trigger, which can still fire", () => {
    // Mirrors the compiler, which does not warn when a reminder (or the
    // notify_if_already_true opt-in) makes a level-only rule legitimate.
    expect(levelOnlyWarning(levelConditions, "reminder", OPERATORS)).toBeNull();
  });

  it("stays silent when notify_if_already_true opts in", () => {
    expect(
      levelOnlyWarning(levelConditions, "on_transition", OPERATORS, {
        notifyIfAlreadyTrue: true,
      }),
    ).toBeNull();
  });

  it("stays silent when consecutive_bars produces the transition", () => {
    expect(
      levelOnlyWarning(levelConditions, "on_transition", OPERATORS, { consecutiveBars: 3 }),
    ).toBeNull();
  });

  it("does not warn when an OR/NOT group contains a crossing operator", () => {
    // The warning looks at every group, so a crossing in `any` means the rule
    // can transition and must not be flagged.
    expect(
      levelOnlyWarning(levelConditions, "on_transition", OPERATORS, {
        anyConditions: [
          { left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 1 } },
        ],
      }),
    ).toBeNull();
  });

  it("warns when every group is level-only", () => {
    const warning = levelOnlyWarning(levelConditions, "on_transition", OPERATORS, {
      anyConditions: [
        { left: { kind: "field", name: "close" }, op: "gt", right: { kind: "constant", value: 1 } },
      ],
    });
    expect(warning).toContain("never notify");
  });

  it("stays silent when the rule can transition", () => {
    expect(
      levelOnlyWarning(
        [{ left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 1 } }],
        "on_transition",
        OPERATORS,
      ),
    ).toBeNull();
  });
});

describe("session compatibility", () => {
  it("mirrors the compiler's case-insensitive exchange rule", () => {
    expect(sessionAcceptsExchange("nse_equity", "NSE", SESSION_EXCHANGES)).toBe(true);
    expect(sessionAcceptsExchange("nse_equity", "nse", SESSION_EXCHANGES)).toBe(true);
    expect(sessionAcceptsExchange("nse_equity", "MCX", SESSION_EXCHANGES)).toBe(false);
  });

  it("extracts the exchange from a qualified key", () => {
    expect(exchangeOf("NSE:RELIANCE")).toBe("NSE");
    expect(exchangeOf("oops")).toBe("");
  });

  it("lists exactly the incompatible instruments", () => {
    const bad = incompatibleInstruments(
      "nse_equity",
      ["NSE:RELIANCE", "MCX:GOLD", "CDS:USDINR"],
      SESSION_EXCHANGES,
    );
    expect(bad.map((item) => item.instrumentKey)).toEqual(["MCX:GOLD", "CDS:USDINR"]);
  });
});

describe("buildDocument", () => {
  const draft: AlertDraft = {
    ...emptyDraft(),
    name: "reliance-breakout",
    session: "nse_equity",
    instruments: ["NSE:RELIANCE"],
    conditions: [
      { left: { kind: "field", name: "close" }, op: "crosses_above", right: { kind: "constant", value: 3000 } },
    ],
  };

  it("emits the canonical skeleton", () => {
    const document = buildDocument(draft);
    expect(document.version).toBe(1);
    expect(document.name).toBe("reliance-breakout");
    expect(document.session).toBe("nse_equity");
    expect(document.instruments).toEqual(["NSE:RELIANCE"]);
    expect(document.universe).toBeNull();
    expect((document.stages as unknown[]).length).toBe(1);
  });

  it("wraps conditions in a single `all` group on a signal stage", () => {
    const stage = (buildDocument(draft).stages as Array<Record<string, unknown>>)[0];
    expect(stage.id).toBe("px");
    expect(stage.type).toBe("signal");
    expect(stage.conditions).toEqual({
      all: [
        { left: { field: "close" }, op: "crosses_above", right: 3000 },
      ],
    });
  });

  it("omits unset optional alert keys so an untouched field cannot move the hash", () => {
    const alert = (buildDocument(draft).alerts as Array<Record<string, unknown>>)[0];
    expect(alert).toEqual({
      id: "a1",
      source: "px",
      trigger: "on_transition",
      channels: [],
    });
    for (const key of [
      "cooldown_s",
      "rearm_level",
      "rearm_direction",
      "reminder_interval_s",
      "notify_if_already_true",
      "max_per_session",
    ]) {
      expect(alert).not.toHaveProperty(key);
    }
  });

  it("emits any/not groups and consecutive_bars only when used", () => {
    const bare = buildDocument(draft);
    const bareStage = (bare.stages as Array<Record<string, unknown>>)[0];
    expect(bareStage).not.toHaveProperty("any_conditions");
    expect(bareStage).not.toHaveProperty("not_conditions");
    expect(bareStage).not.toHaveProperty("consecutive_bars");

    const rich = buildDocument({
      ...draft,
      anyConditions: [
        { left: { kind: "field", name: "close" }, op: "gt", right: { kind: "constant", value: 1 } },
      ],
      notConditions: [
        { left: { kind: "field", name: "volume" }, op: "lt", right: { kind: "constant", value: 5 } },
      ],
      consecutiveBars: 3,
    });
    const stage = (rich.stages as Array<Record<string, unknown>>)[0];
    expect(stage.any_conditions).toHaveLength(1);
    expect(stage.not_conditions).toHaveLength(1);
    expect(stage.consecutive_bars).toBe(3);
  });

  it("emits a constant hysteresis release only when set", () => {
    const condition = {
      left: { kind: "field" as const, name: "close" },
      op: "gt",
      right: { kind: "constant" as const, value: 100 },
      hysteresis: { release: 90 },
    };
    const stage = (buildDocument({ ...draft, conditions: [condition] }).stages as Array<
      Record<string, unknown>
    >)[0];
    const emitted = (stage.conditions as { all: Array<Record<string, unknown>> }).all[0];
    expect(emitted.hysteresis).toEqual({ release: 90 });
  });

  it("includes optional alert keys once set, including max_per_session", () => {
    const alert = (
      buildDocument({
        ...draft,
        alert: {
          ...draft.alert,
          cooldown_s: 300,
          rearm_level: 2900,
          rearm_direction: "below",
          max_per_session: 5,
          notify_if_already_true: false,
        },
      }).alerts as Array<Record<string, unknown>>
    )[0];
    expect(alert.cooldown_s).toBe(300);
    expect(alert.rearm_level).toBe(2900);
    expect(alert.rearm_direction).toBe("below");
    expect(alert.max_per_session).toBe(5);
    // false must not be emitted: a false flag is the default and emitting it
    // would change the canonical hash of a document that did not set it.
    expect(alert).not.toHaveProperty("notify_if_already_true");
  });

  it("emits notify_if_already_true only when true", () => {
    const alert = (
      buildDocument({
        ...draft,
        alert: { ...draft.alert, notify_if_already_true: true },
      }).alerts as Array<Record<string, unknown>>
    )[0];
    expect(alert.notify_if_already_true).toBe(true);
  });
});

describe("timeframe repair", () => {
  it("never writes an empty timeframe into a stage", () => {
    // The schema requires a non-empty timeframe even for a live-price rule, and
    // the parser rejects ''. A definition stored without one must be repaired on
    // save instead of echoed back (which made the save fail with a server error).
    const draft = { ...emptyDraft(), timeframe: "" };
    const document = buildDocument(draft) as { stages: Array<{ timeframe?: string }> };
    expect(document.stages[0].timeframe).toBe("day");

    const merged = buildDocument(
      { ...draft, timeframe: "" },
      { version: 1, name: "x", session: "nse_equity", stages: [{ id: "px", type: "signal", clock: "ltp" }], alerts: [] },
    ) as { stages: Array<{ timeframe?: string }> };
    expect(merged.stages[0].timeframe).toBe("day");
  });

  it("keeps a real timeframe untouched", () => {
    expect(effectiveTimeframe("15minute")).toBe("15minute");
    expect(effectiveTimeframe(null)).toBe("day");
  });
});
