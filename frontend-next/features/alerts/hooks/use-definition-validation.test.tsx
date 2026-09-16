/**
 * Background validation: the editor must never show a stale verdict, must never
 * validate a definition that is still obviously incomplete, and must survive the
 * validation service being down without touching the operator's work.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/features/alerts/api", () => ({
  validateAlertsWorkflow: vi.fn(),
  previewAlertsWorkflow: vi.fn(),
}));

import { previewAlertsWorkflow, validateAlertsWorkflow } from "@/features/alerts/api";
import {
  observationFromQuote,
  useDefinitionValidation,
} from "@/features/alerts/hooks/use-definition-validation";

const QUOTE = {
  last_price: 124_860,
  received_at: "2026-09-15T11:00:01+00:00",
  server_time: "2026-09-15T11:00:01+00:00",
  exchange_timestamp: "2026-09-15T11:00:00+00:00",
  ohlc: { open: 124_000, high: 125_000, low: 123_500, close: 124_400 } as Record<string, number>,
};

function okValidation(issues: unknown[] = []) {
  return { ok: true, issues } as never;
}

function okPreview(overrides: Record<string, unknown> = {}) {
  return { ok: true, issues: [], would_fire: [], evaluation: "dry_run", ...overrides } as never;
}

function setup(overrides: Partial<Parameters<typeof useDefinitionValidation>[0]> = {}) {
  const props = {
    scope: "app:admin",
    document: { version: 1, name: "n", session: "mcx_commodity" },
    enabled: true,
    quote: QUOTE,
    instrumentKey: "MCX:GOLD26DECFUT",
    clock: "ltp",
    crossed: false,
    debounceMs: 10,
    ...overrides,
  };
  return renderHook((next: Partial<Parameters<typeof useDefinitionValidation>[0]>) =>
    useDefinitionValidation({ ...props, ...next }), { initialProps: {} });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(validateAlertsWorkflow).mockResolvedValue(okValidation());
  vi.mocked(previewAlertsWorkflow).mockResolvedValue(okPreview());
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useDefinitionValidation", () => {
  it("does not validate a definition that is not complete enough to mean anything", async () => {
    const { result } = setup({ document: null, enabled: false });
    await waitFor(() => expect(result.current.state).toBe("incomplete"));
    expect(validateAlertsWorkflow).not.toHaveBeenCalled();
    expect(previewAlertsWorkflow).not.toHaveBeenCalled();
  });

  it("sends the live price as the preview sample", async () => {
    setup();
    await waitFor(() => expect(previewAlertsWorkflow).toHaveBeenCalled());
    const payload = vi.mocked(previewAlertsWorkflow).mock.calls[0][0] as {
      observations?: Array<Record<string, unknown>>;
    };
    expect(payload.observations?.[0]).toMatchObject({
      instrument_key: "MCX:GOLD26DECFUT",
      ltp: 124_860,
      close: 124_400,
    });
  });

  it("leaves out the sample entirely when there is no live price", async () => {
    setup({ quote: undefined });
    await waitFor(() => expect(previewAlertsWorkflow).toHaveBeenCalled());
    const payload = vi.mocked(previewAlertsWorkflow).mock.calls[0][0] as { observations?: unknown };
    expect(payload.observations).toBeUndefined();
  });

  it("reports an invalid definition and says which section owns each issue", async () => {
    vi.mocked(validateAlertsWorkflow).mockResolvedValue(
      okValidation([
        { where: "stages.px.conditions", code: "bad_operand", message: "value must be a number", severity: "error" },
      ]),
    );
    const { result } = setup();
    await waitFor(() => expect(result.current.state).toBe("invalid"));
    expect(result.current.bySection.rule).toEqual(["value must be a number"]);
    expect(JSON.stringify(result.current.bySection)).not.toContain("bad_operand");
  });

  it("says the alert is already past its level instead of pretending it is ready", async () => {
    const { result } = setup({ crossed: true });
    await waitFor(() => expect(result.current.state).toBe("crossed"));
  });

  it("degrades to unavailable when the service is down, and keeps the draft state", async () => {
    vi.mocked(validateAlertsWorkflow).mockRejectedValue(new Error("503"));
    vi.mocked(previewAlertsWorkflow).mockRejectedValue(new Error("503"));
    const { result } = setup();
    await waitFor(() => expect(result.current.state).toBe("unavailable"));
    expect(result.current.error).toBeTruthy();
    expect(result.current.issues).toEqual([]);
  });

  it("never applies a superseded response", async () => {
    let resolveFirst: (value: never) => void = () => undefined;
    const first = new Promise((resolve) => {
      resolveFirst = resolve as (value: never) => void;
    });
    vi.mocked(validateAlertsWorkflow)
      .mockReturnValueOnce(first as never)
      .mockResolvedValue(
        okValidation([
          { where: "stages.px.conditions", code: "bad_operand", message: "second answer", severity: "error" },
        ]),
      );

    const { result, rerender } = setup();
    // let the first request actually leave, then change the definition while it
    // is still in flight
    await waitFor(() => expect(vi.mocked(validateAlertsWorkflow).mock.calls.length).toBe(1));
    rerender({ document: { version: 1, name: "n", session: "mcx_commodity", timeframe: "day" } });
    await waitFor(() => expect(result.current.state).toBe("invalid"));
    expect(result.current.bySection.rule).toEqual(["second answer"]);

    // the stale response arrives late and must be ignored
    resolveFirst(
      okValidation([
        { where: "document.session", code: "session_mismatch", message: "stale answer", severity: "error" },
      ]) as never,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(result.current.bySection.rule).toEqual(["second answer"]);
    expect(result.current.bySection.instrument).toEqual([]);
  });

  it("reports a preview that would fire in words", async () => {
    vi.mocked(previewAlertsWorkflow).mockResolvedValue(
      okPreview({ would_fire: [{ alert_id: "a1" }] }),
    );
    const { result } = setup();
    await waitFor(() => expect(result.current.state).toBe("ready"));
    expect(result.current.previewSentence).toMatch(/would fire/);
  });

  it("says there is no market data yet rather than claiming readiness", async () => {
    const { result } = setup({ quote: undefined, clock: "ltp" });
    await waitFor(() => expect(result.current.state).toBe("no-data"));
  });
});

describe("observationFromQuote", () => {
  it("uses the receipt time as event time and marks candle mode as final", () => {
    const tick = observationFromQuote("MCX:GOLD26DECFUT", QUOTE, "ltp");
    expect(tick?.ts).toBe("2026-09-15T11:00:01+00:00");
    expect(tick?.final).toBe(false);

    const candle = observationFromQuote("MCX:GOLD26DECFUT", QUOTE, "candle_close");
    expect(candle?.final).toBe(true);
  });

  it("returns nothing without a price or a timestamp", () => {
    expect(observationFromQuote("X:Y", { ...QUOTE, last_price: null }, "ltp")).toBeNull();
    expect(
      observationFromQuote(
        "X:Y",
        { ...QUOTE, received_at: null, server_time: null, exchange_timestamp: null },
        "ltp",
      ),
    ).toBeNull();
  });
});
