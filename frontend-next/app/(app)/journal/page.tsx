"use client";

import { useEffect, useState } from "react";

import { JournalPageLink } from "@/components/journal/journal-page-link";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { KpiCard } from "@/components/operator/kpi-card";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { fetchJournalEpisodes, fetchJournalV2AnalyticsSummary, fetchJournalV2Unresolved } from "@/lib/journal/api";
import type { AnalysisPeriod, JournalEpisode, JournalV2AnalyticsMetrics, JournalV2UnresolvedItem } from "@/lib/journal/types";

function formatAmount(value: number | string | null | undefined) {
  const amount = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(amount)) {
    return "—";
  }
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(amount);
}

function formatPercent(value: number | string | null | undefined) {
  const amount = typeof value === "number" ? value : Number(value ?? null);
  if (!Number.isFinite(amount)) {
    return "—";
  }
  return `${amount.toFixed(1)}%`;
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "Open";
  }
  return new Date(value).toLocaleString();
}

export default function JournalOverviewPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { environments, environmentsLoading, environmentsError, selectedEnvironmentId, selectedEnvironment } =
    useJournalWorkspace();
  const [v2Metrics, setV2Metrics] = useState<JournalV2AnalyticsMetrics | null>(null);
  const [v2MetricsLoading, setV2MetricsLoading] = useState(false);
  const [v2MetricsError, setV2MetricsError] = useState<string | null>(null);
  const [episodes, setEpisodes] = useState<JournalEpisode[]>([]);
  const [episodesLoading, setEpisodesLoading] = useState(false);
  const [episodesError, setEpisodesError] = useState<string | null>(null);
  const [unresolvedItems, setUnresolvedItems] = useState<JournalV2UnresolvedItem[]>([]);
  const [unresolvedLoading, setUnresolvedLoading] = useState(false);
  const [unresolvedError, setUnresolvedError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedEnvironmentId) {
      setV2Metrics(null);
      setV2MetricsError(null);
      setEpisodes([]);
      setEpisodesError(null);
      setUnresolvedItems([]);
      setUnresolvedError(null);
      return;
    }

    let closed = false;
    setV2MetricsLoading(true);
    setEpisodesLoading(true);
    setUnresolvedLoading(true);

    fetchJournalV2AnalyticsSummary(selectedEnvironmentId)
      .then((payload) => {
        if (closed) {
          return;
        }
        setV2Metrics(payload.metrics);
        setV2MetricsError(null);
      })
      .catch((error) => {
        if (closed) {
          return;
        }
        setV2Metrics(null);
        setV2MetricsError(error instanceof Error ? error.message : "Failed to load Journal V2 analytics");
      })
      .finally(() => {
        if (!closed) {
          setV2MetricsLoading(false);
        }
      });

    fetchJournalEpisodes({ environment_id: selectedEnvironmentId, limit: 5 })
      .then((items) => {
        if (closed) {
          return;
        }
        setEpisodes(items);
        setEpisodesError(null);
      })
      .catch((error) => {
        if (closed) {
          return;
        }
        setEpisodes([]);
        setEpisodesError(error instanceof Error ? error.message : "Failed to load recent episodes");
      })
      .finally(() => {
        if (!closed) {
          setEpisodesLoading(false);
        }
      });

    fetchJournalV2Unresolved(selectedEnvironmentId)
      .then((payload) => {
        if (closed) {
          return;
        }
        setUnresolvedItems(payload.items ?? []);
        setUnresolvedError(null);
      })
      .catch((error) => {
        if (closed) {
          return;
        }
        setUnresolvedItems([]);
        setUnresolvedError(error instanceof Error ? error.message : "Failed to load unresolved queue");
      })
      .finally(() => {
        if (!closed) {
          setUnresolvedLoading(false);
        }
      });

    return () => {
      closed = true;
    };
  }, [selectedEnvironmentId]);

  const liveEnvironmentExists = environments.some((item) => item.mode === "live");
  const quickLinks = [
    { label: "Episodes", href: "/journal/episodes", description: "Inspect episode-level flow and notes." },
    { label: "Analytics", href: "/journal/analytics", description: "Review environment and template scorecards." },
    { label: "Notes", href: "/journal/notes", description: "Browse and capture Journal V2 notes." },
    { label: "Strategies", href: "/journal/strategies", description: "Compare template performance in one environment." },
  ] as const;

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      <Panel
        eyebrow="Journal V2"
        title="Environment-scoped overview"
        className="p-4 md:p-5"
      >
        <div className="space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1">
              <p className="text-sm text-foreground/70">
                Start from a single environment so episode, analytics, and note data stay safely scoped.
              </p>
              {selectedEnvironment ? (
                <StatusBadge tone={selectedEnvironment.mode === "live" ? "warning" : "neutral"}>
                  {selectedEnvironment.display_name || selectedEnvironment.account_scope} · {selectedEnvironment.mode}
                </StatusBadge>
              ) : null}
            </div>
            {!liveEnvironmentExists && environments.length > 0 ? (
              <div className="max-w-md rounded-xl border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
                Live validation environment does not exist yet, so live-side Journal V2 validation is still pending.
              </div>
            ) : null}
          </div>
          {environmentsLoading ? <p className="text-sm text-foreground/60">Loading available environments…</p> : null}
          {environmentsError ? <p className="text-sm text-destructive">{environmentsError}</p> : null}
          {!environmentsLoading && !environmentsError && environments.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border/70 bg-background/40 px-4 py-5 text-sm text-foreground/65">
              No Journal V2 environments are available yet. Create or sync one before using overview analytics.
            </div>
          ) : null}
        </div>
      </Panel>

      {!selectedEnvironmentId ? (
        <Panel title="Waiting for environment" className="p-4 md:p-5">
          <p className="text-sm text-foreground/65">
            Select an environment above to load Journal V2 summary cards, recent episodes, unresolved queue signals, and scoped navigation links.
          </p>
        </Panel>
      ) : null}

      {selectedEnvironmentId ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          <KpiCard
            label="Environment / Mode"
            value={selectedEnvironment ? `${selectedEnvironment.display_name || selectedEnvironment.account_scope}` : "—"}
            note={selectedEnvironment ? `${selectedEnvironment.mode} · epoch ${selectedEnvironment.environment_epoch}` : "Not selected"}
            className="xl:col-span-1 [&_p:last-child]:text-xs [&_p:nth-child(2)]:text-lg [&_p:nth-child(2)]:font-semibold [&_p:nth-child(2)]:text-foreground"
          />
          <KpiCard label="Closed episodes" value={String(v2Metrics?.closed_episode_count ?? 0)} note="Scoped to selected Journal V2 environment" />
          <KpiCard label="Net P&L" value={formatAmount(v2Metrics?.net_pnl)} note="Realized net performance only" />
          <KpiCard label="Win rate" value={formatPercent(v2Metrics?.win_rate)} note="Closed episodes with positive outcome" />
          <KpiCard label="Charges" value={formatAmount(v2Metrics?.total_charges)} note="Brokerage and related costs" />
        </div>
      ) : null}

      {selectedEnvironmentId && v2MetricsLoading ? <p className="text-sm text-foreground/60">Loading Journal V2 overview metrics…</p> : null}
      {selectedEnvironmentId && v2MetricsError ? <p className="text-sm text-destructive">{v2MetricsError}</p> : null}

      <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
        <Panel eyebrow="Recent activity" title="Recent V2 episodes" className="p-4 md:p-5">
          {!selectedEnvironmentId ? <p className="text-sm text-foreground/65">Choose an environment to inspect recent Journal V2 episodes.</p> : null}
          {episodesLoading ? <p className="text-sm text-foreground/60">Loading recent episodes…</p> : null}
          {episodesError ? <p className="text-sm text-destructive">{episodesError}</p> : null}
          {selectedEnvironmentId && !episodesLoading && !episodesError && episodes.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border/70 bg-background/30 px-4 py-5 text-sm text-foreground/65">
              No V2 episodes were found for this environment yet.
            </div>
          ) : null}
          <div className="space-y-3">
            {episodes.map((episode) => (
              <JournalPageLink
                key={episode.id}
                href={`/journal/episodes/${episode.id}`}
                className="block rounded-2xl border border-border/70 bg-background/35 p-4 transition-colors hover:border-primary/30 hover:bg-background/55"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold text-foreground">Episode #{episode.episode_seq}</p>
                    <p className="mt-1 text-xs text-foreground/60">{episode.execution_context_id || "No execution context recorded"}</p>
                  </div>
                  <StatusBadge tone={episode.closed_at ? "positive" : "warning"}>{episode.status}</StatusBadge>
                </div>
                <div className="mt-3 grid gap-2 text-xs text-foreground/65 md:grid-cols-2">
                  <p>Opened: {formatDateTime(episode.opened_at)}</p>
                  <p>Closed: {formatDateTime(episode.closed_at)}</p>
                </div>
              </JournalPageLink>
            ))}
          </div>
        </Panel>

        <div className="space-y-4">
          <Panel eyebrow="Queue health" title="Unresolved queue summary" className="p-4 md:p-5">
            {!selectedEnvironmentId ? <p className="text-sm text-foreground/65">Choose an environment to check unresolved identity or mapping issues.</p> : null}
            {unresolvedLoading ? <p className="text-sm text-foreground/60">Loading unresolved queue…</p> : null}
            {unresolvedError ? <p className="text-sm text-destructive">{unresolvedError}</p> : null}
            {selectedEnvironmentId && !unresolvedLoading && !unresolvedError ? (
              <>
                <div className="rounded-2xl border border-border/70 bg-background/35 p-4">
                  <p className="text-[11px] uppercase tracking-[0.24em] text-foreground/45">Open items</p>
                  <p className="mt-2 text-3xl font-semibold tracking-tight text-foreground">{unresolvedItems.length}</p>
                  <p className="mt-1 text-sm text-foreground/60">Identity or mapping issues still needing action.</p>
                </div>
                {unresolvedItems.length === 0 ? (
                  <p className="text-sm text-foreground/65">No unresolved items for this environment.</p>
                ) : (
                  <ul className="space-y-2">
                    {unresolvedItems.slice(0, 3).map((item) => (
                      <li key={item.id} className="rounded-xl border border-border/70 bg-background/30 p-3 text-sm">
                        <p className="font-medium text-foreground">{item.reason}</p>
                        <p className="mt-1 text-xs text-foreground/60">{item.source_system}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : null}
          </Panel>

          <Panel eyebrow="Navigate" title="Quick links" className="p-4 md:p-5">
            <div className="grid gap-3 sm:grid-cols-2">
              {quickLinks.map((link) => (
                <JournalPageLink
                  key={link.href}
                  href={link.href}
                  className="rounded-2xl border border-border/70 bg-background/35 p-4 transition-colors hover:border-primary/30 hover:bg-background/55"
                >
                  <p className="text-sm font-semibold text-foreground">{link.label}</p>
                  <p className="mt-1 text-xs leading-5 text-foreground/60">{link.description}</p>
                </JournalPageLink>
              ))}
            </div>
          </Panel>
        </div>
      </div>

    </div>
  );
}
