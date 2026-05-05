"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useWorkspace } from "@/components/workspace/workspace-provider";

export default function JournalAnalyticsStrategiesIndexPage() {
  const searchParams = useSearchParams();
  const { selectedEnvironmentId: workspaceEnvId, selectedMode } = useWorkspace();
  const envParam = searchParams.get("env") ?? workspaceEnvId ?? "";
  const modeParam = searchParams.get("mode") ?? selectedMode;
  const hasEnv = !!envParam;

  const overviewSp = new URLSearchParams(searchParams.toString());
  if (envParam) overviewSp.set("env", envParam);
  if (modeParam) overviewSp.set("mode", modeParam);

  if (!hasEnv) {
    return (
      <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view strategy analytics.
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
      Select a strategy from the{" "}
      <Link
        href={`/journal/analytics?${overviewSp.toString()}`}
        className="underline underline-offset-2 hover:text-foreground"
      >
        Analytics Overview
      </Link>{" "}
      to view its deep-dive analytics.
    </div>
  );
}
