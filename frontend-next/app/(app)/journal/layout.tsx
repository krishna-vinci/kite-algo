"use client";

import { WorkspaceProvider } from "@/components/workspace/workspace-provider";
import { JournalShell } from "@/components/journal/journal-shell";

export default function JournalLayout({ children }: { children: React.ReactNode }) {
  return (
    <WorkspaceProvider>
      <JournalShell>{children}</JournalShell>
    </WorkspaceProvider>
  );
}
