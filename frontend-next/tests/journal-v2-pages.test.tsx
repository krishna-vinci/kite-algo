import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import JournalAnalyticsPage from "@/app/(app)/journal/analytics/page";
import JournalEpisodeDetailPage from "@/app/(app)/journal/episodes/[episodeId]/page";
import JournalEpisodesPage from "@/app/(app)/journal/episodes/page";
import JournalOverviewPage from "@/app/(app)/journal/page";
import JournalNotesPage from "@/app/(app)/journal/notes/page";
import JournalStrategiesPage from "@/app/(app)/journal/strategies/page";
import JournalUnresolvedPage from "@/app/(app)/journal/unresolved/page";
import { EnvironmentSelector } from "@/components/journal/environment-selector";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalWorkspaceProvider, useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { renderWithQueryClient } from "@/tests/render-with-query-client";

const fetchJournalEnvironmentsMock = vi.hoisted(() => vi.fn());
const fetchJournalEpisodesMock = vi.hoisted(() => vi.fn());
const fetchJournalEpisodeMock = vi.hoisted(() => vi.fn());
const fetchJournalTimelineMock = vi.hoisted(() => vi.fn());
const fetchJournalNotesMock = vi.hoisted(() => vi.fn());
const fetchJournalNoteRevisionsMock = vi.hoisted(() => vi.fn());
const createJournalNoteMock = vi.hoisted(() => vi.fn());
const fetchJournalV2AnalyticsSummaryMock = vi.hoisted(() => vi.fn());
const fetchJournalV2AnalyticsStrategiesMock = vi.hoisted(() => vi.fn());
const fetchJournalV2PaperLiveComparisonMock = vi.hoisted(() => vi.fn());
const fetchJournalV2UnresolvedMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/journal/api", () => ({
  fetchJournalEnvironments: fetchJournalEnvironmentsMock,
  fetchJournalEpisodes: fetchJournalEpisodesMock,
  fetchJournalEpisode: fetchJournalEpisodeMock,
  fetchJournalTimeline: fetchJournalTimelineMock,
  fetchJournalNotes: fetchJournalNotesMock,
  fetchJournalNoteRevisions: fetchJournalNoteRevisionsMock,
  createJournalNote: createJournalNoteMock,
  fetchJournalV2AnalyticsSummary: fetchJournalV2AnalyticsSummaryMock,
  fetchJournalV2AnalyticsStrategies: fetchJournalV2AnalyticsStrategiesMock,
  fetchJournalV2PaperLiveComparison: fetchJournalV2PaperLiveComparisonMock,
  fetchJournalV2Unresolved: fetchJournalV2UnresolvedMock,
}));

describe("journal v2 pages", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.replaceState({}, "", "/");

    fetchJournalEnvironmentsMock.mockReset();
    fetchJournalEpisodesMock.mockReset();
    fetchJournalEpisodeMock.mockReset();
    fetchJournalTimelineMock.mockReset();
    fetchJournalNotesMock.mockReset();
    fetchJournalNoteRevisionsMock.mockReset();
    createJournalNoteMock.mockReset();
    fetchJournalV2AnalyticsSummaryMock.mockReset();
    fetchJournalV2AnalyticsStrategiesMock.mockReset();
    fetchJournalV2PaperLiveComparisonMock.mockReset();
    fetchJournalV2UnresolvedMock.mockReset();

    fetchJournalEnvironmentsMock.mockResolvedValue([
      {
        id: "env-1",
        mode: "paper",
        account_scope: "kite:paper-e2e",
        display_name: "Paper E2E",
        broker_user_id: null,
        paper_account_key: "kite:paper-e2e",
        environment_epoch: 1,
        metadata: {},
      },
    ]);
    fetchJournalEpisodesMock.mockResolvedValue([]);
    fetchJournalEpisodeMock.mockResolvedValue({
      id: "ep-1",
      environment_id: "env-1",
      execution_context_id: "ctx-1",
      episode_seq: 1,
      status: "open",
      opened_at: "2026-05-01T10:00:00Z",
      closed_at: null,
      metadata: {},
    });
    fetchJournalTimelineMock.mockResolvedValue([]);
    fetchJournalNotesMock.mockResolvedValue([]);
    fetchJournalNoteRevisionsMock.mockResolvedValue([]);
    createJournalNoteMock.mockResolvedValue({
      id: "note-1",
      environment_id: "env-1",
      subject_type: "environment",
      subject_id: "env-1",
      episode_id: null,
      note_type: "review",
      title: "Review note",
      body_markdown: "body",
      tags: [],
      updated_at: "2026-05-01T10:05:00Z",
    });
    fetchJournalV2AnalyticsSummaryMock.mockResolvedValue({
      environment_id: "env-1",
      closed_episode_count: 0,
      metrics: { closed_episode_count: 0, net_pnl: 0, total_charges: 0, win_rate: 0 },
    });
    fetchJournalV2AnalyticsStrategiesMock.mockResolvedValue({ environment_id: "env-1", items: [], count: 0 });
    fetchJournalV2PaperLiveComparisonMock.mockResolvedValue({
      template_id: "tmpl-1",
      paper_environment_id: "env-1",
      live_environment_id: "env-live-1",
      paper: { closed_episode_count: 0, net_pnl: 0, total_charges: 0 },
      live: { closed_episode_count: 0, net_pnl: 0, total_charges: 0 },
      combined: null,
    });
    fetchJournalV2UnresolvedMock.mockResolvedValue({ environment_id: "env-1", items: [], count: 0 });
  });

  function WorkspaceEnvSelector() {
    const { environments, selectedEnvironmentId, setSelectedEnvironmentId } = useJournalWorkspace();
    return (
      <EnvironmentSelector
        environments={environments}
        selectedEnvironmentId={selectedEnvironmentId}
        onSelectEnvironment={setSelectedEnvironmentId}
      />
    );
  }

  it("preserves selected Journal environment across shared workspace renders", async () => {
    window.history.pushState({}, "", "/journal?environment_id=env-1");

    render(
      <JournalWorkspaceProvider>
        <WorkspaceEnvSelector />
        <WorkspaceEnvSelector />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalEnvironmentsMock).toHaveBeenCalled());
    expect(screen.getAllByRole("combobox", { name: /Journal V2 environment/i })).toHaveLength(2);
    for (const selector of screen.getAllByRole("combobox", { name: /Journal V2 environment/i })) {
      expect(selector).toHaveValue("env-1");
    }
  });

  it("shows Journal V2 overview panels from shared environment state", async () => {
    window.history.pushState({}, "", "/journal?environment_id=env-1");
    fetchJournalEpisodesMock.mockResolvedValue([
      {
        id: "ep-1",
        environment_id: "env-1",
        execution_context_id: "ctx-1",
        episode_seq: 1,
        status: "closed",
        opened_at: "2026-05-01T10:00:00Z",
        closed_at: "2026-05-01T10:05:00Z",
        metadata: {},
      },
    ]);

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalOverviewPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalV2AnalyticsSummaryMock).toHaveBeenCalledWith("env-1"));
    expect(screen.getByText(/Environment-scoped review/i)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /live/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /paper/i })).toBeInTheDocument();
    expect(screen.getByText(/Recent episodes/i)).toBeInTheDocument();
    expect(screen.getByText(/Unresolved queue/i)).toBeInTheDocument();
    expect(screen.queryByText(/dev notice/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Combined P&L/i)).not.toBeInTheDocument();
  });

  it("renders episode ledger entries linked with environment_id", async () => {
    window.history.pushState({}, "", "/journal/episodes?environment_id=env-1");
    fetchJournalEpisodesMock.mockResolvedValue([
      {
        id: "ep-1",
        environment_id: "env-1",
        execution_context_id: "ctx-1",
        episode_seq: 1,
        status: "open",
        opened_at: "2026-05-01T10:00:00Z",
        closed_at: null,
        metadata: {},
      },
    ]);

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalEpisodesPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalEpisodesMock).toHaveBeenCalledWith({ environment_id: "env-1" }));
    expect(screen.getByRole("link", { name: /Episode #1/i })).toHaveAttribute(
      "href",
      expect.stringContaining("environment_id=env-1"),
    );
  });

  it("renders V2 Journal nav tabs and preserves environment query in links", async () => {
    window.history.pushState({}, "", "/journal?environment_id=env-1");

    render(
      <JournalWorkspaceProvider>
        <JournalNav />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalEnvironmentsMock).toHaveBeenCalled());
    expect(screen.getByRole("link", { name: /Episodes/i })).toHaveAttribute(
      "href",
      expect.stringContaining("environment_id=env-1"),
    );
    expect(screen.getByRole("link", { name: /Unresolved/i })).toBeInTheDocument();
  });

  it("renders episode review workspace with timeline and note workflow", async () => {
    window.history.pushState({}, "", "/journal/episodes/ep-1?environment_id=env-1");
    fetchJournalTimelineMock.mockResolvedValue([
      {
        id: "evt-1",
        environment_id: "env-1",
        episode_id: "ep-1",
        execution_context_id: "ctx-1",
        subject_type: "episode",
        subject_id: "ep-1",
        event_type: "episode_opened",
        channel: "entry",
        actor_type: "system",
        correlation_id: null,
        causation_id: null,
        occurred_at: "2026-05-01T10:00:00Z",
        payload: {},
      },
    ]);
    fetchJournalNotesMock.mockResolvedValue([
      {
        id: "note-1",
        environment_id: "env-1",
        subject_type: "episode",
        subject_id: "ep-1",
        episode_id: "ep-1",
        note_type: "post_exit_review",
        title: "Review",
        body_markdown: "# Review",
        tags: [],
        updated_at: "2026-05-01T10:05:00Z",
      },
    ]);

    render(
      <JournalWorkspaceProvider>
        <JournalEpisodeDetailPage params={{ episodeId: "ep-1" }} />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalEpisodeMock).toHaveBeenCalledWith("ep-1", "env-1"));
    expect(screen.getByRole("heading", { name: /Episode #1/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Episode activity/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Episode note/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByDisplayValue("# Review")).toBeInTheDocument());
    await waitFor(() => expect(fetchJournalNoteRevisionsMock).toHaveBeenCalledWith("note-1", "env-1"));
    expect(screen.getByRole("heading", { name: /Note revisions/i })).toBeInTheDocument();
  });

  it("shows backend-backed notes archive and unresolved queue from shared Journal environment", async () => {
    window.history.pushState({}, "", "/journal/notes?environment_id=env-1");
    fetchJournalNotesMock.mockResolvedValue([
      {
        id: "note-1",
        environment_id: "env-1",
        subject_type: "episode",
        subject_id: "ep-1",
        episode_id: "ep-1",
        note_type: "post_exit_review",
        title: "Review",
        body_markdown: "# Review",
        tags: [],
        updated_at: "2026-05-01T10:05:00Z",
      },
    ]);

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalNotesPage />
        <JournalUnresolvedPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalNotesMock).toHaveBeenCalledWith({ environment_id: "env-1", limit: 50 }));
    await waitFor(() => expect(fetchJournalV2UnresolvedMock).toHaveBeenCalledWith("env-1"));
    expect(screen.getByRole("heading", { name: /Notes archive/i })).toBeInTheDocument();
    expect(screen.getByText(/Create environment note/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Unresolved queue/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/Markdown note/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save note/i })).toBeInTheDocument();
    expect(screen.queryByText(/Draft locally/i)).not.toBeInTheDocument();
  });

  it("creates a real environment note and refreshes the list", async () => {
    window.history.pushState({}, "", "/journal/notes?environment_id=env-1");
    fetchJournalNotesMock
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          id: "note-1",
          environment_id: "env-1",
          subject_type: "environment",
          subject_id: "env-1",
          episode_id: null,
          note_type: "review",
          title: "Review note",
          body_markdown: "First review",
          tags: ["tag-a"],
          updated_at: "2026-05-01T10:05:00Z",
        },
      ]);

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalNotesPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalNotesMock).toHaveBeenCalledWith({ environment_id: "env-1", limit: 50 }));

    fireEvent.change(screen.getByLabelText(/Markdown note/i), { target: { value: "First review" } });
    fireEvent.change(screen.getByLabelText(/Note tags/i), { target: { value: "tag-a" } });
    fireEvent.click(screen.getByRole("button", { name: /Save note/i }));

    await waitFor(() =>
      expect(createJournalNoteMock).toHaveBeenCalledWith(
        expect.objectContaining({
          environment_id: "env-1",
          subject_type: "environment",
          subject_id: "env-1",
          note_type: "review",
          body_markdown: "First review",
          tags: ["tag-a"],
        }),
      ),
    );
    await waitFor(() => expect(fetchJournalNotesMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/Review note/i)).toBeInTheDocument();
  });

  it("shows analytics page from shared environment selection without mixed totals", async () => {
    window.history.pushState({}, "", "/journal/analytics?environment_id=env-1");
    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalAnalyticsPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalV2AnalyticsSummaryMock).toHaveBeenCalledWith("env-1"));
    expect(screen.queryByText("Combined P&L")).not.toBeInTheDocument();
    expect(screen.getByText(/Paper vs Live Comparison/i)).toBeInTheDocument();
    expect(screen.getByText(/Select a template plus explicit paper and live environments to compare them separately./i)).toBeInTheDocument();
  });

  it("shows strategy scorecards with formatted metrics", async () => {
    window.history.pushState({}, "", "/journal/strategies?environment_id=env-1");
    fetchJournalV2AnalyticsStrategiesMock.mockResolvedValue({
      environment_id: "env-1",
      count: 1,
      items: [
        {
          template_id: "tmpl-1",
          strategy_family: "options_strategy",
          display_name: "Breakout Review",
          metrics: { closed_episode_count: 12, net_pnl: 15234, total_charges: 412, win_rate: 58.3 },
        },
      ],
    });

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalStrategiesPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalV2AnalyticsStrategiesMock).toHaveBeenCalledWith("env-1"));
    expect(screen.getByText(/Strategy Template Scorecards/i)).toBeInTheDocument();
    expect(screen.getByText(/Net P&L: ₹15,234/i)).toBeInTheDocument();
    expect(screen.getByText(/Win rate: 58.3%/i)).toBeInTheDocument();
  });

  it("shows reusable unresolved queue guidance on the full page", async () => {
    window.history.pushState({}, "", "/journal/unresolved?environment_id=env-1");
    fetchJournalV2UnresolvedMock.mockResolvedValue({
      environment_id: "env-1",
      count: 1,
      items: [
        {
          id: "uq-1",
          environment_id: "env-1",
          execution_context_id: "ctx-1",
          source_system: "broker_import",
          reason: "Missing strategy identity",
          raw_identity: {},
          candidate_mappings: [{ template_id: "tmpl-1" }],
          metadata: {},
          status: "pending",
          created_at: "2026-05-01T10:00:00Z",
          resolved_at: null,
        },
      ],
    });

    renderWithQueryClient(
      <JournalWorkspaceProvider>
        <JournalUnresolvedPage />
      </JournalWorkspaceProvider>,
    );

    await waitFor(() => expect(fetchJournalV2UnresolvedMock).toHaveBeenCalledWith("env-1"));
    expect(screen.getByText(/Resolve actions are shown only/i)).toBeInTheDocument();
  });
});
