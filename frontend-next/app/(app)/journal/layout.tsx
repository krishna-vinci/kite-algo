"use client";

import { JournalWorkspaceProvider } from "@/components/journal/journal-workspace-provider";

export default function JournalLayout({ children }: { children: React.ReactNode }) {
  return <JournalWorkspaceProvider>{children}</JournalWorkspaceProvider>;
}
