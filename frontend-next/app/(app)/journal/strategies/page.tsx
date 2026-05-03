"use client";

import { useEffect, useState } from "react";
import type { AnalysisPeriod, JournalV2StrategyScorecard } from "@/lib/journal/types";
import { fetchJournalV2AnalyticsStrategies } from "@/lib/journal/api";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";

function formatMetricValue(key: string, value: unknown) {
  const numberValue = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(numberValue)) return "—";
  if (key.includes("pnl") || key.includes("charges")) {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(numberValue);
  }
  if (key.includes("rate")) return `${numberValue.toFixed(1)}%`;
  return numberValue.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export default function JournalStrategiesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();

  const [strategiesState, setStrategiesState] = useState<{
    environmentId: string;
    items: JournalV2StrategyScorecard[];
    error: string | null;
  }>({ environmentId: "", items: [], error: null });

  useEffect(() => {
    if (!selectedEnvironmentId) {
      return;
    }
    fetchJournalV2AnalyticsStrategies(selectedEnvironmentId)
      .then((payload) => {
        setStrategiesState({ environmentId: selectedEnvironmentId, items: payload.items || [], error: null });
      })
      .catch((err) => {
        setStrategiesState({
          environmentId: selectedEnvironmentId,
          items: [],
          error: err instanceof Error ? err.message : "Failed to load Journal V2 strategies",
        });
      });
  }, [selectedEnvironmentId]);

  const displayedStrategies = selectedEnvironmentId && strategiesState.environmentId === selectedEnvironmentId ? strategiesState.items : [];
  const displayedLoading = Boolean(selectedEnvironmentId) && strategiesState.environmentId !== selectedEnvironmentId;
  const displayedError = selectedEnvironmentId && strategiesState.environmentId === selectedEnvironmentId ? strategiesState.error : null;

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      {!selectedEnvironmentId ? (
        <Panel className="p-4 md:p-5">
          Select an environment to load Journal V2 strategy analytics safely.
        </Panel>
      ) : null}

        <section className="rounded-xl border border-border/60 bg-background/60 p-4">
          {selectedEnvironment ? (
            <p className="mb-2 text-xs text-foreground/60">
              {selectedEnvironment.display_name || selectedEnvironment.account_scope} · {selectedEnvironment.mode}
            </p>
          ) : null}
        <h3 className="text-sm font-semibold">Strategy Template Scorecards</h3>
        {displayedLoading ? <p className="mt-2 text-sm text-foreground/60">Loading environment-scoped strategies…</p> : null}
        {displayedError ? <p className="mt-2 text-sm text-destructive">{displayedError}</p> : null}
        {!displayedLoading && !displayedError && displayedStrategies.length === 0 ? <p className="mt-2 text-sm text-foreground/60">No strategy scorecards found for this environment.</p> : null}
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {displayedStrategies.map((item) => (
            <div key={item.template_id} className="rounded-lg border border-border/60 p-3 text-sm">
              <div className="font-medium">{item.display_name || item.template_id}</div>
              <div className="mt-3 grid gap-2 text-xs text-foreground/70 sm:grid-cols-2 xl:grid-cols-4">
                <span>Episodes: {String(item.metrics.closed_episode_count ?? 0)}</span>
                <span>Net P&L: {formatMetricValue("net_pnl", item.metrics.net_pnl)}</span>
                <span>Win rate: {formatMetricValue("win_rate", item.metrics.win_rate)}</span>
                <span>Charges: {formatMetricValue("total_charges", item.metrics.total_charges)}</span>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
