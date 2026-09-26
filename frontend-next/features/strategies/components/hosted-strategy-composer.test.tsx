import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedStrategyComposer } from "./hosted-strategy-composer";
import { HOSTED_STARTER_SOURCE } from "@/features/strategies/lib/starter";
import type { SourceReadiness } from "@/lib/hosted-strategies/types";

const push = vi.fn();

// Every case here drives a debounced readiness check and a multi-step write
// sequence; under a fully parallel suite that needs more than the 5s default.
vi.setConfig({ testTimeout: 20_000 });

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/strategies/new",
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/lib/platform/api", () => ({
  fetchPlatformStatus: vi.fn().mockResolvedValue({
    mode: "paper",
    broker: { state: "ok", detail: null },
    market_data: { state: "ok", last_tick_age_s: 0 },
    strategy_runner: { state: "ok", last_seen_age_s: 0 },
    live: { enabled: true, lanes_open: [] },
  }),
  fetchPlatformLiveSettings: vi.fn(),
  updatePlatformLiveSettings: vi.fn(),
}));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchHostedOptions: vi.fn(),
  fetchHostedStrategies: vi.fn(),
  fetchHostedVersions: vi.fn(),
  fetchHostedStrategy: vi.fn(),
  checkSourceReadiness: vi.fn(),
  createHostedStrategy: vi.fn(),
  createHostedVersion: vi.fn(),
  setAuthorizationMode: vi.fn(),
  saveAdmissionPolicy: vi.fn(),
  issueExecutionGrant: vi.fn(),
  runHostedStrategy: vi.fn(),
  saveHostedSchedule: vi.fn(),
  fetchAuthorization: vi.fn(),
  fetchExecutionGrants: vi.fn(),
  fetchExecutionRequests: vi.fn(),
  fetchAdmissionPolicy: vi.fn(),
  fetchHostedSchedule: vi.fn(),
  fetchHostedScheduleOccurrences: vi.fn(),
  fetchOperatorCalendar: vi.fn(),
  fetchPlan: vi.fn(),
  fetchHostedPositions: vi.fn(),
}));

import {
  checkSourceReadiness,
  createHostedStrategy,
  createHostedVersion,
  fetchHostedOptions,
  fetchHostedStrategies,
  fetchHostedVersions,
  issueExecutionGrant,
  runHostedStrategy,
  saveAdmissionPolicy,
  setAuthorizationMode,
} from "@/lib/hosted-strategies/api";
import { toast } from "sonner";

const READY_SOURCE = "def main(ctx):\n    return 0\n";

function readiness(overrides: Partial<SourceReadiness> = {}): SourceReadiness {
  return {
    schema_version: 1,
    status: "ready",
    profile: {
      id: "hosted-python-dataframe-indicators",
      python: "3.14",
      base_image: "python:3.14-slim",
      packages: [],
      server_side_indicators: true,
      runtime_pip_install: false,
      notes: null,
    },
    checks: [],
    entrypoint: {
      found: true,
      compatible: true,
      name: "main",
      is_async: false,
      detail: "main(ctx) found",
      remediation: null,
    },
    imports: {
      available: [],
      missing: [],
      optional_available: [],
      optional_missing: [],
      providers: {},
      dynamic: false,
    },
    messages: [],
    ...overrides,
  };
}

/** The server reports `ready` while a check is `unknown`. */
function readyWithUnknownCheck(): SourceReadiness {
  return readiness({
    checks: [
      {
        id: "entrypoint",
        status: "unknown",
        detail: "main is reassigned at module level",
        remediation: null,
      },
    ],
  });
}

const CREATED_STRATEGY = {
  strategy_id: "s-1",
  owner_id: "owner",
  name: "opening range",
  template_id: "hosted:s-1",
  description: null,
  default_execution_mode: "paper",
  default_job_kind: "finite",
  default_account_scope: "kite:paper",
  max_duration_s: 21600,
  progress_deadline_s: 600,
  stale_exit_policy: "none",
  status: "active",
  created_at: null,
  updated_at: null,
};

function versionRow(overrides: Record<string, unknown> = {}) {
  return {
    version_id: "v-1",
    strategy_id: "s-1",
    version: 1,
    source: READY_SOURCE,
    source_sha256: "a".repeat(64),
    parameters_schema: {},
    capabilities_snapshot: {},
    created_by: "owner",
    created_at: null,
    ...overrides,
  };
}

function queuedJob() {
  return {
    job_id: "j-1",
    strategy_id: "s-1",
    owner_id: "owner",
    attempt: 1,
    status: "queued",
    desired_state: "started",
    execution_mode: "paper",
    account_scope: "kite:paper",
    run_id: null,
    replacement_blocked: true,
    recovery_required_at: null,
    reconciled_at: null,
    created_at: null,
    updated_at: null,
    handoff_at: null,
    process_cleanup_state: null,
    process_cleanup_at: null,
    process_cleanup_actor: null,
    last_progress_at: null,
    version_id: "v-1",
    token_present: false,
    stop_requested_at: null,
    stop_requested_by: null,
    stop: {
      requested: false,
      state: "none" as const,
      requested_at: null,
      requested_by: null,
      replacement_blocked: false,
      note: "",
    },
    logs_discarded: false,
    logs_source: null,
  };
}

function renderComposer() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HostedStrategyComposer />
    </QueryClientProvider>,
  );
}

async function fillBasics(name = "opening range", source = READY_SOURCE) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/^name$/i), name);
  await user.type(screen.getByLabelText(/python source/i), source);
  await waitFor(() => expect(screen.getByTestId("readiness-ready")).toBeInTheDocument(), {
    timeout: 15_000,
  });
  return user;
}

function setSource(text: string) {
  fireEvent.change(screen.getByLabelText(/python source/i), { target: { value: text } });
}

/**
 * Permissions, the trade-authorization choice, limits, job kind, stale-exit
 * policy and duration all live behind the "Advanced" disclosure now that the
 * primary flow only asks for name, code, params, mode and run style.
 */
async function openAdvanced(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /advanced/i }));
}

/** Radix Select is a button + listbox, not a native <select>. */
async function chooseOption(
  user: ReturnType<typeof userEvent.setup>,
  label: RegExp,
  option: string,
) {
  await user.click(screen.getByLabelText(label));
  await user.click(await screen.findByRole("option", { name: option }));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchHostedOptions).mockResolvedValue({
    account_scopes: ["kite:paper"],
    execution_modes: ["paper", "dry_run"],
    job_kinds: ["finite", "continuous"],
    stale_exit_policies: ["none", "exit_on_worker_stale"],
    hosted_execution_only: true,
  });
  vi.mocked(fetchHostedStrategies).mockResolvedValue({ strategies: [] });
  vi.mocked(fetchHostedVersions).mockResolvedValue({ versions: [] });
  vi.mocked(checkSourceReadiness).mockResolvedValue(readiness());
  vi.mocked(createHostedStrategy).mockResolvedValue(CREATED_STRATEGY);
  vi.mocked(createHostedVersion).mockResolvedValue(versionRow());
  vi.mocked(runHostedStrategy).mockResolvedValue({ idempotent: false, job: queuedJob() });
});

describe("hosted strategy composer", () => {
  it("blocks a source with no entrypoint and keeps it recoverable", async () => {
    vi.mocked(checkSourceReadiness).mockResolvedValue(
      readiness({
        status: "blocked",
        checks: [
          {
            id: "entrypoint",
            status: "blocked",
            detail: "no module-level main(ctx) was found",
            remediation: "Define main(ctx): with a single context parameter.",
          },
        ],
        entrypoint: {
          found: false,
          compatible: false,
          name: null,
          is_async: null,
          detail: "no module-level main(ctx) was found",
          remediation: "Define main(ctx): with a single context parameter.",
        },
      }),
    );
    renderComposer();
    await userEvent.setup().type(screen.getByLabelText(/^name$/i), "no entrypoint");
    setSource("print('hello')\n");

    await waitFor(() => expect(screen.getByTestId("readiness-blocked")).toBeInTheDocument(), {
      timeout: 15_000,
    });
    expect(screen.getByRole("button", { name: /^start$/i })).toBeDisabled();
    expect(screen.getByLabelText(/python source/i)).toHaveValue("print('hello')\n");
    expect(createHostedStrategy).not.toHaveBeenCalled();
  });

  it("never calls a partly-unverified source ready, and needs an acknowledgement", async () => {
    vi.mocked(checkSourceReadiness).mockResolvedValue(readyWithUnknownCheck());
    renderComposer();
    await userEvent.setup().type(screen.getByLabelText(/^name$/i), "unknown check");
    setSource(READY_SOURCE);

    const warning = await screen.findByTestId("readiness-unknown", {}, { timeout: 15_000 });
    expect(warning).toHaveTextContent(/not certified ready/i);
    expect(screen.queryByTestId("readiness-ready")).toBeNull();
    expect(screen.getByRole("button", { name: /^start$/i })).toBeDisabled();

    await userEvent.setup().click(screen.getByLabelText(/acknowledge the unverified source/i));
    await waitFor(() => expect(screen.getByRole("button", { name: /^start$/i })).toBeEnabled());
  });

  it("refuses an oversized paste without touching the editor contents", async () => {
    renderComposer();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/python source/i), READY_SOURCE);
    setSource("x".repeat(300 * 1024));

    expect(await screen.findByText(/larger than the platform's 256 KB limit/i)).toBeInTheDocument();
    expect(checkSourceReadiness).not.toHaveBeenCalledWith("x".repeat(300 * 1024));
  });

  it("does not refuse the launch over a file drop that changed nothing", async () => {
    renderComposer();
    const user = await fillBasics("drop refusal");
    const area = screen.getByLabelText(/python source/i);

    // A refused drop leaves the typed source exactly as it was, so it is not a
    // reason to refuse the launch too: the readiness answer for this source
    // stays the authority on whether it can run.
    fireEvent.drop(area, {
      dataTransfer: { files: [new File(["not python"], "notes.txt", { type: "text/plain" })] },
    });
    expect(await screen.findByText(/choose a python file/i)).toBeInTheDocument();
    expect(area).toHaveValue(READY_SOURCE);

    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() => expect(runHostedStrategy).toHaveBeenCalledTimes(1), { timeout: 15_000 });
    expect(vi.mocked(toast.error).mock.calls).toEqual([]);
  });

  it("records the owner's limits for a review-first strategy, with no grant", async () => {
    renderComposer();
    const user = await fillBasics("review first limits");
    await openAdvanced(user);
    // "Propose trades" is on by default now; review-first is the default choice.
    expect(screen.getByRole("button", { name: /review trades first/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.type(screen.getByLabelText(/allocation \(inr\)/i), "250000");
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    await waitFor(() => expect(runHostedStrategy).toHaveBeenCalledTimes(1), { timeout: 15_000 });
    expect(saveAdmissionPolicy).toHaveBeenCalledWith("s-1", { allocation_inr: 250000 });
    // Review-first is not a standing authorization: nothing is granted.
    expect(issueExecutionGrant).not.toHaveBeenCalled();
    expect(setAuthorizationMode).not.toHaveBeenCalled();
  });

  it("offers the data-only starter that needs no hidden parameters", async () => {
    renderComposer();
    await userEvent.setup().click(screen.getByRole("button", { name: /insert starter/i }));
    expect(screen.getByLabelText(/python source/i)).toHaveValue(HOSTED_STARTER_SOURCE);
    expect(HOSTED_STARTER_SOURCE).not.toContain("strategy_id");
    await waitFor(() => expect(screen.getByTestId("readiness-ready")).toBeInTheDocument(), {
      timeout: 15_000,
    });
  });

  it("sends exactly the operator's parameter values, even under a strict schema", async () => {
    renderComposer();
    const user = await fillBasics("strict params");
    // A strict schema with a required parameter that has no default.
    await user.click(screen.getByRole("checkbox", { name: /use a json schema instead/i }));
    const schemaBox = screen.getByLabelText(/parameters schema/i);
    fireEvent.change(schemaBox, {
      target: {
        value: JSON.stringify({
          type: "object",
          properties: {
            // Explicitly allows 0: the value must survive the round trip.
            quantity: { type: "integer", minimum: 0 },
            enabled: { type: "boolean" },
            mode: { type: "string", enum: ["intraday", "positional"] },
          },
          required: ["quantity"],
          additionalProperties: false,
        }),
      },
    });

    await waitFor(() => expect(screen.getByLabelText(/^quantity \*$/i)).toBeInTheDocument());
    // Required with no default: submitting before filling it is refused here.
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/check the parameters/i));
    expect(createHostedStrategy).not.toHaveBeenCalled();
    vi.mocked(toast.error).mockClear();

    await user.type(screen.getByLabelText(/^quantity \*$/i), "0");
    await chooseOption(user, /^mode$/i, "positional");
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    expect(vi.mocked(toast.error).mock.calls).toEqual([]);
    await waitFor(() => expect(runHostedStrategy).toHaveBeenCalledTimes(1));
    const [, launch] = vi.mocked(runHostedStrategy).mock.calls[0];
    expect(launch.params).toEqual({ quantity: 0, mode: "positional", enabled: false });
    expect(Object.keys(launch.params)).not.toContain("strategy_id");
  });

  it("registers a new version and mints a new launch key when the code changes after a failure", async () => {
    vi.mocked(runHostedStrategy).mockRejectedValueOnce(new Error("launch failed"));
    renderComposer();
    const user = await fillBasics("edited after failure");
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() => expect(screen.getByTestId("composer-error")).toBeInTheDocument(), {
      timeout: 15_000,
    });
    expect(createHostedVersion).toHaveBeenCalledTimes(1);
    const firstLaunchKey = vi.mocked(runHostedStrategy).mock.calls[0][1].idempotency_key;

    // The operator edits the code: a NEW version is registered for the new source.
    setSource("def main(ctx):\n    return 1\n");
    await waitFor(() => expect(screen.getByTestId("readiness-ready")).toBeInTheDocument(), {
      timeout: 15_000,
    });
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() => expect(runHostedStrategy).toHaveBeenCalledTimes(2));

    expect(createHostedStrategy).toHaveBeenCalledTimes(1); // no duplicate strategy
    expect(createHostedVersion).toHaveBeenCalledTimes(2);
    expect(vi.mocked(createHostedVersion).mock.calls[1][1].source).toContain("return 1");
    const secondLaunchKey = vi.mocked(runHostedStrategy).mock.calls[1][1].idempotency_key;
    expect(secondLaunchKey).not.toBe(firstLaunchKey);
  });

  it("switches the server back to review-first when the choice changes", async () => {
    vi.mocked(runHostedStrategy).mockRejectedValueOnce(new Error("launch failed"));
    renderComposer();
    const user = await fillBasics("mode switch");
    await openAdvanced(user);
    await user.click(screen.getByRole("button", { name: /trade automatically within my limits/i }));
    await user.type(screen.getByLabelText(/allocation \(inr\)/i), "50000");
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() => expect(screen.getByTestId("composer-error")).toBeInTheDocument(), {
      timeout: 15_000,
    });
    expect(setAuthorizationMode).toHaveBeenCalledWith("s-1", {
      mode: "autonomous",
      reason: "chosen while creating the strategy",
    });

    await user.click(screen.getByRole("button", { name: /review trades first/i }));
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() =>
      expect(setAuthorizationMode).toHaveBeenCalledWith("s-1", {
        mode: "approval_based",
        reason: "review-first chosen while creating the strategy",
      }),
    );
  });

  it("persists review-first when the autonomous answer was lost and the mode is unknown", async () => {
    // The server accepted the autonomous choice but the page never saw the
    // answer, so the only proof the server is not left armed is writing
    // review-first. The launched job fails after the mode write, which is how
    // the page ends up with an unknown-but-armed server mode and no local mode.
    vi.mocked(runHostedStrategy).mockRejectedValueOnce(new Error("launch failed"));
    renderComposer();
    const user = await fillBasics("lost mode answer");
    await openAdvanced(user);
    await user.click(screen.getByRole("button", { name: /trade automatically within my limits/i }));
    await user.type(screen.getByLabelText(/allocation \(inr\)/i), "50000");
    // The mode-write answer itself is lost, so ``serverMode`` stays unknown.
    vi.mocked(setAuthorizationMode).mockRejectedValueOnce(new Error("Network request failed"));
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() => expect(setAuthorizationMode).toHaveBeenCalledTimes(1), { timeout: 15_000 });
    expect(setAuthorizationMode).toHaveBeenCalledWith("s-1", {
      mode: "autonomous",
      reason: "chosen while creating the strategy",
    });

    await user.click(screen.getByRole("button", { name: /review trades first/i }));
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() =>
      expect(setAuthorizationMode).toHaveBeenCalledWith("s-1", {
        mode: "approval_based",
        reason: "review-first chosen while creating the strategy",
      }),
    );
  });

  it("persists review-first when adopting an existing autonomous strategy", async () => {
    // Adoption recovers a strategy whose creation response was lost. That path
    // does not read the server's authorization mode, so the mode is unknown and
    // review-first must still be written instead of leaving autonomy armed.
    vi.mocked(createHostedStrategy).mockRejectedValueOnce(new Error("network down"));
    vi.mocked(runHostedStrategy).mockRejectedValueOnce(new Error("launch failed"));
    vi.mocked(fetchHostedStrategies).mockResolvedValue({
      strategies: [
        { ...CREATED_STRATEGY, name: "adopted autonomous", authorization_mode: "autonomous" },
      ],
    });
    renderComposer();
    const user = await fillBasics("adopted autonomous");
    await openAdvanced(user);
    await user.click(screen.getByRole("button", { name: /review trades first/i }));
    await user.type(screen.getByLabelText(/allocation \(inr\)/i), "50000");
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    await waitFor(
      () =>
        expect(setAuthorizationMode).toHaveBeenCalledWith("s-1", {
          mode: "approval_based",
          reason: "review-first chosen while creating the strategy",
        }),
      { timeout: 15_000 },
    );
    expect(issueExecutionGrant).not.toHaveBeenCalled();
  });

  it("mints a new authorization key when the owner's limits change", async () => {
    vi.mocked(runHostedStrategy).mockRejectedValueOnce(new Error("launch failed"));
    renderComposer();
    const user = await fillBasics("limits change");
    await openAdvanced(user);
    await user.click(screen.getByRole("button", { name: /trade automatically within my limits/i }));
    await user.type(screen.getByLabelText(/allocation \(inr\)/i), "50000");
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() => expect(issueExecutionGrant).toHaveBeenCalledTimes(1), { timeout: 15_000 });
    const firstGrantKey = vi.mocked(issueExecutionGrant).mock.calls[0][1].idempotency_key;

    // The owner changes their own number: the authorization is bound to the
    // limits, so the retry records the new ones and issues a NEW request rather
    // than replaying the grant that was bound to the old ones.
    const allocation = screen.getByLabelText(/allocation \(inr\)/i);
    await user.clear(allocation);
    await user.type(allocation, "75000");
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    await waitFor(() => expect(issueExecutionGrant).toHaveBeenCalledTimes(2));
    expect(saveAdmissionPolicy).toHaveBeenLastCalledWith("s-1", { allocation_inr: 75000 });
    const secondGrantKey = vi.mocked(issueExecutionGrant).mock.calls[1][1].idempotency_key;
    expect(secondGrantKey).not.toBe(firstGrantKey);
  });

  it("refuses changed account settings on an existing strategy instead of adopting it", async () => {
    vi.mocked(runHostedStrategy).mockRejectedValueOnce(new Error("launch failed"));
    renderComposer();
    const user = await fillBasics("account change");
    await user.click(screen.getByRole("button", { name: /^start$/i }));
    await waitFor(() => expect(screen.getByTestId("composer-error")).toBeInTheDocument(), {
      timeout: 15_000,
    });

    // A different environment on an already-created strategy cannot be applied.
    await user.click(screen.getByRole("button", { name: /insert starter/i }));
    setSource(READY_SOURCE);
    await waitFor(() => expect(screen.getByTestId("readiness-ready")).toBeInTheDocument(), {
      timeout: 15_000,
    });
    await chooseOption(user, /^environment$/i, "Dry run (no orders at all)");
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    // The refusal is stated on the page (not just a toast), with the way out.
    expect(screen.getByTestId("composer-error")).toHaveTextContent(
      /already exists with different name, account, environment/i,
    );
    expect(createHostedStrategy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /start a new strategy/i })).not.toBeNull();
  });

  it("recovers the strategy created by a lost response instead of creating another", async () => {
    vi.mocked(createHostedStrategy).mockRejectedValueOnce(new Error("network down"));
    renderComposer();
    const user = await fillBasics("opening range");
    vi.mocked(fetchHostedStrategies).mockResolvedValue({ strategies: [CREATED_STRATEGY] });
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    await waitFor(() => expect(createHostedVersion).toHaveBeenCalledTimes(1), { timeout: 15_000 });
    expect(createHostedStrategy).toHaveBeenCalledTimes(1);
    expect(vi.mocked(createHostedVersion).mock.calls[0][0]).toBe("s-1");
  });

  it("refuses to adopt a same-named strategy whose defaults differ", async () => {
    vi.mocked(createHostedStrategy).mockRejectedValueOnce(new Error("network down"));
    renderComposer();
    const user = await fillBasics("opening range");
    // The row that exists carries a different run kind and duration than the
    // page shows, so continuing would run settings the operator never agreed to.
    vi.mocked(fetchHostedStrategies).mockResolvedValue({
      strategies: [{ ...CREATED_STRATEGY, default_job_kind: "continuous", max_duration_s: 3600 }],
    });
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    const refusal = await screen.findByTestId("composer-error", {}, { timeout: 15_000 });
    expect(refusal).toHaveTextContent(/already exists with different name, account, environment/i);
    expect(createHostedVersion).not.toHaveBeenCalled();
    expect(runHostedStrategy).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /start a new strategy/i })).not.toBeNull();
  });

  it("reuses the exact revision a lost version response left behind", async () => {
    vi.mocked(createHostedVersion).mockRejectedValueOnce(new Error("network down"));
    renderComposer();
    const user = await fillBasics("version recovery");
    vi.mocked(fetchHostedVersions).mockResolvedValue({
      versions: [
        versionRow({
          version: 3,
          version_id: "v-3",
          source: READY_SOURCE,
          parameters_schema: {},
          capabilities_snapshot: { data: true, trade: true, notify: false },
        }),
      ],
    });
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    await waitFor(() => expect(runHostedStrategy).toHaveBeenCalledTimes(1), { timeout: 15_000 });
    // No second copy of the same source, and the launch points at the revision
    // that already exists.
    expect(createHostedVersion).toHaveBeenCalledTimes(1);
    expect(vi.mocked(runHostedStrategy).mock.calls[0][1].version_id).toBe("v-3");
  });

  it("does not reuse a revision that differs from what this page submitted", async () => {
    vi.mocked(createHostedVersion).mockRejectedValueOnce(new Error("network down"));
    renderComposer();
    const user = await fillBasics("version mismatch");
    vi.mocked(fetchHostedVersions).mockResolvedValue({
      versions: [
        versionRow({
          version_id: "v-other",
          source: "def main(ctx):\n    return 99\n",
          capabilities_snapshot: { data: true, trade: true, notify: false },
        }),
      ],
    });
    await user.click(screen.getByRole("button", { name: /^start$/i }));

    const refusal = await screen.findByTestId("composer-error", {}, { timeout: 15_000 });
    expect(refusal).toHaveTextContent(/did not complete/i);
    expect(runHostedStrategy).not.toHaveBeenCalled();
  });

  it("shows the trade decision by default and hides it once trading is turned off", async () => {
    renderComposer();
    const user = await fillBasics("data only");
    await openAdvanced(user);
    // "Propose trades" defaults on, so the trade decision is visible immediately.
    expect(screen.getByRole("button", { name: /review trades first/i })).toBeInTheDocument();
    // Review-first still needs admission limits: they are owner numbers, not a
    // default, so the fields are empty and the value is never invented.
    expect(screen.getByLabelText(/allocation \(inr\)/i)).toHaveValue("");

    await user.click(screen.getByRole("checkbox", { name: /propose trades/i }));
    expect(screen.getByTestId("authorization-inapplicable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /trade automatically within my limits/i })).toBeNull();
  });
});
