import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import SettingsPage from "@/app/(app)/settings/page";
import StrategiesPage from "@/app/(app)/strategies/page";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

const replaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/settings",
}));

vi.mock("@/features/trading/hooks/use-trading-console-data", () => ({
  useTradingConsoleData: () => ({
    runtime: {
      brokerConnected: true,
      brokerStatus: "connected",
      brokerMode: "system",
      brokerLastSuccessAt: null,
      brokerLastFailureAt: null,
      brokerLastError: null,
      brokerNextRefreshAt: null,
      websocketStatus: "connected",
      paperAvailable: true,
      appAuthenticated: true,
    },
    quotes: [],
    paper: {
      accountScope: "default",
      account: {
        accountScope: "default",
        currency: "INR",
        startingBalance: 100000,
        availableFunds: 81000,
        blockedFunds: 19000,
        realizedPnl: 700,
        unrealizedPnl: 300,
        openPositionCount: 2,
      },
      activeStrategyCount: 1,
      strategies: [
        {
          strategyRunId: "paper-1",
          strategyId: "paper-1",
          displayName: "Paper Iron Condor",
          strategyTag: "options_runtime",
          algoInstanceId: "algo-paper-1",
          mode: "paper",
          status: "open",
          isOpen: true,
          openLegCount: 4,
          realizedPnl: 700,
          unrealizedPnl: 300,
          marginInUse: 19000,
          summaryFields: [],
          capabilities: { canEditRisk: true, editRiskReason: null, canExitStrategy: true, exitReason: null, allowedActions: ["edit_risk", "exit_strategy"], riskSchema: [] },
          positions: [],
          orders: [],
          trades: [],
          timeline: [],
        },
      ],
    },
    broker: { positions: [], activeCount: 0 },
    control: { generatedAt: null, totals: { strategyCount: 1, openStrategyCount: 1, positionCount: 0, staleWorkerCount: 0, realizedPnl: 0, unrealizedPnl: 0, netPnl: 0 }, strategies: [], unattributed: { displayName: "Manual / unattributed broker exposure", positions: [], orders: [], realizedPnl: 0, unrealizedPnl: 0, netPnl: 0 } },
  }),
}));

vi.mock("@/components/workspace/workspace-provider", () => ({
  useOptionalWorkspace: () => ({
    environments: [
      {
        id: "env-live",
        mode: "live",
        account_scope: "kite:LIVE-USER",
        display_name: "Live Primary",
        broker_user_id: "LIVE-USER",
        paper_account_key: null,
        environment_epoch: 1,
        metadata: {},
      },
      {
        id: "env-paper",
        mode: "paper",
        account_scope: "kite:paper-a",
        display_name: "Paper Alpha",
        broker_user_id: null,
        paper_account_key: "kite:paper-a",
        environment_epoch: 2,
        metadata: {},
      },
    ],
    environmentsLoading: false,
    environmentsError: null,
    selectedMode: "paper",
    selectedEnvironmentId: "env-paper",
    selectedEnvironment: {
      id: "env-paper",
      mode: "paper",
      account_scope: "kite:paper-a",
      display_name: "Paper Alpha",
      broker_user_id: null,
      paper_account_key: "kite:paper-a",
      environment_epoch: 2,
      metadata: {},
    },
  }),
}));

describe("secondary reference pages", () => {
  it("renders the paper tab inside the shared strategies workspace", () => {
    window.history.replaceState({}, "", "/strategies?mode=paper");
    renderWithQueryClient(<StrategiesPage />);

    expect(screen.getByRole("heading", { name: /^strategies$/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /paper/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(/paper iron condor/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /exit/i })).toBeInTheDocument();
  });

  it("renders the settings page with a tab bar navigation", () => {
    renderWithQueryClient(<SettingsPage />);

    // Tab bar is the single navigation model — role="tablist" is the exposed ARIA role
    expect(screen.getByRole("tablist", { name: /settings sections/i })).toBeInTheDocument();

    // All four tabs are present
    expect(screen.getByRole("tab", { name: /reference data/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /worker access/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /workspace/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /apis/i })).toBeInTheDocument();
  });

  it("defaults to the Reference data tab and shows index baselines content", () => {
    renderWithQueryClient(<SettingsPage />);

    const referenceTab = screen.getByRole("tab", { name: /reference data/i });
    expect(referenceTab).toHaveAttribute("aria-selected", "true");

    // IndexBaselinesPanel renders with heading "Index baselines"
    expect(screen.getByRole("heading", { name: /index baselines/i })).toBeInTheDocument();
  });

  it("switches to the APIs tab and shows the placeholder panel", async () => {
    const user = userEvent.setup();
    renderWithQueryClient(<SettingsPage />);

    await user.click(screen.getByRole("tab", { name: /apis/i }));

    expect(screen.getByRole("tab", { name: /apis/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: /settings apis/i })).toBeInTheDocument();
    expect(screen.getByText(/static placeholder values have been removed/i)).toBeInTheDocument();
  });

  it("switches to the Workspace tab and shows environment context", async () => {
    const user = userEvent.setup();
    renderWithQueryClient(<SettingsPage />);

    await user.click(screen.getByRole("tab", { name: /workspace/i }));

    expect(screen.getByRole("heading", { name: /workspace context/i })).toBeInTheDocument();
    expect(screen.getAllByText(/Paper Alpha/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Live Primary/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/All environments \(2\)/i)).toBeInTheDocument();
  });

  it("supports keyboard navigation across settings tabs", async () => {
    const user = userEvent.setup();
    renderWithQueryClient(<SettingsPage />);

    const referenceTab = screen.getByRole("tab", { name: /reference data/i });
    referenceTab.focus();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /worker access/i })).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: /apis/i })).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: /reference data/i })).toHaveAttribute("aria-selected", "true");
  });

  it("does not render anchor links or a sticky sidebar map", () => {
    renderWithQueryClient(<SettingsPage />);

    // No anchor href links to section IDs — navigation is tab-only
    const allLinks = screen.queryAllByRole("link");
    const anchorHrefs = allLinks.map((el) => el.getAttribute("href")).filter((h) => h?.startsWith("#"));
    expect(anchorHrefs).toHaveLength(0);
  });
});
