"use client";

import { EnvironmentSelector } from "@/components/journal/environment-selector";
import { JournalHeader } from "@/components/journal/journal-header";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalV2DevNotice } from "@/components/journal/journal-v2-dev-notice";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import type { AnalysisPeriod } from "@/lib/journal/types";

type JournalWorkspaceHeaderProps = {
  period: AnalysisPeriod;
  setPeriod: (next: AnalysisPeriod) => void;
};

export function JournalWorkspaceHeader({ period, setPeriod }: JournalWorkspaceHeaderProps) {
  const { environments, environmentsLoading, environmentsError, selectedEnvironmentId, setSelectedEnvironmentId } =
    useJournalWorkspace();

  return (
    <div className="space-y-4">
      <JournalHeader period={period} onPeriodChange={setPeriod} showPeriodSelector={false} />
      <JournalNav />
      <JournalV2DevNotice />
      <EnvironmentSelector
        environments={environments}
        selectedEnvironmentId={selectedEnvironmentId}
        onSelectEnvironment={setSelectedEnvironmentId}
        loading={environmentsLoading}
        error={environmentsError}
      />
    </div>
  );
}
