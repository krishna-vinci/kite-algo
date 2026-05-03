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
  JournalV2AnalyticsMetrics,
  JournalV2PaperLiveComparison,
  JournalV2StrategyScorecard,
} from "@/lib/journal/types";

function formatMetricValue(key: string, value: unknown) {
  const numberValue = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(numberValue)) return "—";
  if (key.includes("pnl") || key.includes("charges")) {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(numberValue);
  }
  if (key.includes("rate")) return `${numberValue.toFixed(1)}%`;
  return numberValue.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function MetricsColumn({ title, metrics }: { title: string; metrics: Record<string, unknown> }) {
  const rows = [
    ["closed_episode_count", "Closed episodes"],
    ["net_pnl", "Net P&L"],
    ["win_rate", "Win rate"],
    ["total_charges", "Charges"],
  ] as const;

  return (
    <div className="rounded-xl border border-border/60 bg-background/40 p-4">
      <h4 className="text-sm font-semibold text-foreground">{title}</h4>
      <dl className="mt-3 grid gap-2 text-xs">
        {rows.map(([key, label]) => (
          <div key={key} className="flex items-center justify-between gap-3">
            <dt className="text-foreground/60">{label}</dt>
            <dd className="font-mono text-foreground">{formatMetricValue(key, metrics[key])}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export default function JournalAnalyticsPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { environments, selectedEnvironmentId } = useJournalWorkspace();
  const [summaryState, setSummaryState] = useState<{
    environmentId: string;
    metrics: JournalV2AnalyticsMetrics | null;
    error: string | null;
  }>({ environmentId: "", metrics: null, error: null });
  const [strategiesState, setStrategiesState] = useState<{
    environmentId: string;
    items: JournalV2StrategyScorecard[];
    error: string | null;
  }>({ environmentId: "", items: [], error: null });
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [paperEnvironmentId, setPaperEnvironmentId] = useState("");
  const [liveEnvironmentId, setLiveEnvironmentId] = useState("");
  const [comparisonState, setComparisonState] = useState<{
    requestKey: string;
    comparison: JournalV2PaperLiveComparison | null;
    error: string | null;
  }>({ requestKey: "", comparison: null, error: null });

  useEffect(() => {
    if (!selectedEnvironmentId) {
      return;
    }

    fetchJournalV2AnalyticsSummary(selectedEnvironmentId)
      .then((payload) => {
        setSummaryState({ environmentId: selectedEnvironmentId, metrics: payload.metrics, error: null });
      })
      .catch((error) => {
        setSummaryState({
          environmentId: selectedEnvironmentId,
          metrics: null,
          error: error instanceof Error ? error.message : "Failed to load analytics summary",
        });
      });

    fetchJournalV2AnalyticsStrategies(selectedEnvironmentId)
      .then((payload) => {
        setStrategiesState({ environmentId: selectedEnvironmentId, items: payload.items || [], error: null });
      })
      .catch((error) => {
        setStrategiesState({
          environmentId: selectedEnvironmentId,
          items: [],
          error: error instanceof Error ? error.message : "Failed to load strategy scorecards",
        });
      });
  }, [selectedEnvironmentId]);

  const canCompare = Boolean(selectedTemplateId && paperEnvironmentId && liveEnvironmentId);
  const comparisonRequestKey = canCompare ? `${selectedTemplateId}:${paperEnvironmentId}:${liveEnvironmentId}` : "";

  useEffect(() => {
    if (!canCompare) {
      return;
    }

    fetchJournalV2PaperLiveComparison({
      template_id: selectedTemplateId,
      paper_environment_id: paperEnvironmentId,
      live_environment_id: liveEnvironmentId,
    })
      .then((payload) => {
        setComparisonState({ requestKey: comparisonRequestKey, comparison: payload, error: null });
      })
      .catch((error) => {
        setComparisonState({
          requestKey: comparisonRequestKey,
          comparison: null,
          error: error instanceof Error ? error.message : "Failed to load paper/live comparison",
        });
      });
  }, [canCompare, comparisonRequestKey, liveEnvironmentId, paperEnvironmentId, selectedTemplateId]);

  const displayedSummaryMetrics = selectedEnvironmentId && summaryState.environmentId === selectedEnvironmentId ? summaryState.metrics : null;
  const displayedSummaryError = selectedEnvironmentId && summaryState.environmentId === selectedEnvironmentId ? summaryState.error : null;
  const displayedSummaryLoading = Boolean(selectedEnvironmentId) && summaryState.environmentId !== selectedEnvironmentId;
  const displayedStrategyItems = selectedEnvironmentId && strategiesState.environmentId === selectedEnvironmentId ? strategiesState.items : [];
  const displayedStrategiesError = selectedEnvironmentId && strategiesState.environmentId === selectedEnvironmentId ? strategiesState.error : null;
  const displayedStrategiesLoading = Boolean(selectedEnvironmentId) && strategiesState.environmentId !== selectedEnvironmentId;
  const displayedComparison = comparisonState.requestKey === comparisonRequestKey ? comparisonState.comparison : null;
  const displayedComparisonError = comparisonState.requestKey === comparisonRequestKey ? comparisonState.error : null;
  const displayedComparisonLoading = Boolean(comparisonRequestKey) && comparisonState.requestKey !== comparisonRequestKey;

  const templateOptions = useMemo(
    () =>
      (selectedEnvironmentId && strategiesState.environmentId === selectedEnvironmentId ? strategiesState.items : []).map((item) => ({
        id: item.template_id,
        label: item.display_name || item.template_id,
      })),
    [selectedEnvironmentId, strategiesState.environmentId, strategiesState.items],
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
        {displayedSummaryLoading ? <p className="mt-2 text-sm text-foreground/60">Loading analytics summary…</p> : null}
        {displayedSummaryError ? <p className="mt-2 text-sm text-destructive">{displayedSummaryError}</p> : null}
        {displayedSummaryMetrics ? <div className="mt-2"><MetricsColumn title="Selected Environment" metrics={displayedSummaryMetrics} /></div> : null}
        {!displayedSummaryLoading && !displayedSummaryError && !displayedSummaryMetrics ? <p className="mt-2 text-sm text-foreground/60">No summary yet.</p> : null}
      </section>

      <section className="rounded-xl border border-border/60 bg-background/60 p-4">
        <h3 className="text-sm font-semibold">Strategy Template Scorecards</h3>
        <div className="mt-2 space-y-2">
          {displayedStrategiesLoading ? <p className="text-sm text-foreground/60">Loading strategy scorecards…</p> : null}
          {displayedStrategiesError ? <p className="text-sm text-destructive">{displayedStrategiesError}</p> : null}
          {displayedStrategyItems.map((item) => (
            <MetricsColumn key={item.template_id} title={item.display_name} metrics={item.metrics as Record<string, unknown>} />
          ))}
          {!displayedStrategiesLoading && !displayedStrategiesError && displayedStrategyItems.length === 0 ? <p className="text-sm text-foreground/60">No strategy scorecards found.</p> : null}
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

        {displayedComparisonLoading ? <p className="mt-2 text-sm text-foreground/60">Loading comparison…</p> : null}
        {displayedComparisonError ? <p className="mt-2 text-sm text-destructive">{displayedComparisonError}</p> : null}
        {displayedComparison ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2" data-testid="paper-live-comparison-columns">
            <MetricsColumn title="Paper" metrics={displayedComparison.paper as Record<string, unknown>} />
            <MetricsColumn title="Live" metrics={displayedComparison.live as Record<string, unknown>} />
          </div>
        ) : (
          <p className="mt-2 text-sm text-foreground/60">Select a template plus explicit paper and live environments to compare them separately.</p>
        )}
      </section>
    </div>
  );
}
