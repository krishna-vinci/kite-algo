"use client";

import { useEffect, useState } from "react";

import { JournalKpiGrid } from "@/components/journal/journal-kpi-grid";
import { RecentEpisodesPanel } from "@/components/journal/recent-episodes-panel";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { UnresolvedQueuePanel } from "@/components/journal/unresolved-queue-panel";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";
import { fetchJournalEpisodes, fetchJournalV2AnalyticsSummary, fetchJournalV2Unresolved } from "@/lib/journal/api";
import type { AnalysisPeriod, JournalEpisode, JournalV2AnalyticsMetrics, JournalV2UnresolvedItem } from "@/lib/journal/types";

export default function JournalOverviewPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { environments, environmentsLoading, environmentsError, selectedEnvironmentId, selectedEnvironment } =
    useJournalWorkspace();
  const [v2MetricsState, setV2MetricsState] = useState<{
    environmentId: string;
    metrics: JournalV2AnalyticsMetrics | null;
    error: string | null;
  }>({ environmentId: "", metrics: null, error: null });
  const [episodesState, setEpisodesState] = useState<{
    environmentId: string;
    items: JournalEpisode[];
    error: string | null;
  }>({ environmentId: "", items: [], error: null });
  const [unresolvedState, setUnresolvedState] = useState<{
    environmentId: string;
    items: JournalV2UnresolvedItem[];
    error: string | null;
  }>({ environmentId: "", items: [], error: null });

  useEffect(() => {
    if (!selectedEnvironmentId) {
      return;
    }

    let closed = false;

    fetchJournalV2AnalyticsSummary(selectedEnvironmentId)
      .then((payload) => {
        if (closed) {
          return;
        }
        setV2MetricsState({ environmentId: selectedEnvironmentId, metrics: payload.metrics, error: null });
      })
      .catch((error) => {
        if (closed) {
          return;
        }
        setV2MetricsState({
          environmentId: selectedEnvironmentId,
          metrics: null,
          error: error instanceof Error ? error.message : "Failed to load Journal V2 analytics",
        });
      });

    fetchJournalEpisodes({ environment_id: selectedEnvironmentId, limit: 5 })
      .then((items) => {
        if (closed) {
          return;
        }
        setEpisodesState({ environmentId: selectedEnvironmentId, items, error: null });
      })
      .catch((error) => {
        if (closed) {
          return;
        }
        setEpisodesState({
          environmentId: selectedEnvironmentId,
          items: [],
          error: error instanceof Error ? error.message : "Failed to load recent episodes",
        });
      });

    fetchJournalV2Unresolved(selectedEnvironmentId)
      .then((payload) => {
        if (closed) {
          return;
        }
        setUnresolvedState({ environmentId: selectedEnvironmentId, items: payload.items ?? [], error: null });
      })
      .catch((error) => {
        if (closed) {
          return;
        }
        setUnresolvedState({
          environmentId: selectedEnvironmentId,
          items: [],
          error: error instanceof Error ? error.message : "Failed to load unresolved queue",
        });
      });

    return () => {
      closed = true;
    };
  }, [selectedEnvironmentId]);

  const displayedV2Metrics = selectedEnvironmentId && v2MetricsState.environmentId === selectedEnvironmentId ? v2MetricsState.metrics : null;
  const displayedV2MetricsLoading = Boolean(selectedEnvironmentId) && v2MetricsState.environmentId !== selectedEnvironmentId;
  const displayedV2MetricsError = selectedEnvironmentId && v2MetricsState.environmentId === selectedEnvironmentId ? v2MetricsState.error : null;
  const displayedEpisodes = selectedEnvironmentId && episodesState.environmentId === selectedEnvironmentId ? episodesState.items : [];
  const displayedEpisodesLoading = Boolean(selectedEnvironmentId) && episodesState.environmentId !== selectedEnvironmentId;
  const displayedEpisodesError = selectedEnvironmentId && episodesState.environmentId === selectedEnvironmentId ? episodesState.error : null;
  const displayedUnresolvedItems = selectedEnvironmentId && unresolvedState.environmentId === selectedEnvironmentId ? unresolvedState.items : [];
  const displayedUnresolvedLoading = Boolean(selectedEnvironmentId) && unresolvedState.environmentId !== selectedEnvironmentId;
  const displayedUnresolvedError = selectedEnvironmentId && unresolvedState.environmentId === selectedEnvironmentId ? unresolvedState.error : null;

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      <Panel eyebrow="Journal V2" title="Environment-scoped review" className="p-4 md:p-5">
        <p className="text-sm text-foreground/70">
          Review one execution environment at a time. Paper and live metrics stay separate unless you open an explicit comparison.
        </p>
        {environmentsLoading ? <p className="mt-3 text-sm text-foreground/60">Loading available environments…</p> : null}
        {environmentsError ? <p className="mt-3 text-sm text-destructive">{environmentsError}</p> : null}
        {!environmentsLoading && !environmentsError && environments.length === 0 ? (
          <div className="mt-3 rounded-xl border border-dashed border-border/70 bg-background/40 px-4 py-5 text-sm text-foreground/65">
            No Journal V2 environments are available yet. Create or sync one before using review analytics.
          </div>
        ) : null}
      </Panel>

      {selectedEnvironmentId ? (
        <>
          <JournalKpiGrid
            environment={selectedEnvironment}
            metrics={displayedV2Metrics}
            unresolvedCount={displayedUnresolvedItems.length}
          />
          {displayedV2MetricsLoading ? <p className="text-sm text-foreground/60">Loading Journal V2 overview metrics…</p> : null}
          {displayedV2MetricsError ? <p className="text-sm text-destructive">{displayedV2MetricsError}</p> : null}
        </>
      ) : (
        <Panel title="Waiting for environment" className="p-4 md:p-5">
          <p className="text-sm text-foreground/65">
            Select an environment to load review metrics, recent episodes, unresolved items, and quick notes.
          </p>
        </Panel>
      )}

      <div className="grid gap-4 xl:grid-cols-[1.45fr_1fr]">
        <RecentEpisodesPanel episodes={displayedEpisodes} loading={displayedEpisodesLoading} error={displayedEpisodesError} />
        <UnresolvedQueuePanel
          items={displayedUnresolvedItems}
          loading={displayedUnresolvedLoading}
          error={displayedUnresolvedError}
          compact
        />
      </div>
    </div>
  );
}
