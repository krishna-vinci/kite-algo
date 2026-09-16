"use client";

/**
 * Background validation and preview for the alert editor.
 *
 * The operator never presses Validate and never pastes sample data. This hook
 * watches the definition they are building and answers, on a debounce:
 *
 * * is it complete enough to be worth validating at all (an obviously incomplete
 *   draft is skipped, not sent and rejected);
 * * what does the server say about it (server authority — this hook never
 *   decides validity itself);
 * * what would happen right now, using the LIVE price as the sample, expressed in
 *   a sentence rather than as a result object.
 *
 * Two properties matter for correctness:
 *
 * * a superseded request can never apply its answer (aborted *and* guarded by the
 *   effect's generation), so fast typing cannot show a stale verdict;
 * * an outage degrades to "unavailable" and leaves the draft alone — a failed
 *   validation is not a failed edit.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { previewAlertsWorkflow, validateAlertsWorkflow } from "@/features/alerts/api";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";
import type { AlertsIssue, AlertsPreviewResponse } from "@/features/alerts/types";

export type ValidationState =
  | "incomplete"
  | "checking"
  | "ready"
  | "invalid"
  | "unavailable"
  | "crossed"
  | "no-data";

/** Which part of the page an issue belongs to. */
export type IssueSection = "instrument" | "rule" | "evaluation" | "frequency" | "destinations" | "other";

export type ValidationResult = {
  state: ValidationState;
  issues: AlertsIssue[];
  bySection: Record<IssueSection, string[]>;
  /** One plain sentence about what the definition would do right now. */
  previewSentence: string | null;
  preview: AlertsPreviewResponse | null;
  error: string | null;
  /** Send it now (used after a failed save, when the operator needs certainty). */
  revalidate: () => void;
};

export function sectionForIssue(issue: AlertsIssue): IssueSection {
  const where = String(issue.where ?? "");
  const message = String(issue.message ?? "").toLowerCase();
  if (where.startsWith("document.name") || where.startsWith("document.version")) return "other";
  if (where.startsWith("document.session") || where.startsWith("document.instruments")) {
    return "instrument";
  }
  if (where.startsWith("document.universe")) return "instrument";
  if (where.startsWith("stages") && where.includes("clock")) return "evaluation";
  if (where.startsWith("stages")) return "rule";
  if (where.startsWith("alerts")) {
    return message.includes("channel") ? "destinations" : "frequency";
  }
  if (where.startsWith("document.screener")) return "destinations";
  return "other";
}

export function groupIssues(issues: AlertsIssue[]): Record<IssueSection, string[]> {
  const grouped: Record<IssueSection, string[]> = {
    instrument: [],
    rule: [],
    evaluation: [],
    frequency: [],
    destinations: [],
    other: [],
  };
  for (const issue of issues) {
    const section = sectionForIssue(issue);
    // Show the message, never the raw code: the operator cannot act on `bad_value`.
    const text = String(issue.message || issue.code || "This part of the definition is not valid.");
    if (!grouped[section].includes(text)) grouped[section].push(text);
  }
  return grouped;
}

/** A preview sample built from the live price, or null when there is none. */
export type PreviewQuote = {
  last_price: number | null;
  received_at: string | null;
  server_time: string | null;
  exchange_timestamp: string | null;
  ohlc: Record<string, number> | null;
};

export function observationFromQuote(
  instrumentKey: string,
  quote: PreviewQuote | null | undefined,
  clock: string,
): Record<string, unknown> | null {
  if (!quote || quote.last_price === null || quote.last_price === undefined) return null;
  const ts = quote.received_at ?? quote.server_time ?? quote.exchange_timestamp;
  if (!ts) return null;
  const ohlc = quote.ohlc ?? {};
  return {
    instrument_key: instrumentKey,
    ts,
    ltp: quote.last_price,
    open: typeof ohlc.open === "number" ? ohlc.open : quote.last_price,
    high: typeof ohlc.high === "number" ? ohlc.high : quote.last_price,
    low: typeof ohlc.low === "number" ? ohlc.low : quote.last_price,
    close: typeof ohlc.close === "number" ? ohlc.close : quote.last_price,
    volume: 0,
    epoch_id: "editor-preview",
    // The live price stands in for a completed candle only when the operator
    // chose candle evaluation; saying so keeps the preview honest.
    final: clock === "candle_close",
  };
}

/** Turn a preview result into one sentence a trader can act on. */
export function previewSentence(preview: AlertsPreviewResponse | null): string | null {
  if (!preview) return null;
  const fired = (preview.would_fire ?? []).length;
  if (fired > 0) {
    return `With the current price as the sample, this alert would fire (${fired} notification${fired === 1 ? "" : "s"}).`;
  }
  if (preview.evaluation === "dry_run_no_data") {
    return "Not enough data to preview yet; it will be checked against the live market once saved.";
  }
  const unknown = (preview.unknown_reasons ?? []).filter(Boolean);
  if (unknown.length > 0) {
    return `With the current price as the sample this would not fire yet, and ${unknown.length} input${unknown.length === 1 ? "" : "s"} could not be evaluated.`;
  }
  return "With the current price as the sample, this alert would not fire yet.";
}

export function useDefinitionValidation({
  scope,
  document,
  enabled,
  quote,
  instrumentKey,
  clock,
  crossed,
  debounceMs = 600,
}: {
  scope: string | null;
  /** The definition to validate; null when the draft is not complete enough. */
  document: Record<string, unknown> | null;
  enabled: boolean;
  quote: PreviewQuote | null | undefined;
  instrumentKey: string;
  clock: string;
  /** The client already knows the price is already past the level. */
  crossed: boolean;
  debounceMs?: number;
}): ValidationResult {
  const [state, setState] = useState<ValidationState>("incomplete");
  const [issues, setIssues] = useState<AlertsIssue[]>([]);
  const [preview, setPreview] = useState<AlertsPreviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const generation = useRef(0);

  // A stable dependency: the same definition must not re-validate because a new
  // object identity appeared.
  const documentKey = useMemo(
    () => (document && enabled ? JSON.stringify(document) : ""),
    [document, enabled],
  );

  useEffect(() => {
    const current = generation.current + 1;
    generation.current = current;
    if (!documentKey) {
      // Reset via the scheduler, not synchronously in the effect body, so React
      // sees one update pass rather than a cascading render.
      queueMicrotask(() => {
        if (generation.current !== current) return;
        setState("incomplete");
        setIssues([]);
        setPreview(null);
        setError(null);
      });
      return;
    }
    const controller = new AbortController();
    const parsed = JSON.parse(documentKey) as Record<string, unknown>;
    const observation = observationFromQuote(instrumentKey, quote, clock);
    const timer = setTimeout(() => {
      setState("checking");
      void (async () => {
        try {
          const [validation, previewResponse] = await Promise.all([
            validateAlertsWorkflow({ document: parsed }, scope, { signal: controller.signal }),
            previewAlertsWorkflow(
              {
                document: parsed,
                observations: observation ? [observation] : undefined,
              },
              scope,
              { signal: controller.signal },
            ),
          ]);
          if (controller.signal.aborted || generation.current !== current) return;
          const errors = (validation.issues ?? []).filter(
            (issue) => String(issue.severity ?? "error") !== "warning",
          );
          setIssues(errors);
          setPreview(previewResponse);
          setError(null);
          if (errors.length > 0) {
            setState("invalid");
          } else if (crossed) {
            setState("crossed");
          } else if (observation === null && clock !== "candle_close") {
            setState("no-data");
          } else {
            setState("ready");
          }
        } catch (caught) {
          if (controller.signal.aborted || generation.current !== current) return;
          // An outage is reported as an outage: the draft is untouched and the
          // operator can still save (the server validates again on save).
          setState("unavailable");
          setError(alertsErrorMessage(caught, "the validation service did not answer"));
        }
      })();
    }, debounceMs);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [documentKey, scope, debounceMs, instrumentKey, clock, crossed, quote, nonce]);

  const revalidate = useCallback(() => setNonce((value) => value + 1), []);
  const bySection = useMemo(() => groupIssues(issues), [issues]);
  const sentence = useMemo(() => previewSentence(preview), [preview]);

  return { state, issues, bySection, previewSentence: sentence, preview, error, revalidate };
}
