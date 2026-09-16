"use client";

/**
 * Edit path — the same authoring page as creation.
 *
 * A stored definition the structured form cannot represent is not a reason to
 * open a different product (or to save something lossy): the editor opens on its
 * Code view tab with the reason shown, and the document is preserved exactly as
 * it is stored. Everything else is edited in place, with the modeled fields
 * merged onto the loaded document so unmodeled keys survive a save.
 */

import { AlertCircleIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { UnifiedAlertEditor } from "@/features/alerts/components/unified-alert-editor";
import { useAlertsWorkflow } from "@/features/alerts/hooks/use-alerts-queries";
import { AlertsMarketStreamProvider } from "@/features/alerts/hooks/use-market-stream";
import { documentToDraft, emptyDraft } from "@/features/alerts/lib/authoring";

export function AlertsEditPage({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const workflowQuery = useAlertsWorkflow(workflowId, scope);

  if (workflowQuery.isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  if (workflowQuery.error || !workflowQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Alert not found</AlertTitle>
        <AlertDescription>
          {workflowQuery.error instanceof Error
            ? workflowQuery.error.message
            : "This alert does not exist in the selected scope."}
        </AlertDescription>
      </Alert>
    );
  }

  const workflow = workflowQuery.data;
  const conversion = documentToDraft(workflow.document);
  const expectedRevision =
    workflow.latest_revision?.revision ?? workflow.active_revision?.revision ?? 1;

  // Code view carries the real document when the form cannot represent it; the
  // draft only supplies the page's chrome (name/session) in that case.
  const stored = (workflow.document ?? {}) as { session?: unknown };
  const initialDraft = conversion.ok
    ? conversion.draft
    : {
        ...emptyDraft(),
        name: workflow.name,
        session: typeof stored.session === "string" ? stored.session : "",
      };

  return (
    <AlertsMarketStreamProvider scope={scope}>
      <UnifiedAlertEditor
        scope={scope}
        mode={{
          kind: "edit",
          workflowId,
          expectedRevision,
          baseDocument: workflow.document ?? null,
          workflowName: workflow.name,
          yaml: workflow.yaml ?? null,
        }}
        initialDraft={initialDraft}
        conversionError={conversion.ok ? undefined : conversion.reason}
      />
    </AlertsMarketStreamProvider>
  );
}
