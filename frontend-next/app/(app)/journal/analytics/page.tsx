"use client";

import { useEffect, useMemo, useState } from "react";

import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";
import {
  fetchJournalV2AnalyticsStrategies,
  fetchJournalV2AnalyticsSummary,
  fetchJournalV2PaperLiveComparison,
} from "@/lib/journal/api";
import type {
  AnalysisPeriod,
  JournalV2PaperLiveComparison,
  JournalV2StrategyScorecard,
} from "@/lib/journal/types";

function MetricsColumn({ title, metrics }: { title: string; metrics: Record<string, unknown> }) {
  return (
    <div className="rounded-lg border border-border/60 p-3">
      <h4 className="text-sm font-semibold">{title}</h4>
      <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">
        <dt>Closed Episodes</dt>
        <dd>{String(metrics.closed_episode_count ?? 0)}</dd>
        <dt>Net P&L</dt>
        <dd>{String(metrics.net_pnl ?? 0)}</dd>
        <dt>Total Charges</dt>
        <dd>{String(metrics.total_charges ?? 0)}</dd>
      </dl>
    </div>
  );
}

export default function JournalAnalyticsPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { environments, selectedEnvironmentId } = useJournalWorkspace();
  const [summaryMetrics, setSummaryMetrics] = useState<Record<string, unknown> | null>(null);
  const [strategyItems, setStrategyItems] = useState<JournalV2StrategyScorecard[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [paperEnvironmentId, setPaperEnvironmentId] = useState("");
  const [liveEnvironmentId, setLiveEnvironmentId] = useState("");
  const [comparison, setComparison] = useState<JournalV2PaperLiveComparison | null>(null);

  useEffect(() => {
    if (!selectedEnvironmentId) {
      setSummaryMetrics(null);
      setStrategyItems([]);
      return;
    }
    fetchJournalV2AnalyticsSummary(selectedEnvironmentId).then((payload) =>
      setSummaryMetrics(payload.metrics as Record<string, unknown>),
    );
    fetchJournalV2AnalyticsStrategies(selectedEnvironmentId).then((payload) => setStrategyItems(payload.items || []));
  }, [selectedEnvironmentId]);

  useEffect(() => {
    if (!selectedTemplateId || !paperEnvironmentId || !liveEnvironmentId) {
      setComparison(null);
      return;
    }
    fetchJournalV2PaperLiveComparison({
      template_id: selectedTemplateId,
      paper_environment_id: paperEnvironmentId,
      live_environment_id: liveEnvironmentId,
    }).then(setComparison);
  }, [selectedTemplateId, paperEnvironmentId, liveEnvironmentId]);

  const templateOptions = useMemo(
    () => strategyItems.map((item) => ({ id: item.template_id, label: item.display_name || item.template_id })),
    [strategyItems],
  );
  const paperEnvironments = useMemo(() => environments.filter((item) => item.mode === "paper"), [environments]);
  const liveEnvironments = useMemo(() => environments.filter((item) => item.mode === "live"), [environments]);

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      {!selectedEnvironmentId ? (
        <Panel className="p-4 md:p-5">
          Select an environment to view analytics.
        </Panel>
      ) : null}

      <section className="rounded-xl border border-border/60 bg-background/60 p-4">
        <h3 className="text-sm font-semibold">Environment Summary</h3>
        {summaryMetrics ? <MetricsColumn title="Selected Environment" metrics={summaryMetrics} /> : <p className="text-sm text-foreground/60">No summary yet.</p>}
      </section>

      <section className="rounded-xl border border-border/60 bg-background/60 p-4">
        <h3 className="text-sm font-semibold">Strategy Template Scorecards</h3>
        <div className="mt-2 space-y-2">
          {strategyItems.map((item) => (
            <MetricsColumn key={item.template_id} title={item.display_name} metrics={item.metrics as Record<string, unknown>} />
          ))}
          {strategyItems.length === 0 ? <p className="text-sm text-foreground/60">No strategy scorecards found.</p> : null}
        </div>
      </section>

      <section className="rounded-xl border border-border/60 bg-background/60 p-4">
        <h3 className="text-sm font-semibold">Paper vs Live Comparison</h3>
        <div className="mt-2 grid gap-3 md:grid-cols-3">
          <label className="block text-xs text-foreground/70">
            Template
            <select
              aria-label="Paper live template selector"
              className="mt-1 block rounded-md border border-border/70 bg-background px-2 py-1 text-sm"
              value={selectedTemplateId}
              onChange={(event) => setSelectedTemplateId(event.target.value)}
            >
              <option value="">Select template</option>
              {templateOptions.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-xs text-foreground/70">
            Paper environment
            <select
              aria-label="Paper comparison environment"
              className="mt-1 block rounded-md border border-border/70 bg-background px-2 py-1 text-sm"
              value={paperEnvironmentId}
              onChange={(event) => setPaperEnvironmentId(event.target.value)}
            >
              <option value="">Select paper</option>
              {paperEnvironments.map((env) => (
                <option key={env.id} value={env.id}>{env.display_name || env.account_scope}</option>
              ))}
            </select>
          </label>
          <label className="block text-xs text-foreground/70">
            Live environment
            <select
              aria-label="Live comparison environment"
              className="mt-1 block rounded-md border border-border/70 bg-background px-2 py-1 text-sm"
              value={liveEnvironmentId}
              onChange={(event) => setLiveEnvironmentId(event.target.value)}
            >
              <option value="">Select live</option>
              {liveEnvironments.map((env) => (
                <option key={env.id} value={env.id}>{env.display_name || env.account_scope}</option>
              ))}
            </select>
          </label>
        </div>

        {comparison ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2" data-testid="paper-live-comparison-columns">
            <MetricsColumn title="Paper" metrics={comparison.paper as Record<string, unknown>} />
            <MetricsColumn title="Live" metrics={comparison.live as Record<string, unknown>} />
          </div>
        ) : (
          <p className="mt-2 text-sm text-foreground/60">Select a template plus explicit paper and live environments to compare them separately.</p>
        )}
      </section>
    </div>
  );
}
