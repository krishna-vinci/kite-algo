import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createJournalNote,
  fetchJournalEnvironments,
  fetchJournalEpisode,
  fetchJournalEpisodes,
  fetchJournalV2AnalyticsSummary,
  fetchJournalV2PaperLiveComparison,
  fetchJournalNotes,
  fetchJournalTimeline,
  updateJournalNote,
} from "@/lib/journal/api";

const apiFetchMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api/client", () => ({
  apiFetch: apiFetchMock,
}));

describe("journal v2 API helpers", () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("fetches v2 environments", async () => {
    apiFetchMock.mockResolvedValueOnce({
      items: [{ id: "env-1", mode: "paper", account_scope: "kite:paper-e2e", environment_epoch: 1 }],
    });

    const items = await fetchJournalEnvironments();

    expect(items[0]).toMatchObject({ id: "env-1", mode: "paper", account_scope: "kite:paper-e2e" });
    expect(apiFetchMock).toHaveBeenCalledWith("/api/journal/v2/environments");
  });

  it("requires environment_id for v2 list helpers", async () => {
    await expect(fetchJournalEpisodes({ environment_id: "" })).rejects.toThrow("fetchJournalEpisodes requires environment_id");
    await expect(fetchJournalNotes({ environment_id: "" })).rejects.toThrow("fetchJournalNotes requires environment_id");
  });

  it("fetches episodes and timeline using v2 endpoints", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ items: [{ id: "ep-1", environment_id: "env-1", execution_context_id: "ctx-1", episode_seq: 1, status: "open" }] })
      .mockResolvedValueOnce({ id: "ep-1", environment_id: "env-1", execution_context_id: "ctx-1", episode_seq: 1, status: "open" })
      .mockResolvedValueOnce({ items: [{ id: "evt-1", environment_id: "env-1", subject_type: "episode", subject_id: "ep-1", event_type: "episode_opened" }] });

    const episodes = await fetchJournalEpisodes({ environment_id: "env-1" });
    const detail = await fetchJournalEpisode("ep-1", "env-1");
    const timeline = await fetchJournalTimeline("ep-1", "env-1");

    expect(episodes[0].id).toBe("ep-1");
    expect(detail.id).toBe("ep-1");
    expect(timeline[0].event_type).toBe("episode_opened");
    expect(apiFetchMock).toHaveBeenNthCalledWith(2, "/api/journal/v2/episodes/ep-1?environment_id=env-1");
    expect(apiFetchMock).toHaveBeenNthCalledWith(3, "/api/journal/v2/episodes/ep-1/timeline?environment_id=env-1");
  });

  it("creates and updates notes", async () => {
    apiFetchMock
      .mockResolvedValueOnce({
        id: "note-1",
        environment_id: "env-1",
        subject_type: "episode",
        subject_id: "ep-1",
        episode_id: "ep-1",
        note_type: "thesis",
        title: "Entry",
        body_markdown: "# Plan",
        tags: [],
        updated_at: "2026-05-01T10:00:00+00:00",
      })
      .mockResolvedValueOnce({
        id: "note-1",
        environment_id: "env-1",
        subject_type: "episode",
        subject_id: "ep-1",
        episode_id: "ep-1",
        note_type: "thesis",
        title: "Updated",
        body_markdown: "# Plan",
        tags: [],
        updated_at: "2026-05-01T11:00:00+00:00",
      });

    const created = await createJournalNote({
      environment_id: "env-1",
      subject_type: "episode",
      subject_id: "ep-1",
      episode_id: "ep-1",
      note_type: "thesis",
      title: "Entry",
      body_markdown: "# Plan",
    });

    const updated = await updateJournalNote("note-1", {
      environment_id: "env-1",
      subject_type: "episode",
      subject_id: "ep-1",
      title: "Updated",
    });

    expect(created.id).toBe("note-1");
    expect(updated.title).toBe("Updated");
    expect(apiFetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/journal/v2/notes",
      expect.objectContaining({ method: "POST" }),
    );
    expect(apiFetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/journal/v2/notes/note-1",
      expect.objectContaining({ method: "PATCH" }),
    );
  });

  it("creates a Journal V2 environment note", async () => {
    apiFetchMock.mockResolvedValueOnce({
      id: "note-1",
      environment_id: "env-1",
      subject_type: "environment",
      subject_id: "env-1",
      note_type: "review",
      title: "Review note",
      body_markdown: "body",
      tags: ["tag"],
      updated_at: "2026-05-03T00:00:00Z",
    });

    const note = await createJournalNote({
      environment_id: "env-1",
      subject_type: "environment",
      subject_id: "env-1",
      note_type: "review",
      title: "Review note",
      body_markdown: "body",
      tags: ["tag"],
    });

    expect(note.id).toBe("note-1");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/notes",
      expect.objectContaining({
        method: "POST",
        json: expect.objectContaining({ subject_type: "environment", subject_id: "env-1" }),
      }),
    );
  });

  it("fetches analytics summary with explicit environment", async () => {
    apiFetchMock.mockResolvedValueOnce({ environment_id: "env-1", closed_episode_count: 0, metrics: {} });

    const payload = await fetchJournalV2AnalyticsSummary("env-1");

    expect(payload.environment_id).toBe("env-1");
    expect(apiFetchMock).toHaveBeenCalledWith("/api/journal/v2/analytics/summary?environment_id=env-1");
  });

  it("paper-live comparison payload keeps combined null", async () => {
    apiFetchMock.mockResolvedValueOnce({
      template_id: "tmpl-1",
      paper_environment_id: "00000000-0000-4000-8000-000000000001",
      live_environment_id: "00000000-0000-4000-8000-000000000002",
      paper: { net_pnl: 10 },
      live: { net_pnl: 5 },
      combined: null,
    });

    const payload = await fetchJournalV2PaperLiveComparison({
      template_id: "tmpl-1",
      paper_environment_id: "00000000-0000-4000-8000-000000000001",
      live_environment_id: "00000000-0000-4000-8000-000000000002",
    });

    expect(payload.template_id).toBe("tmpl-1");
    expect(payload.combined).toBeNull();
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/journal/v2/analytics/compare-paper-live?template_id=tmpl-1&paper_environment_id=00000000-0000-4000-8000-000000000001&live_environment_id=00000000-0000-4000-8000-000000000002",
    );
  });
});
