import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ReactElement } from "react";
import { AlgoWorkerAccessPanel } from "./algo-worker-access-panel";

const apiMocks = vi.hoisted(() => ({
  listAlgoWorkerTokens: vi.fn(),
  getKiteProfile: vi.fn(),
  createAlgoWorkerToken: vi.fn(),
  revokeAlgoWorkerToken: vi.fn(),
}));

vi.mock("@/lib/algo-workers/api", () => apiMocks);

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

describe("AlgoWorkerAccessPanel", () => {
  it("describes live-bound tokens as usable for paper scopes too", async () => {
    apiMocks.listAlgoWorkerTokens.mockResolvedValueOnce([
      {
        tokenId: "worker-1",
        name: "multi-mode",
        accountScope: "kite:XJJ446",
        allowedModes: ["paper", "dry_run", "live"],
        allowedActions: ["runs:create"],
        allowedTemplates: [],
        status: "active",
        createdAt: null,
        expiresAt: null,
        lastUsedAt: null,
      },
    ]);
    apiMocks.getKiteProfile.mockResolvedValueOnce({ userId: "XJJ446", userName: "User", raw: {} });

    renderWithQueryClient(<AlgoWorkerAccessPanel />);

    expect(await screen.findByText("Scope: Live kite:XJJ446 + any paper scope")).toBeInTheDocument();
  });

  it("explains that live-enabled tokens can also be reused for paper and dry-run", async () => {
    apiMocks.listAlgoWorkerTokens.mockResolvedValueOnce([]);
    apiMocks.getKiteProfile.mockResolvedValueOnce({ userId: "XJJ446", userName: "User", raw: {} });
    const user = userEvent.setup();

    renderWithQueryClient(<AlgoWorkerAccessPanel />);

    const liveModeLabel = await screen.findByText("Live");
    const liveModeCheckbox = liveModeLabel.closest("label")?.querySelector('input[type="checkbox"]');
    expect(liveModeCheckbox).not.toBeNull();
    await user.click(liveModeCheckbox as HTMLInputElement);

    expect(await screen.findByText(/The same token can still create paper or dry-run runs/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generate paper \+ live token/i })).toBeInTheDocument();
  });
});
