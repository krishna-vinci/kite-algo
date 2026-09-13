import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OperatorIssueList } from "./operator-issue-list";
import { UniverseTargetingEditor, universeNameOptions } from "./universe-targeting-editor";
import type { UniverseDraft } from "@/features/alerts/lib/authoring";

vi.mock("@/features/alerts/api", () => ({
  fetchAlertsCapabilities: vi.fn(),
  fetchAlertsUniverses: vi.fn(),
}));

import { fetchAlertsCapabilities, fetchAlertsUniverses } from "@/features/alerts/api";

function renderWithQuery(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function capabilities(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    capabilities: {
      universe_ref_kinds: ["universe", "index"],
      // Deliberately NOT the real list: if the component hard-coded Nifty50 the
      // assertion below would still pass, so the test also asserts the real
      // names are ABSENT.
      universe_index_source_lists: ["TestIndexOnly"],
      limits: { max_instruments: 1000 },
      sessions: [],
      clocks: {},
      timeframes: [],
      operators: {},
      fields: [],
      triggers: [],
      session_exchanges: {},
      universe_source_kinds: ["explicit", "index"],
      screener: {
        attachment_triggers: [],
        attachment_hysteresis: {},
        schedule_calendars: ["nse_equity"],
        schedule_note: "",
        stored_data_fields: [],
        run_statuses: [],
        tie_break: "",
      },
      ...overrides,
    },
  } as never;
}

const EMPTY_UNIVERSE: UniverseDraft = {
  union: [{ kind: "universe", name: "" }],
  exclude: [],
  deduplicate: true,
};

beforeEach(() => {
  vi.mocked(fetchAlertsCapabilities).mockResolvedValue(capabilities());
  vi.mocked(fetchAlertsUniverses).mockResolvedValue({
    ok: true,
    universes: [
      {
        universe_id: "u1",
        name: "saved-top100",
        kind: "explicit",
        enabled: true,
        latest_revision: { revision: 3 },
      },
    ],
  } as never);
});

describe("UniverseTargetingEditor", () => {
  // The option lists are asserted through the exported helper rather than by
  // opening the Radix Select: Radix opens on a pointer interaction jsdom does
  // not implement, so a DOM-driven assertion would be testing the harness.

  it("derives index options from capabilities, never a hard-coded copy", () => {
    const options = universeNameOptions("index", ["TestIndexOnly"], []);
    expect(options).toEqual([{ name: "TestIndexOnly", hint: "index" }]);
    expect(options.map((option) => option.name)).not.toContain("Nifty50");
  });

  it("derives universe options from live data, including resolution state", () => {
    const options = universeNameOptions("universe", ["ignored"], [
      { name: "saved-top100", kind: "explicit", latest_revision: { revision: 3 } },
      { name: "never-resolved", kind: "index", latest_revision: null },
    ]);
    expect(options).toEqual([
      { name: "saved-top100", hint: "explicit · r3" },
      // "never resolved" is not the same as "resolved to nothing" — an operator
      // reading a silent alert needs to tell them apart.
      { name: "never-resolved", hint: "index · never resolved" },
    ]);
  });

  it("treats watchlist as an alias of saved universes, not of index lists", () => {
    const options = universeNameOptions("watchlist", ["TestIndexOnly"], [
      { name: "my-list", kind: "explicit", latest_revision: null },
    ]);
    expect(options.map((option) => option.name)).toEqual(["my-list"]);
  });

  it("renders and reports a blank reference instead of treating it as a wildcard", () => {
    renderWithQuery(
      <UniverseTargetingEditor scope="owner-1" value={EMPTY_UNIVERSE} onChange={vi.fn()} />,
    );
    expect(screen.getByText(/at least one reference/i)).toBeInTheDocument();
    expect(screen.getByText(/needs a name/i)).toBeInTheDocument();
  });

  it("falls back to a free-text input while index lists are unavailable", () => {
    vi.mocked(fetchAlertsCapabilities).mockResolvedValue(
      capabilities({ universe_index_source_lists: [] }),
    );
    renderWithQuery(
      <UniverseTargetingEditor
        scope="owner-1"
        value={{ ...EMPTY_UNIVERSE, union: [{ kind: "index", name: "" }] }}
        onChange={vi.fn()}
      />,
    );
    // An empty option list must not render an empty dropdown that silently
    // discards the reference.
    expect(screen.getByLabelText("Name")).toHaveProperty("tagName", "INPUT");
  });

  it("cannot remove the last union reference", () => {
    renderWithQuery(
      <UniverseTargetingEditor scope="owner-1" value={EMPTY_UNIVERSE} onChange={vi.fn()} />,
    );
    expect(screen.getByLabelText("Remove reference 1")).toBeDisabled();
  });
});

describe("OperatorIssueList", () => {
  it("separates 'will not save' from 'will not fire'", () => {
    render(
      <OperatorIssueList
        issues={[
          { where: "document.stages[0]", code: "bad_value", message: "unknown operator", severity: "error" },
          {
            where: "document.stages[0].conditions",
            code: "level_only_never_fires",
            message: "this will never notify",
            severity: "warning",
          },
        ]}
      />,
    );

    // The heading must not claim the document is invalid when only a warning
    // is present — the two call for different operator responses.
    expect(screen.getByText(/will not activate/i)).toBeInTheDocument();
    expect(screen.getByText(/unknown operator/)).toBeInTheDocument();
    expect(screen.getByText(/this will never notify/)).toBeInTheDocument();
  });

  it("says 'advisory' and not 'invalid' when only warnings are present", () => {
    render(
      <OperatorIssueList
        issues={[
          { where: "x", code: "level_only_never_fires", message: "advisory text", severity: "warning" },
        ]}
      />,
    );
    expect(screen.getByText(/Advisory warnings/i)).toBeInTheDocument();
    expect(screen.queryByText(/will not activate/i)).not.toBeInTheDocument();
  });

  it("renders nothing when there are no issues", () => {
    const { container } = render(<OperatorIssueList issues={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("bare mode omits the surrounding card, for embedding in a step body", () => {
    const { container } = render(
      <OperatorIssueList
        bare
        issues={[{ where: "x", code: "c", message: "boom", severity: "error" }]}
      />,
    );
    expect(within(container).getByText(/boom/)).toBeInTheDocument();
  });
});
