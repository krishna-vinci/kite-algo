import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AlertsListPage } from "./alerts-list-page";
import type { AlertsWorkflowSummary } from "@/features/alerts/types";

const replaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  usePathname: () => "/alerts",
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

vi.mock("@/features/alerts/api", () => ({
  fetchAlertsScopes: vi.fn(),
  fetchAlertsWorkflows: vi.fn(),
}));

import { fetchAlertsScopes, fetchAlertsWorkflows } from "@/features/alerts/api";

function renderPage(ui: ReactElement = <AlertsListPage />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function workflow(partial: Partial<AlertsWorkflowSummary> = {}): AlertsWorkflowSummary {
  return {
    workflow_id: "wf-1",
    name: "NIFTY breakout",
    archived: false,
    archived_at: null,
    created_at: null,
    updated_at: null,
    latest_revision: null,
    active_revision: {
      revision_id: "r1",
      revision: 1,
      status: "active",
      canonical_hash: "hash",
      created_at: null,
      activated_at: null,
    },
    kind: "alert",
    instruments: ["NSE:INFY"],
    instrument_summary: "NSE:INFY",
    has_universe: false,
    alerts: [],
    channels: ["telegram-primary"],
    warnings: [],
    subscription_count: 1,
    freshness: {
      last_evaluated_at: null,
      evaluation_age_s: 12,
      subscription_count: 1,
      stale_subscriptions: 0,
      stale: false,
      stale_after_seconds: 300,
    },
    ...partial,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchAlertsScopes).mockResolvedValue({
    ok: true,
    scopes: [{ scope: "paper-a", is_default: true, has_data: true }],
    note: "",
  } as never);
});

describe("AlertsListPage", () => {
  it("renders lifecycle and freshness as separate signals", async () => {
    vi.mocked(fetchAlertsWorkflows).mockResolvedValue({
      ok: true,
      scope: "paper-a",
      workflows: [workflow()],
    } as never);

    renderPage();

    expect(await screen.findByText("NIFTY breakout")).toBeInTheDocument();
    // lifecycle
    expect(screen.getByText("active")).toBeInTheDocument();
    // freshness, shown independently of lifecycle
    expect(screen.getByText(/data 12s old/i)).toBeInTheDocument();
    expect(screen.getByText("NSE:INFY")).toBeInTheDocument();
  });

  it("shows 'not evaluated yet' when the backend reports stale === null", async () => {
    // A never-evaluated workflow must not read as healthy. stale === null is the
    // API's explicit "nothing has been evaluated" signal.
    vi.mocked(fetchAlertsWorkflows).mockResolvedValue({
      ok: true,
      scope: "paper-a",
      workflows: [
        workflow({
          freshness: {
            last_evaluated_at: null,
            evaluation_age_s: null,
            subscription_count: 0,
            stale_subscriptions: 0,
            stale: null,
            stale_after_seconds: 300,
          },
        }),
      ],
    } as never);

    renderPage();

    expect(await screen.findByText("not evaluated yet")).toBeInTheDocument();
  });

  it("surfaces a never-firing warning badge with its message", async () => {
    vi.mocked(fetchAlertsWorkflows).mockResolvedValue({
      ok: true,
      scope: "paper-a",
      workflows: [
        workflow({
          warnings: [
            {
              where: "alert:main",
              code: "level_only_transition",
              message: "a level condition cannot emit this transition trigger",
              severity: "error",
            },
          ],
        }),
      ],
    } as never);

    renderPage();

    expect(await screen.findByText("will not fire")).toBeInTheDocument();
    expect(
      screen.getByText("a level condition cannot emit this transition trigger"),
    ).toBeInTheDocument();
  });

  it("renders an empty state that distinguishes empty from unauthorized", async () => {
    vi.mocked(fetchAlertsWorkflows).mockResolvedValue({
      ok: true,
      scope: "paper-a",
      workflows: [],
    } as never);

    renderPage();

    expect(await screen.findByText("No alerts in this scope")).toBeInTheDocument();
    expect(screen.getByText(/not that the .*scope is wrong/i)).toBeInTheDocument();
  });

  it("renders a destructive alert when the list request fails", async () => {
    vi.mocked(fetchAlertsWorkflows).mockRejectedValue(new Error("boom"));

    renderPage();

    expect(await screen.findByText("Failed to load alerts")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("exposes the archived toggle and the new-alert entry point", async () => {
    vi.mocked(fetchAlertsWorkflows).mockResolvedValue({
      ok: true,
      scope: "paper-a",
      workflows: [],
    } as never);

    renderPage();

    expect(await screen.findByLabelText("Include archived")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /new alert/i })).toHaveAttribute(
      "href",
      "/alerts/new",
    );
  });
});
