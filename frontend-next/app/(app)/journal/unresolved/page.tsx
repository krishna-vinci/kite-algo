"use client";

import { useEffect, useState } from "react";

import { UnresolvedQueuePanel } from "@/components/journal/unresolved-queue-panel";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";
import { fetchJournalV2Unresolved } from "@/lib/journal/api";
import type { AnalysisPeriod, JournalV2UnresolvedItem } from "@/lib/journal/types";

export default function JournalUnresolvedPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();
  const [unresolvedState, setUnresolvedState] = useState<{
    environmentId: string;
    items: JournalV2UnresolvedItem[];
    error: string | null;
  }>({ environmentId: "", items: [], error: null });

  useEffect(() => {
    if (!selectedEnvironmentId) {
      return;
    }
    fetchJournalV2Unresolved(selectedEnvironmentId)
      .then((payload) => {
        setUnresolvedState({ environmentId: selectedEnvironmentId, items: payload.items || [], error: null });
      })
      .catch((loadError) => {
        setUnresolvedState({
          environmentId: selectedEnvironmentId,
          items: [],
          error: loadError instanceof Error ? loadError.message : "Failed to load unresolved queue",
        });
      });
  }, [selectedEnvironmentId]);

  const displayedItems = selectedEnvironmentId && unresolvedState.environmentId === selectedEnvironmentId ? unresolvedState.items : [];
  const displayedLoading = Boolean(selectedEnvironmentId) && unresolvedState.environmentId !== selectedEnvironmentId;
  const displayedError = selectedEnvironmentId && unresolvedState.environmentId === selectedEnvironmentId ? unresolvedState.error : null;

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      {selectedEnvironment ? (
        <p className="text-xs text-foreground/60">
          {selectedEnvironment.display_name || selectedEnvironment.account_scope} · {selectedEnvironment.mode}
        </p>
      ) : null}

      {selectedEnvironmentId ? (
        <UnresolvedQueuePanel items={displayedItems} loading={displayedLoading} error={displayedError} />
      ) : (
        <Panel title="Unresolved queue" className="p-4 md:p-5">
          <p className="text-sm text-foreground/60">Select an environment to view unresolved items.</p>
        </Panel>
      )}

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
