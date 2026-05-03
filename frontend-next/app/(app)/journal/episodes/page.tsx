"use client";

import { useEffect, useState } from "react";

import { JournalPageLink } from "@/components/journal/journal-page-link";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { fetchJournalEpisodes } from "@/lib/journal/api";
import type { AnalysisPeriod, JournalEpisode } from "@/lib/journal/types";

function formatDateTime(value: string | null) {
  if (!value) {
    return "Open";
  }
  return new Date(value).toLocaleString();
}

export default function JournalEpisodesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();
  const [episodesState, setEpisodesState] = useState<{
    environmentId: string;
    items: JournalEpisode[];
    error: string | null;
  }>({ environmentId: "", items: [], error: null });

  useEffect(() => {
    let closed = false;
    if (!selectedEnvironmentId) {
      return () => {
        closed = true;
      };
    }
    fetchJournalEpisodes({ environment_id: selectedEnvironmentId })
      .then((items) => {
        if (!closed) {
          setEpisodesState({ environmentId: selectedEnvironmentId, items, error: null });
        }
      })
      .catch((error) => {
        if (!closed) {
          setEpisodesState({
            environmentId: selectedEnvironmentId,
            items: [],
            error: error instanceof Error ? error.message : "Failed to load episodes",
          });
        }
      });
    return () => {
      closed = true;
    };
  }, [selectedEnvironmentId]);

  const displayedEpisodes = selectedEnvironmentId && episodesState.environmentId === selectedEnvironmentId ? episodesState.items : [];
  const displayedEpisodesLoading = Boolean(selectedEnvironmentId) && episodesState.environmentId !== selectedEnvironmentId;
  const displayedEpisodesError = selectedEnvironmentId && episodesState.environmentId === selectedEnvironmentId ? episodesState.error : null;

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      {!selectedEnvironment ? (
        <Panel className="p-4 md:p-5">
          <p className="text-sm text-foreground/70">Select an environment to view the Journal V2 episode feed safely.</p>
        </Panel>
      ) : null}

      <Panel
        eyebrow={selectedEnvironment ? `${selectedEnvironment.display_name || selectedEnvironment.account_scope} · ${selectedEnvironment.mode}` : "Episodes"}
        title="Episode ledger"
        className="p-4 md:p-5"
      >
        {displayedEpisodesLoading ? <p className="text-sm text-foreground/60">Loading episodes…</p> : null}
        {displayedEpisodesError ? <p className="text-sm text-destructive">{displayedEpisodesError}</p> : null}
        {!displayedEpisodesLoading && !displayedEpisodesError && selectedEnvironment && displayedEpisodes.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border/70 bg-background/35 px-4 py-6 text-sm text-foreground/65">
            No episodes found for this environment yet.
          </div>
        ) : null}

        {!displayedEpisodesLoading && !displayedEpisodesError && displayedEpisodes.length > 0 ? (
          <div className="space-y-3">
            {displayedEpisodes.map((episode) => (
              <JournalPageLink
                key={episode.id}
                href={`/journal/episodes/${episode.id}`}
                className="block rounded-2xl border border-border/70 bg-background/35 p-4 transition-colors hover:border-primary/30 hover:bg-background/55"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold text-foreground">Episode #{episode.episode_seq}</p>
                    <p className="mt-1 text-xs text-foreground/60">{episode.execution_context_id || "No execution context"}</p>
                  </div>
                  <StatusBadge tone={episode.closed_at ? "positive" : "warning"}>{episode.status}</StatusBadge>
                </div>
                <div className="mt-4 grid gap-3 text-xs text-foreground/65 md:grid-cols-4">
                  <div>
                    <p className="uppercase tracking-[0.18em] text-foreground/45">Episode ID</p>
                    <p className="mt-1 truncate text-foreground/75">{episode.id}</p>
                  </div>
                  <div>
                    <p className="uppercase tracking-[0.18em] text-foreground/45">Opened</p>
                    <p className="mt-1 text-foreground/75">{formatDateTime(episode.opened_at)}</p>
                  </div>
                  <div>
                    <p className="uppercase tracking-[0.18em] text-foreground/45">Closed</p>
                    <p className="mt-1 text-foreground/75">{formatDateTime(episode.closed_at)}</p>
                  </div>
                  <div>
                    <p className="uppercase tracking-[0.18em] text-foreground/45">Environment</p>
                    <p className="mt-1 truncate text-foreground/75">{episode.environment_id}</p>
                  </div>
                </div>
              </JournalPageLink>
            ))}
          </div>
        ) : null}
      </Panel>
    </div>
  );
}
