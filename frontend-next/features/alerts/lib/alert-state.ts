/**
 * Plain states for an alert, derived from what the platform actually knows.
 *
 * The operator's question is "is this thing watching, waiting, or broken?", and
 * the answer must never be invented: an alert with no evaluations yet is not
 * "watching", a closed market is not "stale data", and a crossing level the
 * price is already past is not "armed".
 */

import type { MarketQuote } from "@/features/alerts/lib/market-stream";

export type AlertState =
  | "archived"
  | "draft"
  | "paused"
  | "needs-attention"
  | "market-closed"
  | "stale"
  | "waiting-reset"
  | "waiting-first-crossing"
  | "watching";

export type AlertStateView = {
  state: AlertState;
  label: string;
  tone: "positive" | "warning" | "danger" | "neutral";
  hint: string;
};

export type AlertStateInput = {
  lifecycle: string | null | undefined;
  /** Server warnings; any error-severity warning outranks the other states. */
  hasErrorWarning?: boolean;
  hasWarning?: boolean;
  /** When did the platform last evaluate this alert, if ever. */
  lastEvaluatedAt?: string | null;
  quote?: MarketQuote | undefined;
  /** True when the price is already past the level a crossing rule waits for. */
  alreadyBeyond?: boolean;
};

export function describeAlertState(input: AlertStateInput): AlertStateView {
  const lifecycle = String(input.lifecycle ?? "").toLowerCase();

  if (lifecycle === "archived") {
    return {
      state: "archived",
      label: "Archived",
      tone: "neutral",
      hint: "Archived alerts are not evaluated.",
    };
  }
  if (input.hasErrorWarning) {
    return {
      state: "needs-attention",
      label: "Needs attention",
      tone: "danger",
      hint: "This alert cannot fire as defined. Open it to see what to fix.",
    };
  }
  if (lifecycle === "draft") {
    return {
      state: "draft",
      label: "Draft",
      tone: "neutral",
      hint: "A draft is stored but nothing is evaluating it yet.",
    };
  }
  if (lifecycle === "paused") {
    return {
      state: "paused",
      label: "Paused",
      tone: "warning",
      hint: "Paused alerts keep their definition and stop being evaluated.",
    };
  }

  const freshness = input.quote?.freshness;
  if (freshness === "MARKET CLOSED") {
    return {
      state: "market-closed",
      label: "Market closed",
      tone: "neutral",
      hint: "The exchange for this instrument is not trading; the alert resumes with the session.",
    };
  }
  if (freshness === "STALE") {
    return {
      state: "stale",
      label: "Market data stale",
      tone: "warning",
      hint: "The last price is old, so this alert may not be seeing the market.",
    };
  }
  if (input.alreadyBeyond) {
    return {
      state: "waiting-reset",
      label: "Waiting to reset",
      tone: "warning",
      hint: "The price is already past the level, so the alert waits to cross it again.",
    };
  }
  if (!input.lastEvaluatedAt) {
    return {
      state: "waiting-first-crossing",
      label: "Waiting for the first crossing",
      tone: "neutral",
      hint: "Activated recently; nothing has been evaluated yet, and activation itself is silent.",
    };
  }
  if (freshness === "LIVE" || freshness === "DELAYED") {
    return {
      state: "watching",
      label: "Watching",
      tone: "positive",
      hint: input.hasWarning
        ? "Watching, with a warning worth reading."
        : "Watching the market and evaluating on every update.",
    };
  }
  return {
    state: "waiting-first-crossing",
    label: "Waiting",
    tone: "neutral",
    hint: "The alert is active; there is no live price for it right now.",
  };
}

/**
 * When the platform last evaluated this alert.
 *
 * The age the server already computed is preferred, so the row needs no clock of
 * its own (reading `Date.now()` during render is impure and would make the row
 * differ between renders); when only a timestamp is available the absolute time
 * is shown instead of inventing an age.
 */
export function describeLastChecked(
  ageSeconds: number | null | undefined,
  lastEvaluatedAt?: string | null,
): string {
  if (typeof ageSeconds === "number" && Number.isFinite(ageSeconds)) {
    const seconds = Math.max(0, Math.round(ageSeconds));
    if (seconds < 60) return `checked ${seconds}s ago`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `checked ${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `checked ${hours}h ago`;
    return `checked ${Math.round(hours / 24)}d ago`;
  }
  if (!lastEvaluatedAt) return "not evaluated yet";
  const parsed = Date.parse(lastEvaluatedAt);
  if (Number.isNaN(parsed)) return "not evaluated yet";
  return `checked at ${new Date(parsed).toLocaleTimeString()}`;
}

/**
 * The one-line description of a multi-instrument or universe alert.
 *
 * One price cannot speak for a whole universe, so these alerts report what they
 * cover and how fresh the feed is instead of a number.
 */
export function describeCoverage(
  instruments: string[],
  hasUniverse: boolean,
  quotes: Array<MarketQuote | undefined>,
): string {
  const count = instruments.length;
  if (hasUniverse || count === 0) {
    return count > 0
      ? `universe · ${count} member price${count === 1 ? "" : "s"} streaming`
      : "universe · members resolve when the scan runs";
  }
  const live = quotes.filter((quote) => quote?.freshness === "LIVE").length;
  const fresh = quotes.filter((quote) => quote && quote.freshness !== "NO DATA").length;
  if (fresh === 0) return `${count} instruments · no price data yet`;
  return `${count} instruments · ${fresh} streaming${live > 0 ? ` (${live} live)` : ""}`;
}
