import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JournalShell } from "@/components/journal/journal-shell";
import { WorkspaceProvider } from "@/components/workspace/workspace-provider";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

const fetchJournalEnvironmentsMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/journal/api", () => ({
  fetchJournalEnvironments: fetchJournalEnvironmentsMock,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => window.location.pathname,
}));

function withWorkspace(ui: React.ReactElement) {
  return renderWithQueryClient(<WorkspaceProvider>{ui}</WorkspaceProvider>);
}

describe("JournalShell scope preservation", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");
    fetchJournalEnvironmentsMock.mockReset();
    fetchJournalEnvironmentsMock.mockResolvedValue([
      {
        id: "env-live-1",
        mode: "live",
        account_scope: "kite:live",
        display_name: "Live",
        broker_user_id: "user-1",
        paper_account_key: null,
        environment_epoch: 1,
        metadata: {},
      },
    ]);
  });

  it("hydrates bare /journal URL and preserves scope in analytics switch link", async () => {
    window.history.pushState({}, "", "/journal");

    withWorkspace(
      <JournalShell>
        <div>content</div>
      </JournalShell>,
    );

    const analyticsLink = await screen.findByRole("tab", { name: /Analytics/i });

    await waitFor(() => expect(window.location.search).toContain("env=env-live-1"));
    await waitFor(() => expect(window.location.search).toContain("mode=live"));

    expect(analyticsLink).toHaveAttribute("href", expect.stringContaining("env=env-live-1"));
    expect(analyticsLink).toHaveAttribute("href", expect.stringContaining("mode=live"));
  });

  it("preserves review context when returning from analytics", async () => {
    window.history.pushState(
      {},
      "",
      "/journal/analytics?env=env-live-1&mode=live&date=2026-05-06&period=month&review=week",
    );

    withWorkspace(
      <JournalShell>
        <div>content</div>
      </JournalShell>,
    );

    const reviewLink = await screen.findByRole("tab", { name: /Review/i });
    expect(reviewLink).toHaveAttribute("href", expect.stringContaining("/journal/week?"));
    expect(reviewLink).toHaveAttribute("href", expect.stringContaining("env=env-live-1"));
    expect(reviewLink).toHaveAttribute("href", expect.stringContaining("mode=live"));
  });
});
