"use client";

import { EnvironmentSelector } from "@/components/journal/environment-selector";
import { JournalHeader } from "@/components/journal/journal-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import type { AnalysisPeriod } from "@/lib/journal/types";

type JournalWorkspaceHeaderProps = {
  period: AnalysisPeriod;
  setPeriod: (next: AnalysisPeriod) => void;
};

export function JournalWorkspaceHeader({ period, setPeriod }: JournalWorkspaceHeaderProps) {
  const { environments, environmentsLoading, environmentsError, selectedEnvironmentId, selectedMode, setSelectedEnvironmentId, setSelectedMode } =
    useJournalWorkspace();
  const visibleEnvironments = environments.filter((environment) => (environment.mode === "live" ? "live" : "paper") === selectedMode);

  return (
    <div className="space-y-4">
      <JournalHeader period={period} onPeriodChange={setPeriod} showPeriodSelector={false} />
      <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label="Journal modes">
        {(["live", "paper"] as const).map((mode) => {
          const active = selectedMode === mode;
          return (
            <button
              key={mode}
              type="button"
              role="tab"
              aria-selected={active}
              className={`rounded-full border px-3 py-1.5 text-[11px] font-medium uppercase tracking-[0.24em] transition-colors ${active ? "border-primary/40 bg-primary/10 text-primary" : "border-border/70 bg-background/60 text-foreground/70 hover:border-primary/25 hover:text-foreground"}`}
              onClick={() => setSelectedMode(mode)}
            >
              {mode}
            </button>
          );
        })}
      </div>
      <EnvironmentSelector
        environments={visibleEnvironments}
        selectedEnvironmentId={selectedEnvironmentId}
        onSelectEnvironment={setSelectedEnvironmentId}
        label={`${selectedMode} environment`}
        loading={environmentsLoading}
        error={environmentsError}
      />
    </div>
  );
}
