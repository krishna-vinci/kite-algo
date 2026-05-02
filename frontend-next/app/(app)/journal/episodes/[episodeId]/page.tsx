"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { EpisodeTimeline } from "@/components/journal/episode-timeline";
import { MarkdownNoteEditor } from "@/components/journal/markdown-note-editor";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import {
  createJournalNote,
  fetchJournalEpisode,
  fetchJournalNoteRevisions,
  fetchJournalNotes,
  fetchJournalTimeline,
  updateJournalNote,
} from "@/lib/journal/api";
import type { AnalysisPeriod, JournalEpisode, JournalNote, JournalNoteRevision, JournalTimelineEvent } from "@/lib/journal/types";

function formatDateTime(value: string | null) {
  if (!value) {
    return "Still open";
  }
  return new Date(value).toLocaleString();
}

function getEpisodeStatusTone(episode: JournalEpisode | null) {
  if (!episode) {
    return "neutral" as const;
  }
  if (episode.closed_at) {
    return "positive" as const;
  }
  return "warning" as const;
}

type EpisodeDetailPageProps = {
  params: {
    episodeId: string;
  };
};

export default function JournalEpisodeDetailPage({ params }: EpisodeDetailPageProps) {
  let workspaceEnvironmentId = "";
  try {
    workspaceEnvironmentId = useJournalWorkspace().selectedEnvironmentId;
  } catch {
    workspaceEnvironmentId = "";
  }

  const [environmentId, setEnvironmentId] = useState("");

  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const [episode, setEpisode] = useState<JournalEpisode | null>(null);
  const [episodeLoading, setEpisodeLoading] = useState(false);
  const [episodeError, setEpisodeError] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<JournalTimelineEvent[]>([]);
  const [timelineLoading, setTimelineLoading] = useState(false);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [activeNote, setActiveNote] = useState<JournalNote | null>(null);
  const [noteLoading, setNoteLoading] = useState(false);
  const [noteError, setNoteError] = useState<string | null>(null);
  const [noteRevisions, setNoteRevisions] = useState<JournalNoteRevision[]>([]);
  const [revisionsLoading, setRevisionsLoading] = useState(false);
  const [revisionsError, setRevisionsError] = useState<string | null>(null);

  useEffect(() => {
    const fromUrl =
      typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("environment_id")?.trim() ?? "" : "";
    setEnvironmentId(fromUrl || workspaceEnvironmentId);
  }, [workspaceEnvironmentId]);

  useEffect(() => {
    let closed = false;
      if (!environmentId) {
        setEpisode(null);
        setTimeline([]);
        setActiveNote(null);
        setNoteRevisions([]);
        return () => {
          closed = true;
        };
    }

    setEpisodeLoading(true);
    setTimelineLoading(true);
    setNoteLoading(true);

    fetchJournalEpisode(params.episodeId, environmentId)
      .then((item) => {
        if (!closed) {
          setEpisode(item);
          setEpisodeError(null);
        }
      })
      .catch((error) => {
        if (!closed) {
          setEpisode(null);
          setEpisodeError(error instanceof Error ? error.message : "Failed to load episode");
        }
      })
      .finally(() => {
        if (!closed) {
          setEpisodeLoading(false);
        }
      });

    fetchJournalTimeline(params.episodeId, environmentId)
      .then((items) => {
        if (!closed) {
          setTimeline(items);
          setTimelineError(null);
        }
      })
      .catch((error) => {
        if (!closed) {
          setTimeline([]);
          setTimelineError(error instanceof Error ? error.message : "Failed to load timeline");
        }
      })
      .finally(() => {
        if (!closed) {
          setTimelineLoading(false);
        }
      });

      fetchJournalNotes({
        environment_id: environmentId,
        episode_id: params.episodeId,
        subject_type: "episode",
        subject_id: params.episodeId,
        limit: 10,
      })
        .then((items) => {
          if (!closed) {
            setActiveNote(items[0] ?? null);
            setNoteError(null);
          }
        })
        .catch((error) => {
          if (!closed) {
            setActiveNote(null);
            setNoteError(error instanceof Error ? error.message : "Failed to load note");
          }
        })
      .finally(() => {
        if (!closed) {
          setNoteLoading(false);
        }
      });

    return () => {
      closed = true;
    };
  }, [environmentId, params.episodeId]);

  useEffect(() => {
    let closed = false;
    if (!environmentId || !activeNote?.id) {
      setNoteRevisions([]);
      setRevisionsError(null);
      return () => {
        closed = true;
      };
    }
    setRevisionsLoading(true);
    fetchJournalNoteRevisions(activeNote.id, environmentId)
      .then((items) => {
        if (!closed) {
          setNoteRevisions(items);
          setRevisionsError(null);
        }
      })
      .catch((error) => {
        if (!closed) {
          setNoteRevisions([]);
          setRevisionsError(error instanceof Error ? error.message : "Failed to load note revisions");
        }
      })
      .finally(() => {
        if (!closed) {
          setRevisionsLoading(false);
        }
      });
    return () => {
      closed = true;
    };
  }, [activeNote?.id, environmentId]);

  async function handleSaveNote(markdown: string) {
    if (!environmentId) {
      throw new Error("Select an environment before saving notes.");
    }

    const saved = activeNote
      ? await updateJournalNote(activeNote.id, {
          environment_id: environmentId,
          subject_type: "episode",
          subject_id: params.episodeId,
          title: activeNote.title || `Episode ${episode?.episode_seq ?? params.episodeId} note`,
          body_markdown: markdown,
        })
      : await createJournalNote({
          environment_id: environmentId,
          subject_type: "episode",
          subject_id: params.episodeId,
          episode_id: params.episodeId,
          note_type: episode?.closed_at ? "post_exit_review" : "execution_rationale",
          title: `Episode ${episode?.episode_seq ?? params.episodeId} note`,
          body_markdown: markdown,
        });

    setActiveNote(saved);
    setNoteError(null);
  }

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />
      {!environmentId ? (
        <Panel className="p-4 md:p-5">
          <p className="text-sm text-amber-100">
            Add <span className="font-mono">environment_id</span> to the URL to load this episode without crossing Journal V2 environment boundaries.
          </p>
        </Panel>
      ) : null}

      <Panel
        eyebrow="Episode detail"
        title={episode ? `Episode #${episode.episode_seq}` : `Episode ${params.episodeId}`}
        action={
          environmentId ? (
            <Link
              href={`/journal/episodes?environment_id=${encodeURIComponent(environmentId)}`}
              className="rounded-full border border-border/70 bg-background/60 px-3 py-1.5 text-xs font-medium uppercase tracking-[0.2em] text-foreground/70"
            >
              Back to episodes
            </Link>
          ) : null
        }
        className="p-4 md:p-5"
      >
        {episodeLoading ? <p className="text-sm text-foreground/60">Loading episode metadata…</p> : null}
        {episodeError ? <p className="text-sm text-destructive">{episodeError}</p> : null}
        {!episodeLoading && !episodeError ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge tone={getEpisodeStatusTone(episode)}>{episode?.status ?? "pending"}</StatusBadge>
              <StatusBadge tone="neutral">Environment {environmentId}</StatusBadge>
            </div>

            <div className="grid gap-3 text-sm md:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-xl border border-border/70 bg-background/35 p-3">
                <p className="text-[11px] uppercase tracking-[0.2em] text-foreground/45">Episode ID</p>
                <p className="mt-2 break-all text-foreground/80">{episode?.id ?? params.episodeId}</p>
              </div>
              <div className="rounded-xl border border-border/70 bg-background/35 p-3">
                <p className="text-[11px] uppercase tracking-[0.2em] text-foreground/45">Execution context</p>
                <p className="mt-2 break-all text-foreground/80">{episode?.execution_context_id || "Not attached yet"}</p>
              </div>
              <div className="rounded-xl border border-border/70 bg-background/35 p-3">
                <p className="text-[11px] uppercase tracking-[0.2em] text-foreground/45">Opened</p>
                <p className="mt-2 text-foreground/80">{formatDateTime(episode?.opened_at ?? null)}</p>
              </div>
              <div className="rounded-xl border border-border/70 bg-background/35 p-3">
                <p className="text-[11px] uppercase tracking-[0.2em] text-foreground/45">Closed</p>
                <p className="mt-2 text-foreground/80">{formatDateTime(episode?.closed_at ?? null)}</p>
              </div>
            </div>
          </div>
        ) : null}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel eyebrow="Timeline" title="Episode activity" className="p-4 md:p-5">
          {timelineLoading ? <p className="text-sm text-foreground/60">Loading timeline…</p> : null}
          {timelineError ? <p className="text-sm text-destructive">{timelineError}</p> : null}
          {!timelineLoading && !timelineError ? <EpisodeTimeline events={timeline} /> : null}
        </Panel>
        <Panel eyebrow="Notes" title="Episode note" className="p-4 md:p-5">
          <div className="space-y-3">
            {noteLoading ? <p className="text-sm text-foreground/60">Loading note…</p> : null}
            {noteError ? <p className="text-sm text-destructive">{noteError}</p> : null}
            {!noteLoading ? (
              <div className="rounded-xl border border-dashed border-border/70 bg-background/30 px-4 py-3 text-sm text-foreground/65">
                {environmentId
                  ? activeNote
                    ? "Update the current episode note with observations, mistakes, or follow-up actions."
                    : "No note exists for this episode yet. Capture the trader context while it is still fresh."
                  : "Notes stay locked until an environment_id is present in the URL."}
              </div>
            ) : null}
            <MarkdownNoteEditor
              title={activeNote?.title ?? `Episode ${episode?.episode_seq ?? params.episodeId} note`}
              helperText={
                activeNote ? "Updates are revisioned on the backend." : "Create the first note for this episode."
              }
              initialMarkdown={activeNote?.body_markdown ?? ""}
              placeholder="Write a short markdown review for this episode…"
              disabled={!environmentId || noteLoading}
              onSave={handleSaveNote}
            />
          </div>
        </Panel>
      </div>

      <Panel eyebrow="History" title="Note revisions" className="p-4 md:p-5">
        {revisionsLoading ? <p className="text-sm text-foreground/60">Loading revisions…</p> : null}
        {revisionsError ? <p className="text-sm text-destructive">{revisionsError}</p> : null}
        {!revisionsLoading && !revisionsError ? (
          noteRevisions.length ? (
            <div className="space-y-2">
              {noteRevisions.slice(0, 5).map((revision) => (
                <div key={`${revision.note_id}-${revision.revision_no}`} className="rounded-xl border border-border/70 bg-background/35 p-3">
                  <p className="text-xs text-foreground/60">
                    Revision #{revision.revision_no}
                    {revision.edited_at ? ` · ${new Date(revision.edited_at).toLocaleString()}` : ""}
                  </p>
                  {revision.change_reason ? <p className="mt-1 text-xs text-foreground/70">{revision.change_reason}</p> : null}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-foreground/65">No revisions yet.</p>
          )
        ) : null}
      </Panel>
    </div>
  );
}
