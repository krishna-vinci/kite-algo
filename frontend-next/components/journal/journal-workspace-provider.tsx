/**
 * Compatibility re-export — maintains the old import path.
 * New code should import from `@/components/workspace/workspace-provider`.
 */
export {
  WorkspaceProvider as JournalWorkspaceProvider,
  useWorkspace as useJournalWorkspace,
  useOptionalWorkspace as useOptionalJournalWorkspace,
} from "@/components/workspace/workspace-provider";

export type { WorkspaceContextValue as JournalWorkspaceContextValue } from "@/components/workspace/workspace-provider";
