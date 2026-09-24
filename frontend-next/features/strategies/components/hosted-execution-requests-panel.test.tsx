import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedExecutionRequestsPanel } from "./hosted-execution-requests-panel";
import type { ExecutionRequestRow } from "@/lib/hosted-strategies/types";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchExecutionRequests: vi.fn(),
  approveExecutionRequest: vi.fn(),
  rejectExecutionRequest: vi.fn(),
  fetchPlan: vi.fn(),
  previewPlanAdmission: vi.fn(),
}));

import {
  approveExecutionRequest,
  fetchExecutionRequests,
  fetchPlan,
  rejectExecutionRequest,
} from "@/lib/hosted-strategies/api";

function request(overrides: Partial<ExecutionRequestRow> = {}): ExecutionRequestRow {
  return {
    request_id: "req-1",
    owner_id: "app:admin",
    strategy_id: "s-1",
    canonical_strategy_id: "s-1",
    account_id: "kite:paper",
    execution_environment: "paper",
    strategy_run_id: "run-1",
    job_id: "j-1",
    token_id: "tok-1",
    attempt: 1,
    lease_epoch: 3,
    version_id: "v-1",
    version_number: 1,
    source_sha256: "a".repeat(64),
    policy_hash: "b".repeat(64),
    evaluation_id: "eval-1",
    plan_id: "plan-1",
    plan_hash: "c".repeat(64),
    authorization_mode: "approval_based",
    grant_id: null,
    status: "awaiting_approval",
    refusal_code: null,
    refusal_detail: {},
    decision_kind: null,
    decision_actor: null,
    decision_at: null,
    decision_evidence: {},
    approval_id: null,
    reservation_id: null,
    execution_detail: {},
    outcome_state: null,
    dispatch_claim_id: null,
    dispatch_claimed_at: null,
    dispatch_started_at: null,
    dispatch_finished_at: null,
    idempotency_key: "internal-key",
    created_at: "2026-09-23T04:00:00+00:00",
    updated_at: null,
    terminal: false,
    executable: false,
    ...overrides,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HostedExecutionRequestsPanel strategyId="s-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchExecutionRequests).mockResolvedValue({ strategy_id: "s-1", requests: [] });
  vi.mocked(approveExecutionRequest).mockResolvedValue({
    request: request({ status: "queued" }),
    approved: true,
    rejected: false,
  });
  vi.mocked(rejectExecutionRequest).mockResolvedValue({
    request: request({ status: "rejected" }),
    approved: false,
    rejected: true,
  });
  vi.mocked(fetchPlan).mockResolvedValue({
    plan_id: "plan-1",
    proposal_id: "prop-1",
    strategy_id: "s-1",
    account_id: "kite:paper",
    plan_kind: "single_instrument",
    plan_hash: "c".repeat(64),
    logical_plan: {},
    resolved_plan: {
      legs: [{ tradingsymbol: "RELIANCE", signed_quantity: 5, product: "CNC", reference_price: 2900.5 }],
    },
    pinned_universe_revision_id: null,
    pinned_member_hash: null,
    pinned_catalog_generation: "gen-1",
    invalidation_state: { valid: true },
  });
});

describe("hosted execution requests panel", () => {
  it("explains that queued is not running and dispatched is not filled", async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByTestId("request-state-explainer")).toBeInTheDocument());
    const copy = screen.getByTestId("request-state-explainer").textContent ?? "";
    expect(copy).toMatch(/Queued means the platform has not started/i);
    expect(copy).toMatch(/not that the broker accepted/i);
    expect(copy).toMatch(/is not retried on its own/i);
  });

  it("approves exactly one awaiting plan and shows the executor's own outcome", async () => {
    vi.mocked(fetchExecutionRequests).mockResolvedValue({
      strategy_id: "s-1",
      requests: [request()],
    });
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /approve this plan/i }));

    await waitFor(() =>
      expect(approveExecutionRequest).toHaveBeenCalledWith("s-1", "req-1", { reason: null }),
    );
    expect(screen.getByTestId("request-status-req-1")).toHaveTextContent(/waiting for your decision/i);
  });

  it("rejects a plan without queuing anything", async () => {
    vi.mocked(fetchExecutionRequests).mockResolvedValue({
      strategy_id: "s-1",
      requests: [request()],
    });
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^reject$/i }));
    await waitFor(() =>
      expect(rejectExecutionRequest).toHaveBeenCalledWith("s-1", "req-1", { reason: null }),
    );
    expect(approveExecutionRequest).not.toHaveBeenCalled();
  });

  it("shows a named refusal in readable words and the executor's outcome", async () => {
    vi.mocked(fetchExecutionRequests).mockResolvedValue({
      strategy_id: "s-1",
      requests: [
        request({
          request_id: "req-2",
          status: "refused",
          authorization_mode: "autonomous",
          decision_kind: "automatic",
          refusal_code: "GRANT_REVOKED",
        }),
        request({
          request_id: "req-3",
          status: "executed",
          outcome_state: "submitted",
          terminal: true,
        }),
      ],
    });
    renderPanel();
    const refusal = await screen.findByTestId("request-refusal");
    expect(refusal).toHaveTextContent(/revoked/i);
    expect(refusal).toHaveTextContent(/GRANT_REVOKED/);
    expect(screen.getByText(/Sent to the broker — acceptance not yet confirmed/i)).toBeInTheDocument();
    expect(screen.getByText(/decided by your standing authorization/i)).toBeInTheDocument();
  });

  it("shows the frozen plan's legs when the operator expands it", async () => {
    vi.mocked(fetchExecutionRequests).mockResolvedValue({
      strategy_id: "s-1",
      requests: [request()],
    });
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^plan$/i }));

    await waitFor(() => expect(fetchPlan).toHaveBeenCalledWith("s-1", "plan-1"));
    expect(await screen.findByText(/RELIANCE/)).toBeInTheDocument();
    expect(screen.getByText(/5 × CNC/)).toBeInTheDocument();
  });
});
