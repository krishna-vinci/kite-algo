import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceProvider, useWorkspace } from "@/components/workspace/workspace-provider";

const fetchJournalEnvironmentsMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/journal/api", () => ({
  fetchJournalEnvironments: fetchJournalEnvironmentsMock,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => window.location.pathname,
  useSearchParams: (() => {
    let lastSearch = "";
    let lastParams = new URLSearchParams("");
    return () => {
      const currentSearch = window.location.search;
      if (currentSearch !== lastSearch) {
        lastSearch = currentSearch;
        lastParams = new URLSearchParams(currentSearch);
      }
      return lastParams;
    };
  })(),
}));

function WorkspaceProbe() {
  const {
    selectedMode,
    selectedEnvironmentId,
    setSelectedMode,
    selectedEnvironment,
  } = useWorkspace();

  return (
    <div>
      <div data-testid="mode">{selectedMode}</div>
      <div data-testid="env">{selectedEnvironmentId}</div>
      <div data-testid="env-mode">{selectedEnvironment?.mode ?? "none"}</div>
      <button onClick={() => setSelectedMode("live")}>set-live</button>
      <button onClick={() => setSelectedMode("paper")}>set-paper</button>
    </div>
  );
}

describe("WorkspaceProvider live/paper sync", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/journal");
    fetchJournalEnvironmentsMock.mockReset();
    fetchJournalEnvironmentsMock.mockResolvedValue([
      {
        id: "env-live-1",
        mode: "live",
        account_scope: "kite:live",
        display_name: "Live",
        broker_user_id: "user-live",
        paper_account_key: null,
        environment_epoch: 1,
        metadata: {},
      },
      {
        id: "env-paper-1",
        mode: "paper",
        account_scope: "kite:paper",
        display_name: "Paper",
        broker_user_id: null,
        paper_account_key: "kite:paper",
        environment_epoch: 1,
        metadata: {},
      },
    ]);
  });

  it("hydrates state from direct paper URL scope", async () => {
    window.history.replaceState({}, "", "/journal?env=env-paper-1&mode=paper");

    render(
      <WorkspaceProvider>
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("mode").textContent).toBe("paper"));
    await waitFor(() => expect(screen.getByTestId("env").textContent).toBe("env-paper-1"));
    await waitFor(() => expect(screen.getByTestId("env-mode").textContent).toBe("paper"));
  });

  it("mode switch auto-selects matching environment and canonical URL", async () => {
    window.history.replaceState({}, "", "/journal?env=env-paper-1&mode=paper");

    render(
      <WorkspaceProvider>
        <WorkspaceProbe />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("env").textContent).toBe("env-paper-1"));

    fireEvent.click(screen.getByRole("button", { name: "set-live" }));

    await waitFor(() => expect(screen.getByTestId("mode").textContent).toBe("live"));
    await waitFor(() => expect(screen.getByTestId("env").textContent).toBe("env-live-1"));
    await waitFor(() => expect(window.location.search).toContain("env=env-live-1"));
    await waitFor(() => expect(window.location.search).toContain("mode=live"));
  });
});
