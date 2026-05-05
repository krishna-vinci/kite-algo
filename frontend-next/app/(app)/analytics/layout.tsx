"use client";

import { WorkspaceProvider } from "@/components/workspace/workspace-provider";
import { AnalyticsShell } from "@/components/analytics/analytics-shell";

export default function AnalyticsLayout({ children }: { children: React.ReactNode }) {
  return (
    <WorkspaceProvider>
      <AnalyticsShell>{children}</AnalyticsShell>
    </WorkspaceProvider>
  );
}
