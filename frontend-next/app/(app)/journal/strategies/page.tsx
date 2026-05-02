"use client";

import { useEffect, useState } from "react";
import type { AnalysisPeriod, JournalV2StrategyScorecard } from "@/lib/journal/types";
import { fetchJournalV2AnalyticsStrategies } from "@/lib/journal/api";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";

export default function JournalStrategiesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();

  const [strategies, setStrategies] = useState<JournalV2StrategyScorecard[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedEnvironmentId) {
      setStrategies([]);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    fetchJournalV2AnalyticsStrategies(selectedEnvironmentId)
      .then((payload) => {
        setStrategies(payload.items || []);
        setError(null);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load Journal V2 strategies");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [selectedEnvironmentId]);

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
        <h3 className="text-sm font-semibold">Journal V2 strategy scorecards</h3>
        {loading ? <p className="mt-2 text-sm text-foreground/60">Loading environment-scoped strategies…</p> : null}
        {error ? <p className="mt-2 text-sm text-destructive">{error}</p> : null}
        {!loading && !error && strategies.length === 0 ? <p className="mt-2 text-sm text-foreground/60">No strategy scorecards found for this environment.</p> : null}
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {strategies.map((item) => (
            <div key={item.template_id} className="rounded-lg border border-border/60 p-3 text-sm">
              <div className="font-medium">{item.display_name || item.template_id}</div>
              <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-foreground/70">
                <span>Episodes: {String(item.metrics.closed_episode_count ?? 0)}</span>
                <span>Net: {String(item.metrics.net_pnl ?? 0)}</span>
                <span>Charges: {String(item.metrics.total_charges ?? 0)}</span>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
