import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedAuthorizationPanel } from "./hosted-authorization-panel";
import type {
  AuthorizationStatus,
  HostedStrategy,
  HostedVersion,
  PolicySnapshot,
} from "@/lib/hosted-strategies/types";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/lib/hosted-strategies/api", () => ({
  fetchAuthorization: vi.fn(),
  fetchExecutionGrants: vi.fn(),
  fetchAdmissionPolicy: vi.fn(),
  setAuthorizationMode: vi.fn(),
  issueExecutionGrant: vi.fn(),
  revokeExecutionGrant: vi.fn(),
  saveAdmissionPolicy: vi.fn(),
}));

import {
  fetchAdmissionPolicy,
  fetchAuthorization,
  fetchExecutionGrants,
  issueExecutionGrant,
  revokeExecutionGrant,
  saveAdmissionPolicy,
  setAuthorizationMode,
} from "@/lib/hosted-strategies/api";
import { toast } from "sonner";

const POLICY: PolicySnapshot = {
  admission: {
    account_id: "kite:paper",
    allocation_inr: null,
    per_instrument_notional_inr: null,
    gross_notional_inr: null,
    max_open_instruments: null,
    admissions_per_window: null,
    admission_window_seconds: null,
    daily_loss_budget_inr: null,
  },
  protection: { stale_exit_policy: "none", max_duration_s: 21600, progress_deadline_s: 600 },
};

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
    parameters_schema: {},
    capabilities_snapshot: {},
    created_by: "app:admin",
    created_at: null,
  },
];

function status(overrides: Partial<AuthorizationStatus> = {}): AuthorizationStatus {
  return {
    strategy_id: "s-1",
    authorization_mode: "approval_based",
    active_grant: null,
    policy_snapshot: POLICY,
    policy_hash: "b".repeat(64),
    policy_concrete: false,
    grant_usable: false,
    blocking_reasons: [],
    evaluated_at: null,
    ...overrides,
  };
}

function renderPanel(versions: HostedVersion[] = VERSIONS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HostedAuthorizationPanel
        strategy={strategy()}
        versions={versions}
        options={{
          account_scopes: ["kite:paper"],
          execution_modes: ["paper"],
          job_kinds: ["finite"],
          stale_exit_policies: ["none"],
          hosted_execution_only: true,
        }}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchAuthorization).mockResolvedValue(status());
  vi.mocked(fetchExecutionGrants).mockResolvedValue([]);
  vi.mocked(fetchAdmissionPolicy).mockResolvedValue(null);
  vi.mocked(setAuthorizationMode).mockResolvedValue({
    strategy_id: "s-1",
    authorization_mode: "autonomous",
    previous_mode: "approval_based",
    changed: true,
  });
  vi.mocked(saveAdmissionPolicy).mockResolvedValue({
    strategy_id: "s-1",
    account_id: "kite:paper",
    allocation_inr: 100000,
    per_instrument_notional_inr: null,
    gross_notional_inr: null,
    max_open_instruments: null,
    admissions_per_window: null,
    admission_window_seconds: null,
    daily_loss_budget_inr: null,
    updated_by: "app:admin",
  });
  vi.mocked(issueExecutionGrant).mockResolvedValue({
    grant_id: "g-1",
    owner_id: "app:admin",
    strategy_id: "s-1",
    canonical_strategy_id: "s-1",
    version_id: "v-1",
    version_number: 1,
    source_sha256: "a".repeat(64),
    account_id: "kite:paper",
    execution_environment: "paper",
    policy_hash: "b".repeat(64),
    policy_snapshot: POLICY,
    issued_by: "app:admin",
    issued_at: "2026-09-23T04:00:00+00:00",
    expires_at: null,
    status: "active",
    revoked_by: null,
    revoked_at: null,
    revocation_reason: null,
    superseded_by: null,
    superseded_at: null,
    supersession_reason: null,
    request_key: "internal-key",
    content_sha256: "d".repeat(64),
    created_at: null,
    idempotent: false,
  });
  vi.mocked(revokeExecutionGrant).mockResolvedValue({
    grant: {
      grant_id: "g-1",
      owner_id: "app:admin",
      strategy_id: "s-1",
      canonical_strategy_id: "s-1",
      version_id: "v-1",
      version_number: 1,
      source_sha256: "a".repeat(64),
      account_id: "kite:paper",
      execution_environment: "paper",
      policy_hash: "b".repeat(64),
      policy_snapshot: POLICY,
      issued_by: "app:admin",
      issued_at: "2026-09-23T04:00:00+00:00",
      expires_at: null,
      status: "revoked",
      revoked_by: "app:admin",
      revoked_at: "2026-09-23T05:00:00+00:00",
      revocation_reason: null,
      superseded_by: null,
      superseded_at: null,
      supersession_reason: null,
      request_key: "internal-key",
      content_sha256: "d".repeat(64),
      created_at: null,
      idempotent: false,
    },
    revoked_at: "2026-09-23T05:00:00+00:00",
  });
});

describe("hosted authorization panel", () => {
  it("says a version that cannot trade has nothing for anyone to approve", async () => {
    renderPanel();
    expect(
      await screen.findByTestId("authorization-no-trade-capability"),
    ).toHaveTextContent(/never asks anyone to approve a trade/i);
  });

  it("does not add the notice for a version that can trade", async () => {
    renderPanel([
      {
        ...VERSIONS[0],
        capabilities_snapshot: { data: true, trade: true, notify: false },
      },
    ]);
    await waitFor(() => expect(screen.getByTestId("grant-summary")).toBeInTheDocument());
    expect(screen.queryByTestId("authorization-no-trade-capability")).toBeNull();
  });

  it("issues nothing merely because the page rendered", async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByText(/review trades first/i)).toBeInTheDocument());
    expect(issueExecutionGrant).not.toHaveBeenCalled();
    expect(setAuthorizationMode).not.toHaveBeenCalled();
    expect(saveAdmissionPolicy).not.toHaveBeenCalled();
  });

  it("asks the owner for real limits and states exactly what a grant authorizes", async () => {
    renderPanel();
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByTestId("grant-summary")).toBeInTheDocument());

    // Without limits the button refuses: the platform never invents capital.
    await user.click(screen.getByRole("button", { name: /authorize automatic trading/i }));
    expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/your own limits/i));
    expect(issueExecutionGrant).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText(/allocation \(inr\)/i), "100000");
    await waitFor(() =>
      expect(screen.getByTestId("grant-summary")).toHaveTextContent(/allocation \(INR\) 100000/),
    );
    expect(screen.getByTestId("grant-summary")).toHaveTextContent(/v1 of opening range/);
    expect(screen.getByTestId("grant-summary")).toHaveTextContent(/kite:paper/);
    expect(screen.getByTestId("grant-summary")).toHaveTextContent(/Paper account/);

    await user.click(screen.getByRole("button", { name: /authorize automatic trading/i }));
    await waitFor(() => expect(setAuthorizationMode).toHaveBeenCalled());
    await waitFor(() => expect(saveAdmissionPolicy).toHaveBeenCalledWith("s-1", { allocation_inr: 100000 }));
    await waitFor(() =>
      expect(issueExecutionGrant).toHaveBeenCalledWith(
        "s-1",
        expect.objectContaining({
          version_id: "v-1",
          execution_environment: "paper",
          idempotency_key: expect.stringMatching(/^grant-/),
        }),
      ),
    );
  });

  it("shows an active grant with what it is bound to and revokes on confirmation", async () => {
    vi.mocked(fetchAuthorization).mockResolvedValue(
      status({
        authorization_mode: "autonomous",
        policy_concrete: true,
        grant_usable: true,
        active_grant: {
          grant_id: "g-1",
          owner_id: "app:admin",
          strategy_id: "s-1",
          canonical_strategy_id: "s-1",
          version_id: "v-1",
          version_number: 1,
          source_sha256: "a".repeat(64),
          account_id: "kite:paper",
          execution_environment: "paper",
          policy_hash: "b".repeat(64),
          policy_snapshot: {
            admission: { ...POLICY.admission!, allocation_inr: 100000 },
            protection: POLICY.protection,
          },
          issued_by: "app:admin",
          issued_at: "2026-09-23T04:00:00+00:00",
          expires_at: null,
          status: "active",
          revoked_by: null,
          revoked_at: null,
          revocation_reason: null,
          superseded_by: null,
          superseded_at: null,
          supersession_reason: null,
          request_key: "internal-key",
          content_sha256: "d".repeat(64),
          created_at: null,
          idempotent: false,
        },
      }),
    );
    renderPanel();
    expect(await screen.findByText(/authorization active/i)).toBeInTheDocument();
    expect(screen.getByText(/allocation 100000 INR/)).toBeInTheDocument();
    // Revocation is never described as cancelling an in-flight order.
    expect(screen.getByText(/does not cancel an order the broker already holds/i)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^revoke$/i }));
    await user.click(screen.getByRole("button", { name: /confirm revoke/i }));
    await waitFor(() =>
      expect(revokeExecutionGrant).toHaveBeenCalledWith("s-1", { grant_id: "g-1", reason: null }),
    );
  });
});
