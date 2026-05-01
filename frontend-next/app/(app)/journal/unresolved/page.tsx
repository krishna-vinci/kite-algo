"use client";

import { useEffect, useState } from "react";

import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";
import { fetchJournalV2Unresolved } from "@/lib/journal/api";
import type { AnalysisPeriod, JournalV2UnresolvedItem } from "@/lib/journal/types";

function queuePriority(status: string) {
  if (status === "pending" || status === "open") return "Needs action";
  if (status === "in_progress") return "Investigating";
  return "Queued";
}

export default function JournalUnresolvedPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();
  const [items, setItems] = useState<JournalV2UnresolvedItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedEnvironmentId) {
      setItems([]);
      setLoading(false);
      setError(null);
      return;
    }
    setLoading(true);
    fetchJournalV2Unresolved(selectedEnvironmentId)
      .then((payload) => {
        setItems(payload.items || []);
        setError(null);
      })
      .catch((loadError) => {
        setItems([]);
        setError(loadError instanceof Error ? loadError.message : "Failed to load unresolved queue");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [selectedEnvironmentId]);

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      <section className="rounded-xl border border-border/60 bg-background/60 p-4">
        <h3 className="text-sm font-semibold">Unresolved identity/activity queue</h3>
        {selectedEnvironment ? (
          <p className="mt-1 text-xs text-foreground/60">
            {selectedEnvironment.display_name || selectedEnvironment.account_scope} · {selectedEnvironment.mode}
          </p>
        ) : null}
        {!selectedEnvironmentId ? (
          <p className="mt-2 text-sm text-foreground/60">Select an environment to view unresolved items.</p>
        ) : loading ? (
          <p className="mt-2 text-sm text-foreground/60">Loading unresolved operator queue…</p>
        ) : error ? (
          <p className="mt-2 text-sm text-destructive">{error}</p>
        ) : items.length === 0 ? (
          <p className="mt-2 text-sm text-foreground/60">No unresolved items.</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {items.map((item) => (
              <li key={item.id} className="rounded-lg border border-border/60 p-3 text-sm">
                <div className="flex items-start justify-between gap-3">
                  <p className="font-medium">{item.reason}</p>
                  <span className="text-xs text-foreground/60">{queuePriority(item.status)}</span>
                </div>
                <p className="text-xs text-foreground/70">Source: {item.source_system}</p>
                <p className="text-xs text-foreground/70">Status: {item.status}</p>
                <p className="text-xs text-foreground/70">Raw strategy label: {String(item.raw_identity?.strategy_name ?? "-")}</p>
                <p className="text-xs text-foreground/70">Candidate mappings: {item.candidate_mappings.length}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      {selectedEnvironmentId ? (
        <Panel title="Queue operator guidance" className="p-4 md:p-5">
          <ul className="list-disc space-y-1 pl-5 text-sm text-foreground/65">
            <li>Start with items marked “Needs action” and missing strategy identity.</li>
            <li>Validate raw identity fields against recent episode context before mapping.</li>
            <li>Track recurring reason patterns to improve upstream tagging quality.</li>
          </ul>
        </Panel>
      ) : null}
    </div>
  );
}
