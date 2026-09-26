import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ReactElement } from "react";
import { ApprovalsInboxPage } from "./approvals-inbox-page";

const apiMocks = vi.hoisted(() => ({
  fetchPendingApprovals: vi.fn(),
  approveExecutionRequest: vi.fn(),
  rejectExecutionRequest: vi.fn(),
}));

vi.mock("@/lib/hosted-strategies/api", () => apiMocks);

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function renderWithQueryClient(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("ApprovalsInboxPage", () => {
  it("lists pending approvals and approves the right strategy/request pair", async () => {
    apiMocks.fetchPendingApprovals.mockResolvedValue({
      count: 1,
      items: [
        {
          strategy_id: "strat-1",
          strategy_name: "Mean Reversion",
          request_id: "req-1",
          plan_id: "plan-1",
          environment: "live",
          summary: "Buy 2 lots NIFTY 25000 CE",
          created_at: "2026-09-26T09:00:00Z",
          expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
        },
      ],
    });
    apiMocks.approveExecutionRequest.mockResolvedValue({});
    const user = userEvent.setup();

    renderWithQueryClient(<ApprovalsInboxPage />);

    expect(await screen.findByText("Mean Reversion")).toBeInTheDocument();
    expect(screen.getByText("Buy 2 lots NIFTY 25000 CE")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(apiMocks.approveExecutionRequest).toHaveBeenCalledWith("strat-1", "req-1");
    });
  });

  it("shows an empty state when nothing is waiting", async () => {
    apiMocks.fetchPendingApprovals.mockResolvedValue({ count: 0, items: [] });

    renderWithQueryClient(<ApprovalsInboxPage />);

    expect(await screen.findByText("Nothing is waiting for your approval.")).toBeInTheDocument();
  });
});
