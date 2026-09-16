/**
 * The unified authoring page: one editor for create and edit.
 *
 * These are component tests (jsdom + Testing Library), so what they prove is the
 * contract the operator sees: the common path is on one page, the session is
 * inferred rather than asked for, the timeframe only appears when it matters, the
 * target is explained against the live price, a partial save is reported
 * honestly, and a definition the form cannot represent lands in Code view on the
 * same page instead of a second product.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { AlertDraft } from "@/features/alerts/lib/authoring";
import { emptyDraft } from "@/features/alerts/lib/authoring";

vi.mock("@/features/alerts/api", () => ({
  fetchAlertsCapabilities: vi.fn(),
  fetchAlertsChannels: vi.fn(),
  fetchAlertsUniverse: vi.fn(),
  createAlertsWorkflow: vi.fn(),
  patchAlertsWorkflow: vi.fn(),
  activateAlertsWorkflow: vi.fn(),
  validateAlertsWorkflow: vi.fn(),
  previewAlertsWorkflow: vi.fn(),
  fetchAlertsWorkflow: vi.fn(),
}));

vi.mock("@/features/alerts/components/advanced-definition-editor", () => ({
  AdvancedDefinitionEditor: ({ reason }: { reason?: string }) => (
    <div data-testid="code-view">code view {reason ?? ""}</div>
  ),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

// A stub stream keeps the live-price hooks deterministic without a socket.
const QUOTE = {
  instrument_key: "MCX:GOLD26DECFUT",
  broker_token: 111,
  last_price: 124_860,
  change_absolute: 400,
  change_percent: 0.32,
  ohlc: null,
  exchange_timestamp: "2026-09-15T11:00:00+00:00",
  received_at: "2026-09-15T11:00:01+00:00",
  server_time: "2026-09-15T11:00:01+00:00",
  age_ms: 1200,
  session_state: "open" as const,
  freshness: "LIVE" as const,
  origin: "tick" as const,
};

vi.mock("@/features/alerts/lib/market-stream", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/alerts/lib/market-stream")>();
  class StubStream {
    static instances: StubStream[] = [];
    registrations: string[] = [];
    constructor() {
      StubStream.instances.push(this);
    }
    register(key: string | null | undefined) {
      if (key) this.registrations.push(key);
      return () => undefined;
    }
    subscribeQuote(key: string, listener: (quote: unknown) => void) {
      if (key === QUOTE.instrument_key) listener(QUOTE);
      return () => undefined;
    }
    subscribeStatus() {
      return () => undefined;
    }
    getQuote(key?: string | null) {
      return key === QUOTE.instrument_key ? QUOTE : undefined;
    }
    getStatus() {
      return { state: "live" as const, runtime: null, error: null, frames: 1, reconnects: 0 };
    }
    destroy() {
      return undefined;
    }
  }
  return { ...actual, AlertsMarketStream: StubStream };
});

import {
  activateAlertsWorkflow,
  createAlertsWorkflow,
  fetchAlertsCapabilities,
  fetchAlertsChannels,
  fetchAlertsWorkflow,
  patchAlertsWorkflow,
  previewAlertsWorkflow,
  validateAlertsWorkflow,
} from "@/features/alerts/api";
import { AlertsMarketStreamProvider } from "@/features/alerts/hooks/use-market-stream";
import { UnifiedAlertEditor, type UnifiedEditorMode } from "./unified-alert-editor";

function renderWithQuery(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AlertsMarketStreamProvider scope="app:admin">{ui}</AlertsMarketStreamProvider>
    </QueryClientProvider>,
  );
}

function capabilities() {
  return {
    ok: true,
    capabilities: {
      operators: { crosses_above: "crosses above", crosses_below: "crosses below", gt: "is above", lt: "is below", rises_pct: "rises by", falls_pct: "falls by" },
      fields: ["ltp", "close", "change_pct"],
      fundamentals_fields: [],
      fundamentals_source: { table: "t", description: "", freshness_keys: [], columns: {} },
      clocks: { ltp: { label: "ltp" }, candle_close: { label: "candle_close" } },
      clock_aliases: {},
      triggers: ["once", "on_transition"],
      timeframes: ["minute", "5minute", "15minute", "day"],
      sessions: ["nse_equity", "mcx_commodity"],
      session_exchanges: { nse_equity: ["NSE"], mcx_commodity: ["MCX"] },
      features: {
        ema: {
          params: { period: { min: 1, max: 500 } },
          defaults: { period: 9 },
          inputs: ["close"],
          outputs: ["ema"],
        },
      },
      arithmetic: [],
      limits: { max_instruments: 100 },
      stage_types: ["signal", "filter"],
      pairs: {},
      pair_lookback_bounds: [1, 100],
      pair_max_skew_bars: 5,
      breadth_modes: {},
      breadth_semantics: "",
      universe_ref_kinds: ["universe"],
      universe_index_source_lists: [],
      universe_source_kinds: ["explicit"],
      screener: {
        attachment_triggers: [],
        attachment_hysteresis: {},
        schedule_calendars: ["nse_equity"],
        schedule_note: "",
        stored_data_fields: [],
        run_statuses: [],
        tie_break: "",
      },
    },
  } as never;
}

function draftWithInstrument(overrides: Partial<AlertDraft> = {}): AlertDraft {
  const base = emptyDraft();
  return {
    ...base,
    instruments: ["MCX:GOLD26DECFUT"],
    session: "mcx_commodity",
    clock: "ltp",
    conditions: [
      { left: { kind: "field", name: "ltp" }, op: "crosses_above", right: { kind: "constant", value: 125_000 } },
    ],
    ...overrides,
  };
}

function indicatorRuleDraft(): AlertDraft {
  return draftWithInstrument({
    conditions: [
      {
        left: { kind: "indicator", name: "ema", period: 9 },
        op: "crosses_above",
        right: { kind: "indicator", name: "ema", period: 19 },
      },
    ],
  });
}

beforeAll(() => {
  // Radix Select scrolls its highlighted option into view; jsdom has no layout.
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => undefined);
});

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchAlertsCapabilities).mockResolvedValue(capabilities());
  vi.mocked(fetchAlertsChannels).mockResolvedValue({
    ok: true,
    channels: [
      { channel_id: "c1", name: "telegram_ops", provider: "telegram", enabled: true, secret_env: "T" },
    ],
  } as never);
});

describe("UnifiedAlertEditor", () => {
  it("renders the create page with the rule, frequency and destination on one page", async () => {
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );

    expect(await screen.findByText("New alert")).toBeTruthy();
    // The rule is one editor: row 1 carries the operator and the level.
    expect(screen.getByLabelText("Operator")).toBeTruthy();
    expect(screen.getByLabelText("condition 1 right value")).toBeTruthy();
    expect(screen.getByLabelText("Evaluation")).toBeTruthy();
    expect(screen.getByText("When should we notify you?")).toBeTruthy();
    expect(screen.getByText("Notify via")).toBeTruthy();
    expect(screen.getByText("Create and activate")).toBeTruthy();
    expect(screen.getByText("Save draft")).toBeTruthy();
    // no session dropdown, no separate validation step
    expect(screen.queryByLabelText(/session/i)).toBeNull();
    expect(screen.queryByText(/^Validate$/)).toBeNull();
  });

  it("shows the live price with freshness and exchange/receipt timestamps", async () => {
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    // The price card exists twice on purpose: the inline strip (small screens)
    // and the rail's Live market card both render it.
    expect((await screen.findAllByText("₹1,24,860.00")).length).toBe(2);
    expect(screen.getAllByText("LIVE").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/updated 1s ago/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/exchange .*received/).length).toBe(2);
  });

  it("explains the distance to the target and offers shortcuts from the live price", async () => {
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    // 124,860 → 125,000 is ₹140 above, rendered by the rule row and by the
    // rail's price ladder.
    expect((await screen.findAllByText(/₹140\.00 above the current price/)).length).toBeGreaterThan(0);
    expect(screen.getByText("Use current price")).toBeTruthy();
    fireEvent.click(screen.getByText("+1%"));
    await waitFor(() =>
      expect((screen.getByLabelText("condition 1 right value") as HTMLInputElement).value).toBe(
        "126108.6",
      ),
    );
  });

  it("says plainly when the price is already past a crossing level", async () => {
    renderWithQuery(
      <UnifiedAlertEditor
        scope="app:admin"
        mode={{ kind: "create" }}
        initialDraft={draftWithInstrument({
          conditions: [
            { left: { kind: "field", name: "ltp" }, op: "crosses_above", right: { kind: "constant", value: 124_000 } },
          ],
        })}
      />,
    );
    // The preview sentence surfaces in the rail's insight card and (prefixed by
    // the evaluation label) in the save bar.
    expect((await screen.findAllByText(/Price is already above/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/wait for the price to move below the target/).length).toBeGreaterThan(0);
  });

  it("shows the timeframe only when the evaluation needs one", async () => {
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    await screen.findByText("New alert");
    expect(screen.queryByLabelText("Measured over")).toBeNull();

    fireEvent.click(screen.getByLabelText("Evaluation"));
    fireEvent.click(await screen.findByText("Completed candle"));
    await waitFor(() => expect(screen.getByLabelText("Measured over")).toBeTruthy());
    // human labels, not backend codes
    expect(screen.getByText("15 minutes")).toBeTruthy();
    expect(screen.queryByText("15minute")).toBeNull();
  });

  it("does not offer percentage-offset shortcuts without a live price for the instrument", async () => {
    renderWithQuery(
      <UnifiedAlertEditor
        scope="app:admin"
        mode={{ kind: "create" }}
        initialDraft={draftWithInstrument({ instruments: [] })}
      />,
    );
    await screen.findByText("New alert");
    expect(screen.queryByText("+1%")).toBeNull();
  });

  it("creates an indicator-versus-indicator rule instead of demanding a value", async () => {
    // Regression: the top row owned conditions[0] and could only express a
    // level, so a rule comparing two indicators could never satisfy "enter the
    // value this alert compares against" and the alert could not be created at
    // all.
    vi.mocked(validateAlertsWorkflow).mockResolvedValue({ ok: true, issues: [] } as never);
    vi.mocked(previewAlertsWorkflow).mockResolvedValue({ ok: true } as never);

    renderWithQuery(
      <UnifiedAlertEditor
        scope="app:admin"
        mode={{ kind: "create" }}
        initialDraft={indicatorRuleDraft()}
      />,
    );

    expect(await screen.findByText("New alert")).toBeTruthy();
    expect(screen.queryByText(/Enter the value/)).toBeNull();
    expect(screen.queryByText(/choose what it compares against/)).toBeNull();
    expect((screen.getByText("Save draft").closest("button") as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect(
      (screen.getByText("Create and activate").closest("button") as HTMLButtonElement).disabled,
    ).toBe(false);

    // The definition the server is asked about carries the indicator operand.
    await waitFor(() => expect(validateAlertsWorkflow).toHaveBeenCalled(), { timeout: 3000 });
    const payload = vi.mocked(validateAlertsWorkflow).mock.calls[0][0] as {
      document: { stages: Array<{ conditions: { all: Array<{ right: unknown; op: string }> } }> };
    };
    expect(payload.document.stages[0].conditions.all[0]).toMatchObject({
      op: "crosses_above",
      right: { indicator: "ema", period: 19 },
    });
  });

  it("keeps a brand-new alert unready while the level is the untouched placeholder", async () => {
    renderWithQuery(
      <UnifiedAlertEditor
        scope="app:admin"
        mode={{ kind: "create" }}
        initialDraft={draftWithInstrument({
          conditions: [
            {
              left: { kind: "field", name: "ltp" },
              op: "crosses_above",
              right: { kind: "constant", value: 0 },
            },
          ],
        })}
      />,
    );

    expect(await screen.findByText("New alert")).toBeTruthy();
    expect(screen.getByText(/Condition 1: choose what it compares against/)).toBeTruthy();
    expect((screen.getByText("Save draft").closest("button") as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(
      (screen.getByText("Create and activate").closest("button") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("moves the clock and the left side when the operator becomes a percentage move", async () => {
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    await screen.findByText("New alert");
    expect(screen.queryByLabelText("Measured over")).toBeNull();

    fireEvent.click(screen.getByLabelText("Operator"));
    fireEvent.click(await screen.findByText("rises by %"));

    // A percentage move is measured between completed candles and compares the
    // close, so both have to follow the operator.
    await waitFor(() =>
      expect(screen.getByLabelText("condition 1 left field").textContent).toContain("close"),
    );
    expect(screen.getByLabelText("Evaluation").textContent).toContain("Completed candle");
    expect(screen.getByLabelText("Measured over")).toBeTruthy();
  });

  it("reads the rule and the generated name in the operands' own words", async () => {
    renderWithQuery(
      <UnifiedAlertEditor
        scope="app:admin"
        mode={{ kind: "create" }}
        initialDraft={indicatorRuleDraft()}
      />,
    );

    const rail = (await screen.findByText("You are creating")).closest("section") as HTMLElement;
    expect(within(rail).getAllByText("EMA 9 crosses above EMA 19").length).toBeGreaterThan(0);
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe(
      "EMA 9 crosses above EMA 19",
    );
  });

  it("reports a saved draft when activation fails, and links to it", async () => {
    vi.mocked(createAlertsWorkflow).mockResolvedValue({ workflow_id: "wf-1", ok: true } as never);
    vi.mocked(activateAlertsWorkflow).mockRejectedValue(new Error("nope"));

    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    fireEvent.click(await screen.findByText("Create and activate"));

    expect(await screen.findByText(/Saved as a draft — activation failed/)).toBeTruthy();
    expect(screen.getByText(/Open the draft to retry activation/)).toBeTruthy();
  });

  it("saves the destination that is shown, including a preselected one", async () => {
    // Regression: the single enabled channel was rendered checked but never
    // written into the draft, so the created alert had NO destination while the
    // form looked complete.
    vi.mocked(createAlertsWorkflow).mockResolvedValue({ workflow_id: "wf-1", ok: true } as never);

    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    fireEvent.click(await screen.findByText("Save draft"));

    await waitFor(() => expect(createAlertsWorkflow).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(createAlertsWorkflow).mock.calls[0][0] as {
      document: { alerts: Array<{ channels: string[] }> };
    };
    expect(payload.document.alerts[0].channels).toEqual(["telegram_ops"]);
  });

  it("saves no destination when the operator clears the only one", async () => {
    vi.mocked(createAlertsWorkflow).mockResolvedValue({ workflow_id: "wf-1", ok: true } as never);
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    const checkbox = (await screen.findByLabelText(/telegram_ops/)) as HTMLInputElement;
    fireEvent.click(checkbox);
    // an explicit "none" is a real choice, not a reason to re-derive the default
    await waitFor(() => expect(screen.getByText(/Choose at least one destination/)).toBeTruthy());
    expect((screen.getByText("Save draft").closest("button") as HTMLButtonElement).disabled).toBe(true);
  });

  it("reuses one idempotency key when a save is retried", async () => {
    vi.mocked(createAlertsWorkflow).mockResolvedValue({ workflow_id: "wf-1", ok: true } as never);
    vi.mocked(activateAlertsWorkflow).mockRejectedValue(new Error("nope"));

    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    fireEvent.click(await screen.findByText("Save draft"));
    await waitFor(() => expect(createAlertsWorkflow).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText("Save draft"));
    await waitFor(() => expect(createAlertsWorkflow).toHaveBeenCalledTimes(2));

    const first = vi.mocked(createAlertsWorkflow).mock.calls[0][0] as { idempotency_key?: string };
    const second = vi.mocked(createAlertsWorkflow).mock.calls[1][0] as { idempotency_key?: string };
    expect(first.idempotency_key).toBeTruthy();
    expect(first.idempotency_key).toBe(second.idempotency_key);
  });

  it("keeps the entered values when the server rejects a save", async () => {
    vi.mocked(createAlertsWorkflow).mockRejectedValue(new Error("boom"));
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    fireEvent.change(await screen.findByLabelText("condition 1 right value"), {
      target: { value: "9999" },
    });
    fireEvent.click(screen.getByText("Create and activate"));

    expect(await screen.findByText("Could not save")).toBeTruthy();
    expect((screen.getByLabelText("condition 1 right value") as HTMLInputElement).value).toBe("9999");
  });

  it("renders the edit page with the stored definition and the edit actions", async () => {
    const mode: UnifiedEditorMode = {
      kind: "edit",
      workflowId: "wf-1",
      expectedRevision: 3,
      baseDocument: { version: 1 },
      workflowName: "GOLD breakout",
      yaml: null,
    };
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={mode} initialDraft={draftWithInstrument()} />,
    );
    // The header trail names where you are: Alerts / {name} / Edit.
    expect(await screen.findByText("GOLD breakout")).toBeTruthy();
    expect(screen.getByText("Edit")).toBeTruthy();
    expect(screen.getByText("Save changes")).toBeTruthy();
    expect(screen.getByText("Save and activate latest")).toBeTruthy();

    vi.mocked(patchAlertsWorkflow).mockResolvedValue({ ok: true, revision: 4 } as never);
    vi.mocked(activateAlertsWorkflow).mockResolvedValue({ ok: true } as never);
    fireEvent.click(screen.getByText("Save and activate latest"));
    await waitFor(() => expect(activateAlertsWorkflow).toHaveBeenCalledWith("wf-1", { revision: 4, scope: "app:admin" }));
  });

  it("opens Code view on the same page when the definition cannot be represented", async () => {
    const mode: UnifiedEditorMode = {
      kind: "edit",
      workflowId: "wf-1",
      expectedRevision: 2,
      baseDocument: { version: 1, stages: [{ id: "px", sequence: { within_bars: 5 } }] },
      workflowName: "Sequence alert",
      yaml: null,
    };
    renderWithQuery(
      <UnifiedAlertEditor
        scope="app:admin"
        mode={mode}
        initialDraft={draftWithInstrument()}
        conversionError="this definition uses a sequence"
      />,
    );
    await screen.findByTestId("code-view");
    expect(screen.getAllByTestId("code-view")).toHaveLength(1);
    expect(screen.getByText(/Editing as text/)).toBeTruthy();
    // the reason is explained on the page (alert + the editor's own props)
    expect(screen.getAllByText(/this definition uses a sequence/).length).toBeGreaterThan(0);
  });

  it("offers a lossless text view while creating", async () => {
    renderWithQuery(
      <UnifiedAlertEditor
        scope="app:admin"
        mode={{ kind: "create" }}
        initialDraft={draftWithInstrument()}
        conversionError="this definition uses a sequence"
      />,
    );
    await screen.findByText("New alert");
    const editor = screen.getByLabelText("Alert definition") as HTMLTextAreaElement;
    expect(editor.value).toContain('"version": 1');
    expect(editor.value).toContain("MCX:GOLD26DECFUT");
  });

  it("surfaces a conflict without dropping the operator's work", async () => {
    vi.mocked(patchAlertsWorkflow).mockRejectedValue(
      Object.assign(new Error("conflict"), { status: 409, body: { detail: "revision moved" } }),
    );
    vi.mocked(fetchAlertsWorkflow).mockResolvedValue({ ok: true, latest_revision: { revision: 9 } } as never);

    const mode: UnifiedEditorMode = {
      kind: "edit",
      workflowId: "wf-1",
      expectedRevision: 3,
      baseDocument: { version: 1 },
      workflowName: "GOLD breakout",
      yaml: null,
    };
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={mode} initialDraft={draftWithInstrument()} />,
    );
    fireEvent.change(await screen.findByLabelText("condition 1 right value"), {
      target: { value: "130000" },
    });
    fireEvent.click(screen.getByText("Save changes"));

    expect(await screen.findByText(/changed while you were editing/)).toBeTruthy();
    expect((screen.getByLabelText("condition 1 right value") as HTMLInputElement).value).toBe(
      "130000",
    );
    expect(screen.getByText("Load the newer revision")).toBeTruthy();
  });

  it("renders the workspace: sticky rail with summary, and a compact frequency segmented control", async () => {
    renderWithQuery(
      <UnifiedAlertEditor scope="app:admin" mode={{ kind: "create" }} initialDraft={draftWithInstrument()} />,
    );
    expect(await screen.findByText("New alert")).toBeTruthy();
    // The rail exists with its three cards:
    expect(screen.getByText("Live market")).toBeTruthy();
    expect(screen.getByText("What this alert will do")).toBeTruthy();
    expect(screen.getByText("You are creating")).toBeTruthy();
    // Frequency is a segmented radiogroup, one row, hints collapsed to the selected one:
    const group = screen.getByRole("radiogroup", { name: "Notification frequency" });
    expect(group.querySelectorAll("input[type=radio]").length).toBe(3);
    expect(screen.getAllByRole("radio", { name: "Once when it happens" }).length).toBeGreaterThan(0);
  });
});

describe("existing-alert actions", () => {
  it("reads the stored frequency back into a choice", async () => {
    const { frequencyOf } = await import("@/features/alerts/lib/plain-language");
    expect(frequencyOf({ trigger: "once", reminder_interval_s: null })).toBe("once");
    expect(frequencyOf({ trigger: "on_transition", reminder_interval_s: null })).toBe("repeated");
    expect(frequencyOf({ trigger: "on_transition", reminder_interval_s: 600 })).toBe("reminder");
  });

  it("reports what a frequency change actually did", async () => {
    const { describeFrequencyResult } = await import("@/features/alerts/components/alert-actions");
    expect(
      describeFrequencyResult({ changed: true, trigger: "once", activated: true, revision: 4 }),
    ).toContain("put in force");
    expect(
      describeFrequencyResult({ changed: true, trigger: "once", activated: false, revision: 4 }),
    ).toContain("Activate it");
    expect(
      describeFrequencyResult({
        changed: false,
        trigger: "once",
        activated: true,
        revision: 1,
        note: "the alert already notifies this way",
      }),
    ).toBe("the alert already notifies this way");
  });
});
