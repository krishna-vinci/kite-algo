/**
 * Plain-language reading of a screener run.
 *
 * The operator question is "did the scan work, and did nothing match, or did it
 * have nothing to work with?" Those are different answers and the run row must
 * never leave them ambiguous: a run that evaluated zero members because their
 * candle history was never fetched must not read like "no instruments matched".
 */

export type ScreenerRunLike = {
  status: string;
  coverage?: Record<string, unknown> | null;
  failure_reason?: string | null;
  data_freshness?: Record<string, unknown> | null;
};

export type RunReading = {
  /** One sentence for the operator. */
  summary: string;
  /** True when the limiting factor was missing/insufficient candle data. */
  dataLimited: boolean;
  /** Whether results are usable as a ranked list. */
  ranked: boolean;
};

function numberField(source: Record<string, unknown> | null | undefined, key: string): number | null {
  const value = source?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function warmingMembers(run: ScreenerRunLike): { warming: number; unavailable: number } {
  const block = run.coverage?.candle_warming;
  if (!block || typeof block !== "object") return { warming: 0, unavailable: 0 };
  const record = block as Record<string, unknown>;
  const members = Array.isArray(record.members) ? record.members : [];
  let warming = 0;
  let unavailable = 0;
  for (const entry of members) {
    if (!entry || typeof entry !== "object") continue;
    const status = String((entry as Record<string, unknown>).status ?? "");
    if (status === "unavailable" || status === "expired") unavailable += 1;
    if (status === "skipped") warming += 1;
  }
  if (warming === 0 && typeof record.skipped === "number") warming = Number(record.skipped);
  if (unavailable === 0 && typeof record.unavailable === "number") {
    unavailable = Number(record.unavailable);
  }
  return { warming, unavailable };
}

export function readScreenerRun(run: ScreenerRunLike): RunReading {
  const expected = numberField(run.coverage, "expected") ?? 0;
  const evaluated = numberField(run.coverage, "evaluated") ?? 0;
  const qualifying = numberField(run.coverage, "qualifying") ?? 0;
  const unavailable = numberField(run.coverage, "unavailable") ?? 0;
  const unknown = numberField(run.coverage, "unknown_conditions") ?? 0;
  const { warming, unavailable: warmingUnavailable } = warmingMembers(run);
  const missingData =
    expected > 0 && evaluated === 0 && (unavailable > 0 || warming > 0 || warmingUnavailable > 0);

  if (run.status === "running") {
    return {
      summary: "Scanning… results appear when the run finishes.",
      dataLimited: false,
      ranked: false,
    };
  }

  if (run.status === "failed") {
    if (missingData) {
      const symbols = warming > 0 ? `${warming} symbol(s) still warming` : `${unavailable} symbol(s) without daily candles`;
      return {
        summary:
          `No candle data to scan yet — ${symbols}. This is missing data, not a market result. ` +
          "Fetch candle history, then run again.",
        dataLimited: true,
        ranked: false,
      };
    }
    return {
      summary: `The scan failed${run.failure_reason ? `: ${run.failure_reason}` : ""}. Nothing was replaced.`,
      dataLimited: false,
      ranked: false,
    };
  }

  if (run.status === "partial") {
    return {
      summary:
        `Scanned ${evaluated} of ${expected} symbols; ${unavailable + unknown} could not be scored ` +
        `(missing or insufficient data) and ${qualifying} qualified. Partial results are not a ` +
        "complete membership replacement.",
      dataLimited: unavailable > 0,
      ranked: qualifying > 0,
    };
  }

  if (qualifying === 0) {
    return {
      summary: `Scanned ${evaluated} of ${expected} symbols with complete data; none met the qualification.`,
      dataLimited: false,
      ranked: false,
    };
  }

  return {
    summary: `Scanned ${evaluated} of ${expected} symbols; ${qualifying} qualified and were ranked.`,
    dataLimited: false,
    ranked: true,
  };
}
