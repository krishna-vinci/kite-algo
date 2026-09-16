/**
 * Plain language for the alert authoring surface.
 *
 * Every backend vocabulary item the operator should not have to learn lives
 * here as a mapping, so the UI has exactly one place that decides what a clock,
 * a trigger, a timeframe or a limit is called. The canonical values stay
 * untouched in the document — these functions are display and translation only.
 */

import type { AlertDraft, AlertTriggerDraft, Operand } from "@/features/alerts/lib/authoring";

// ---------------------------------------------------------------------------
// evaluation (clock) and timeframes
// ---------------------------------------------------------------------------

export const EVALUATION_OPTIONS = [
  {
    value: "ltp",
    label: "Live price",
    hint: "Checked on every price tick, so a crossing is noticed immediately.",
  },
  {
    value: "candle_close",
    label: "Completed candle",
    hint: "Checked once a candle closes, so intrabar noise cannot trigger it.",
  },
] as const;

const TIMEFRAME_LABELS: Record<string, string> = {
  minute: "1 minute",
  "3minute": "3 minutes",
  "5minute": "5 minutes",
  "10minute": "10 minutes",
  "15minute": "15 minutes",
  "30minute": "30 minutes",
  "60minute": "1 hour",
  day: "1 day",
  week: "1 week",
};

export function timeframeLabel(code: string | null | undefined): string {
  const key = String(code ?? "").trim();
  if (!key) return "";
  return TIMEFRAME_LABELS[key] ?? key;
}

export function timeframeOptions(codes: readonly string[]): Array<{ value: string; label: string }> {
  return codes.map((code) => ({ value: code, label: timeframeLabel(code) }));
}

export function evaluationLabel(clock: string | null | undefined): string {
  const found = EVALUATION_OPTIONS.find((option) => option.value === clock);
  return found?.label ?? "Live price";
}

/** Operators measured between completed candles rather than against a level. */
const CANDLE_ONLY_OPERATORS = new Set(["rises_pct", "falls_pct", "rose_pct", "fell_pct"]);

/** Whether this definition needs a timeframe control at all. */
export function needsTimeframe(draft: Pick<AlertDraft, "clock" | "conditions">): boolean {
  if (draft.clock === "candle_close") return true;
  return draft.conditions.some((condition) => CANDLE_ONLY_OPERATORS.has(condition.op));
}

// ---------------------------------------------------------------------------
// notification frequency (trigger + reminder)
// ---------------------------------------------------------------------------

export type FrequencyChoice = "once" | "repeated" | "reminder";

export const FREQUENCY_OPTIONS: Array<{
  value: FrequencyChoice;
  label: string;
  hint: string;
}> = [
  {
    value: "once",
    label: "Once when it happens",
    hint: "One notification, then the alert goes quiet until you switch it on again.",
  },
  {
    value: "repeated",
    label: "Every time it happens again",
    hint: "Notifies on each new crossing, so you hear about a level every time it is crossed.",
  },
  {
    value: "reminder",
    label: "Remind me while it remains true",
    hint: "Repeats at an interval for as long as the condition stays true.",
  },
];

export function frequencyOf(trigger: Pick<AlertTriggerDraft, "trigger" | "reminder_interval_s">): FrequencyChoice {
  if ((trigger.reminder_interval_s ?? null) !== null) return "reminder";
  return trigger.trigger === "on_transition" ? "repeated" : "once";
}

export function applyFrequency(
  trigger: AlertTriggerDraft,
  choice: FrequencyChoice,
): AlertTriggerDraft {
  if (choice === "reminder") {
    return {
      ...trigger,
      trigger: "on_transition",
      reminder_interval_s: trigger.reminder_interval_s ?? 900,
    };
  }
  if (choice === "repeated") {
    return { ...trigger, trigger: "on_transition", reminder_interval_s: null };
  }
  return { ...trigger, trigger: "once", reminder_interval_s: null };
}

/** One sentence describing when the operator will hear from this alert. */
export function describeFrequency(trigger: AlertTriggerDraft): string {
  const choice = frequencyOf(trigger);
  const parts: string[] = [];
  if (choice === "once") parts.push("You are notified once, on the first time it happens.");
  else if (choice === "repeated") parts.push("You are notified every time it happens again.");
  else
    parts.push(
      `You are reminded every ${formatDuration(trigger.reminder_interval_s ?? 900)} for as long as it stays true.`,
    );
  if (trigger.cooldown_s) {
    parts.push(`At most one notification every ${formatDuration(trigger.cooldown_s)}.`);
  }
  if (trigger.max_per_session) {
    parts.push(`No more than ${trigger.max_per_session} notification${trigger.max_per_session === 1 ? "" : "s"} a day.`);
  }
  if (trigger.rearm_level !== null && trigger.rearm_level !== undefined) {
    const direction = trigger.rearm_direction === "below" ? "below" : "above";
    parts.push(`It becomes ready again once the price moves ${direction} ${formatPrice(trigger.rearm_level)}.`);
  }
  if (trigger.notify_if_already_true) {
    parts.push("It notifies immediately if the condition is already true when switched on.");
  }
  return parts.join(" ");
}

export function formatDuration(seconds: number): string {
  const value = Math.max(0, Math.round(seconds));
  if (value % 3600 === 0 && value >= 3600) {
    const hours = value / 3600;
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  if (value % 60 === 0 && value >= 60) {
    const minutes = value / 60;
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  return `${value} second${value === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------------------
// prices, targets and distance
// ---------------------------------------------------------------------------

export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  const digits = magnitude >= 1000 ? 2 : magnitude >= 1 ? 2 : 4;
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function formatAge(ageMs: number | null | undefined): string {
  if (ageMs === null || ageMs === undefined || !Number.isFinite(ageMs)) return "no update yet";
  const seconds = Math.max(0, Math.round(ageMs / 1000));
  if (seconds < 1) return "updated just now";
  if (seconds < 60) return `updated ${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `updated ${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `updated ${hours}h ago`;
}

export type TargetDistance = {
  /** Signed distance from the current price to the target. */
  distance: number;
  percent: number;
  direction: "above" | "below";
  sentence: string;
  /** True when the price is already past a crossing level. */
  alreadyBeyond: boolean;
};

/**
 * How far the entered target is from the live price, in the operator's terms.
 *
 * `op` decides which side is "already there": for a crossing-above rule, a price
 * already above the target means the alert waits for a reset rather than firing,
 * and saying so is more useful than a number.
 */
export function describeTarget(
  target: number | null | undefined,
  price: number | null | undefined,
  op?: string | null,
): TargetDistance | null {
  if (target === null || target === undefined || !Number.isFinite(target)) return null;
  if (price === null || price === undefined || !Number.isFinite(price) || price === 0) return null;
  const distance = target - price;
  const percent = (distance / price) * 100;
  const direction = distance >= 0 ? "above" : "below";
  const upward = new Set(["crosses_above", "gt", "gte", "rises_pct", "rose_pct"]);
  const downward = new Set(["crosses_below", "lt", "lte", "falls_pct", "fell_pct"]);
  const isUpward = op ? upward.has(op) : distance >= 0;
  const isDownward = op ? downward.has(op) : distance < 0;
  let alreadyBeyond = false;
  if (isUpward) alreadyBeyond = price > target;
  if (isDownward) alreadyBeyond = price < target;

  const relative = `${formatPrice(Math.abs(distance))} ${direction === "above" ? "above" : "below"} the current price`;
  // For a crossed level the sentence is about the ALERT, not about where the
  // target sits relative to the price: "crosses above X" with a price already
  // above X is waiting for a move back below, and saying "below" (the target's
  // position) would describe the opposite of what happens.
  const side = isUpward ? "above" : isDownward ? "below" : direction === "above" ? "above" : "below";
  const reset = side === "above" ? "below" : "above";
  const sentence = alreadyBeyond
    ? `Price is already ${side} ${formatPrice(target)}. This alert will wait for the price to move ${reset} the target and cross it again.`
    : `Target is ${relative} (${formatPercent(percent)}).`;

  return { distance, percent, direction, sentence, alreadyBeyond };
}

/**
 * A target suggested from the live price, at a sensible precision.
 *
 * Rounded to two decimals (four for sub-rupee prices) rather than snapped to a
 * guessed tick: the instrument's real tick size is not part of the capability
 * payload here, and inventing one would put a wrong number in the field.
 */
export function offsetPrice(price: number, percent: number): number {
  const raw = price * (1 + percent / 100);
  const magnitude = Math.abs(price);
  const factor = magnitude >= 1 ? 100 : 10_000;
  return Math.round(raw * factor) / factor;
}

export const TARGET_SHORTCUTS: Array<{ label: string; percent: number | null }> = [
  { label: "Use current price", percent: 0 },
  { label: "+0.5%", percent: 0.5 },
  { label: "+1%", percent: 1 },
  { label: "-0.5%", percent: -0.5 },
];

// ---------------------------------------------------------------------------
// session inference
// ---------------------------------------------------------------------------

export type SessionInference = {
  session: string;
  exchange: string;
  /** Set when the exchange maps to no session or to more than one. */
  error: string | null;
};

/**
 * The session a canonical instrument key belongs to.
 *
 * Ambiguity is an error, never a guess: an alert with the wrong session would be
 * rejected by the server (or worse, silently evaluate nothing), and the operator
 * has no way to see why.
 */
export function inferSession(
  instrumentKey: string | null | undefined,
  sessionExchanges: Record<string, string[]> | undefined,
): SessionInference {
  const key = String(instrumentKey ?? "").trim();
  const exchange = key.includes(":") ? key.split(":")[0].trim().toUpperCase() : "";
  if (!exchange) return { session: "", exchange: "", error: null };
  const map = sessionExchanges ?? {};
  const matches = Object.entries(map)
    .filter(([, exchanges]) => (exchanges ?? []).some((item) => item.trim().toUpperCase() === exchange))
    .map(([session]) => session);
  if (matches.length === 1) return { session: matches[0], exchange, error: null };
  if (matches.length === 0) {
    return {
      session: "",
      exchange,
      error: `${exchange} is not part of any session this deployment supports, so this alert cannot be evaluated.`,
    };
  }
  return {
    session: "",
    exchange,
    error: `${exchange} belongs to more than one session (${matches.join(", ")}); choose the session explicitly in Code view.`,
  };
}

// ---------------------------------------------------------------------------
// rule summary
// ---------------------------------------------------------------------------

/** "GOLD OCT crosses above ₹125,000" — the row/title form of a simple rule. */
export function describeRule(
  instrumentLabel: string,
  operatorLabel: string,
  value: number | null | undefined,
): string {
  const target = value === null || value === undefined ? "…" : formatPrice(value);
  return `${instrumentLabel} ${operatorLabel} ${target}`.trim();
}

/**
 * One side of a condition in the operator's words.
 *
 * A field reads as its own name, because that name is the vocabulary the
 * operator picked it from. An indicator carries its period — "ema 9" is not how
 * anyone says it out loud, and the period is what makes two uses of the same
 * indicator different operands.
 */
export function describeOperand(operand: Operand): string {
  switch (operand.kind) {
    case "constant":
      return formatPrice(operand.value);
    case "field":
      return operand.name;
    case "indicator":
      return operand.period === undefined
        ? operand.name.toUpperCase()
        : `${operand.name.toUpperCase()} ${operand.period}`;
  }
}

/**
 * The whole rule in the operator's words, whichever operands it compares.
 *
 * With a level on the right the instrument is the subject — "INFY crosses above
 * ₹1,500" — which is the sentence this rail has always shown. When the right
 * side is itself an operand, the instrument is no longer what the sentence is
 * about: "EMA 9 crosses above EMA 19" is the complete rule, and naming the
 * instrument in front of it would read as a second subject.
 */
export function describeCondition(
  instrumentLabel: string,
  operatorLabel: string,
  condition: { left: Operand; right: Operand },
  target: number | null | undefined,
): string {
  if (condition.right.kind === "constant") {
    return describeRule(instrumentLabel, operatorLabel, target);
  }
  const sides = `${describeOperand(condition.left)} ${operatorLabel} ${describeOperand(condition.right)}`;
  return sides.replace(/\s+/g, " ").trim();
}

/** A name suggested from the definition, which the operator can override. */
export function suggestName(
  symbol: string,
  operatorLabel: string,
  value: number | null | undefined,
  operands?: { left: Operand; right: Operand },
): string {
  if (!symbol) return "";
  // An operand-vs-operand rule names both of its sides; the level form below
  // would have no price to put after the operator.
  if (operands && operands.right.kind !== "constant") {
    return describeCondition(symbol, operatorLabel, operands, value).replace(/\s+/g, " ").trim();
  }
  const target = value === null || value === undefined ? "" : formatPrice(value).replace("₹", "");
  return `${symbol} ${operatorLabel} ${target}`.replace(/\s+/g, " ").trim();
}
