import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedSchedulePanel } from "./hosted-schedule-panel";
import type {
  HostedSchedule,
  HostedStrategy,
  HostedStrategyOptions,
  HostedVersion,
} from "@/lib/hosted-strategies/types";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchHostedSchedule: vi.fn(),
  fetchHostedScheduleOccurrences: vi.fn(),
  fetchOperatorCalendar: vi.fn(),
  saveHostedSchedule: vi.fn(),
  setHostedScheduleEnabled: vi.fn(),
}));

import {
  fetchHostedSchedule,
  fetchHostedScheduleOccurrences,
  fetchOperatorCalendar,
  saveHostedSchedule,
  setHostedScheduleEnabled,
} from "@/lib/hosted-strategies/api";

function strategy(): HostedStrategy {
  return {
    strategy_id: "s-1",
    owner_id: "app:admin",
    name: "opening range",
    template_id: "hosted:s-1",
    description: null,
    default_execution_mode: "paper",
    default_job_kind: "finite",
    default_account_scope: "kite:paper",
    max_duration_s: 21600,
    progress_deadline_s: 600,
    stale_exit_policy: "none",
    authorization_mode: "approval_based",
    status: "active",
    created_at: null,
    updated_at: null,
  };
}

const VERSIONS: HostedVersion[] = [
  {
    version_id: "v-1",
    strategy_id: "s-1",
    version: 1,
    source: "def main(ctx):\n    return 0\n",
    source_sha256: "a".repeat(64),
    parameters_schema: {
      type: "object",
      properties: { quantity: { type: "integer", minimum: 1 } },
      required: ["quantity"],
    },
    capabilities_snapshot: {},
    created_by: "app:admin",
    created_at: null,
  },
];

const OPTIONS: HostedStrategyOptions = {
  account_scopes: ["kite:paper"],
  execution_modes: ["paper"],
  job_kinds: ["finite", "continuous"],
  stale_exit_policies: ["none"],
  hosted_execution_only: true,
};

function schedule(overrides: Partial<HostedSchedule> = {}): HostedSchedule {
  return {
    schedule_id: "sch-1",
    strategy_id: "s-1",
    version_id: "v-1",
    version_number: 1,
    account_scope: "kite:paper",
    execution_mode: "paper",
    job_kind: "finite",
    params_snapshot: { quantity: 2 },
    schedule_kind: "daily",
    at_time: "09:30",
    weekday: null,
    day_of_month: null,
    calendar_dates: [],
    timezone: "Asia/Kolkata",
    window_end: null,
    squareoff_at: null,
    enabled: true,
    manually_paused: false,
    max_duration_s: 21600,
    progress_deadline_s: 600,
    misfire_grace_seconds: 3600,
    overlap_policy: "defer_until_resolved",
    next_occurrence_at: "2026-09-24T04:00:00+00:00",
    next_occurrence_key: "sch-1:2026-09-24",
    last_occurrence: {
      occurrence_key: "sch-1:2026-09-20",
      due_at: "2026-09-20T04:00:00+00:00",
      status: "skipped",
      fired_at: null,
      evaluation_id: null,
      skip_reason: "MISFIRE_GRACE_EXCEEDED",
      detail: {},
    },
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HostedSchedulePanel strategy={strategy()} versions={VERSIONS} options={OPTIONS} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchHostedSchedule).mockResolvedValue(null);
  vi.mocked(fetchHostedScheduleOccurrences).mockResolvedValue([]);
  vi.mocked(fetchOperatorCalendar).mockResolvedValue({
    schema_version: 1,
    source: "operator_imported_official_nse_document",
    source_as_of: "2026-09-01T00:00:00+00:00",
    retrieved_at: "2026-09-23T04:00:00+00:00",
    exchange: "NSE",
    segment: "CM",
    calendar_version: 1,
    official_source_document_sha256: "a".repeat(64),
    canonical_csv_sha256: "b".repeat(64),
    sessions: [
      {
        session_date: "2026-09-24",
        session_type: "REGULAR",
        opens_at: "2026-09-24T03:45:00+00:00",
        closes_at: "2026-09-24T10:00:00+00:00",
        verified: true,
        source_reference: "NSE",
      },
    ],
  });
  vi.mocked(saveHostedSchedule).mockResolvedValue(schedule());
  vi.mocked(setHostedScheduleEnabled).mockResolvedValue(schedule({ enabled: false }));
});

describe("hosted schedule panel", () => {
  it("reports the runtime's next run, last occurrence and policies", async () => {
    vi.mocked(fetchHostedSchedule).mockResolvedValue(schedule());
    vi.mocked(fetchHostedScheduleOccurrences).mockResolvedValue([
      {
        occurrence_key: "sch-1:2026-09-20",
        due_at: "2026-09-20T04:00:00+00:00",
        status: "skipped",
        fired_at: null,
        evaluation_id: null,
        skip_reason: "MISFIRE_GRACE_EXCEEDED",
        detail: {},
      },
    ]);
    renderPanel();

    expect(await screen.findByText(/every day at 09:30 Asia\/Kolkata/i)).toBeInTheDocument();
    expect(screen.getByTestId("schedule-next")).toHaveTextContent("2026-09-24T04:00:00+00:00");
    expect(screen.getByTestId("schedule-last")).toHaveTextContent(/skipped/i);
    expect(screen.getByTestId("schedule-last")).toHaveTextContent(/MISFIRE_GRACE_EXCEEDED/);
    const policy = screen.getByTestId("schedule-policy").textContent ?? "";
    expect(policy).toMatch(/within 1 hour/i);
    expect(policy).toMatch(/waits while the previous one is still unresolved/i);
  });

  it("disables a schedule without touching the strategy", async () => {
    vi.mocked(fetchHostedSchedule).mockResolvedValue(schedule());
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^disable$/i }));
    await waitFor(() => expect(setHostedScheduleEnabled).toHaveBeenCalledWith("s-1", false));
  });

  it("creates a schedule with the operator's parameters and stamps the strategy identity", async () => {
    renderPanel();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: /create schedule/i });
    await user.type(screen.getByLabelText(/^quantity \*$/i), "3");
    await user.clear(screen.getByLabelText(/at \(local time/i));
    await user.type(screen.getByLabelText(/at \(local time/i), "15:45");
    await user.click(screen.getByRole("button", { name: /create schedule/i }));

    await waitFor(() =>
      expect(saveHostedSchedule).toHaveBeenCalledWith(
        "s-1",
        expect.objectContaining({
          version_id: "v-1",
          execution_mode: "paper",
          schedule_kind: "daily",
          at_time: "15:45",
          timezone: "Asia/Kolkata",
          enabled: true,
          // Exactly the operator's parameters: the platform stamps nothing.
          params: { quantity: 3 },
        }),
      ),
    );
  });

  it("reports an uncovered exchange calendar instead of inventing sessions", async () => {
    const { ApiClientError } = await import("@/lib/api/client");
    vi.mocked(fetchOperatorCalendar).mockRejectedValue(
      new ApiClientError(503, { detail: { rejection_reason: "CALENDAR_RANGE_UNCOVERED" } }),
    );
    renderPanel();
    const message = await screen.findByTestId("calendar-unavailable");
    expect(message).toHaveTextContent(/CALENDAR_RANGE_UNCOVERED/);
  });
});
