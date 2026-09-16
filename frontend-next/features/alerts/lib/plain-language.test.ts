/**
 * Plain language: the mapping between backend vocabulary and what the operator
 * reads. If one of these drifts, the UI starts teaching the wrong mental model,
 * so each mapping is asserted rather than assumed.
 */

import { describe, expect, it } from "vitest";

import type { AlertTriggerDraft } from "@/features/alerts/lib/authoring";
import {
  applyFrequency,
  describeFrequency,
  describeTarget,
  evaluationLabel,
  formatAge,
  formatDuration,
  formatPercent,
  formatPrice,
  frequencyOf,
  inferSession,
  needsTimeframe,
  offsetPrice,
  suggestName,
  timeframeLabel,
} from "@/features/alerts/lib/plain-language";
import { groupIssues, previewSentence, sectionForIssue } from "@/features/alerts/hooks/use-definition-validation";

const SESSIONS = { nse_equity: ["NSE"], mcx_commodity: ["MCX"], currency: ["CDS", "BCD"] };

function trigger(overrides: Partial<AlertTriggerDraft> = {}): AlertTriggerDraft {
  return {
    id: "a1",
    trigger: "once",
    channels: ["telegram_ops"],
    cooldown_s: null,
    rearm_level: null,
    rearm_direction: null,
    reminder_interval_s: null,
    notify_if_already_true: false,
    max_per_session: null,
    ...overrides,
  };
}

describe("timeframes and evaluation", () => {
  it("renders timeframes as durations, never as backend codes", () => {
    expect(timeframeLabel("minute")).toBe("1 minute");
    expect(timeframeLabel("15minute")).toBe("15 minutes");
    expect(timeframeLabel("60minute")).toBe("1 hour");
    expect(timeframeLabel("day")).toBe("1 day");
    // an unknown code is shown as-is rather than mangled
    expect(timeframeLabel("3minute")).toBe("3 minutes");
    expect(timeframeLabel("weird")).toBe("weird");
  });

  it("names the two evaluations the operator chooses between", () => {
    expect(evaluationLabel("ltp")).toBe("Live price");
    expect(evaluationLabel("candle_close")).toBe("Completed candle");
    expect(evaluationLabel(undefined)).toBe("Live price");
  });

  it("asks for a timeframe only when something needs one", () => {
    expect(needsTimeframe({ clock: "ltp", conditions: [{ op: "crosses_above" } as never] })).toBe(false);
    expect(needsTimeframe({ clock: "candle_close", conditions: [{ op: "crosses_above" } as never] })).toBe(true);
    // a percentage move is measured between candles even when the clock says ltp
    expect(needsTimeframe({ clock: "ltp", conditions: [{ op: "rises_pct" } as never] })).toBe(true);
  });
});

describe("notification frequency", () => {
  it("maps the three choices onto the canonical trigger fields", () => {
    const once = applyFrequency(trigger(), "once");
    expect(once.trigger).toBe("once");
    expect(once.reminder_interval_s).toBeNull();

    const repeated = applyFrequency(trigger(), "repeated");
    expect(repeated.trigger).toBe("on_transition");
    expect(repeated.reminder_interval_s).toBeNull();

    const reminder = applyFrequency(trigger(), "reminder");
    expect(reminder.trigger).toBe("on_transition");
    expect(reminder.reminder_interval_s).toBe(900);
  });

  it("reads the stored trigger back into a choice", () => {
    expect(frequencyOf(trigger())).toBe("once");
    expect(frequencyOf(trigger({ trigger: "on_transition" }))).toBe("repeated");
    expect(frequencyOf(trigger({ trigger: "on_transition", reminder_interval_s: 60 }))).toBe("reminder");
  });

  it("explains the outcome, including the limits, in one sentence", () => {
    const sentence = describeFrequency(
      trigger({ cooldown_s: 300, max_per_session: 5, rearm_level: 125_000, rearm_direction: "below" }),
    );
    expect(sentence).toContain("notified once");
    expect(sentence).toContain("every 5 minutes");
    expect(sentence).toContain("5 notifications a day");
    expect(sentence).toContain("becomes ready again");
    // no backend vocabulary leaks into the sentence
    expect(sentence).not.toMatch(/cooldown|max_per_session|rearm_level|on_transition/);
  });

  it("formats durations the way a person would say them", () => {
    expect(formatDuration(60)).toBe("1 minute");
    expect(formatDuration(900)).toBe("15 minutes");
    expect(formatDuration(3600)).toBe("1 hour");
    expect(formatDuration(45)).toBe("45 seconds");
  });
});

describe("targets and distance", () => {
  it("formats prices for Indian markets", () => {
    expect(formatPrice(124_860)).toBe("₹1,24,860.00");
    expect(formatPrice(0.5)).toBe("₹0.5000");
    expect(formatPrice(null)).toBe("—");
    expect(formatPercent(0.11)).toBe("+0.11%");
    expect(formatPercent(-1)).toBe("-1.00%");
  });

  it("describes how far the target is from the live price", () => {
    const target = describeTarget(125_000, 124_860, "crosses_above");
    expect(target?.sentence).toBe("Target is ₹140.00 above the current price (+0.11%).");
    expect(target?.alreadyBeyond).toBe(false);
  });

  it("says the price is already past a crossing level, from the alert's side", () => {
    const above = describeTarget(124_000, 124_860, "crosses_above");
    expect(above?.alreadyBeyond).toBe(true);
    expect(above?.sentence).toContain("Price is already above");
    expect(above?.sentence).toContain("move below the target and cross it again");

    const below = describeTarget(125_000, 124_860, "crosses_below");
    expect(below?.alreadyBeyond).toBe(true);
    expect(below?.sentence).toContain("Price is already below");
    expect(below?.sentence).toContain("move above the target");
  });

  it("says nothing without a price or a target", () => {
    expect(describeTarget(null, 124_860, "crosses_above")).toBeNull();
    expect(describeTarget(125_000, null, "crosses_above")).toBeNull();
  });

  it("suggests targets at a sane precision", () => {
    expect(offsetPrice(124_860, 0)).toBe(124_860);
    expect(offsetPrice(124_860, 1)).toBe(126_108.6);
    expect(offsetPrice(124_860, -0.5)).toBe(124_235.7);
    expect(offsetPrice(0.8456, 1)).toBeCloseTo(0.8541, 4);
  });

  it("never claims to know a tick size it was not given", () => {
    // two decimals for rupee-scale instruments, four for sub-rupee ones
    expect(String(offsetPrice(99.94, 0.5))).toBe("100.44");
    expect(String(offsetPrice(0.1234, 1))).toBe("0.1246");
  });
});

describe("session inference", () => {
  it("infers the session from the instrument's exchange", () => {
    expect(inferSession("MCX:GOLD26DECFUT", SESSIONS).session).toBe("mcx_commodity");
    expect(inferSession("NSE:RELIANCE", SESSIONS).session).toBe("nse_equity");
    expect(inferSession("CDS:USDINR26OCTFUT", SESSIONS).session).toBe("currency");
  });

  it("reports an unknown exchange instead of guessing", () => {
    const result = inferSession("BSE:SENSEX", SESSIONS);
    expect(result.session).toBe("");
    expect(result.error).toMatch(/not part of any session/);
  });

  it("refuses to choose when an exchange belongs to several sessions", () => {
    const result = inferSession("BCD:USDINR", { currency: ["CDS", "BCD"], other: ["BCD"] });
    expect(result.session).toBe("");
    expect(result.error).toMatch(/more than one session/);
  });

  it("says nothing about an empty key", () => {
    expect(inferSession("", SESSIONS)).toEqual({ session: "", exchange: "", error: null });
  });
});

describe("age and names", () => {
  it("describes freshness age in words", () => {
    expect(formatAge(400)).toBe("updated just now");
    expect(formatAge(1200)).toBe("updated 1s ago");
    expect(formatAge(90_000)).toBe("updated 2m ago");
    expect(formatAge(null)).toBe("no update yet");
  });

  it("suggests a name from the definition", () => {
    expect(suggestName("GOLD26DECFUT", "crosses above", 125_000)).toBe(
      "GOLD26DECFUT crosses above 1,25,000.00",
    );
    expect(suggestName("", "crosses above", 1)).toBe("");
  });
});

describe("validation presentation", () => {
  it("maps a server issue path to the section that can fix it", () => {
    expect(sectionForIssue({ where: "document.session", code: "", message: "", severity: "error" })).toBe("instrument");
    expect(sectionForIssue({ where: "stages.px.conditions", code: "", message: "", severity: "error" })).toBe("rule");
    expect(sectionForIssue({ where: "stages.px.clock", code: "", message: "", severity: "error" })).toBe("evaluation");
    expect(
      sectionForIssue({ where: "alerts.a1", code: "", message: "unknown channel", severity: "error" }),
    ).toBe("destinations");
    expect(sectionForIssue({ where: "alerts.a1", code: "", message: "bad trigger", severity: "error" })).toBe(
      "frequency",
    );
  });

  it("groups messages, never raw codes", () => {
    const grouped = groupIssues([
      { where: "stages.px.conditions", code: "bad_value", message: "value must be a number", severity: "error" },
      { where: "document.session", code: "session_mismatch", message: "MCX is not in this session", severity: "error" },
    ]);
    expect(grouped.rule).toEqual(["value must be a number"]);
    expect(grouped.instrument).toEqual(["MCX is not in this session"]);
    expect(JSON.stringify(grouped)).not.toContain("bad_value");
  });

  it("turns a preview result into a sentence", () => {
    expect(
      previewSentence({ ok: true, issues: [], would_fire: [{ alert_id: "a1" }], evaluation: "dry_run" } as never),
    ).toContain("would fire");
    expect(previewSentence({ ok: true, issues: [], evaluation: "dry_run_no_data" } as never)).toContain(
      "Not enough data",
    );
    expect(previewSentence({ ok: true, issues: [], would_fire: [], evaluation: "dry_run" } as never)).toContain(
      "would not fire yet",
    );
    expect(previewSentence(null)).toBeNull();
  });
});
