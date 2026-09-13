"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AlertCircleIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { AdvancedDefinitionEditor } from "@/features/alerts/components/advanced-definition-editor";
import { AlertWizard } from "@/features/alerts/components/alert-wizard";
import { useAlertsWorkflow } from "@/features/alerts/hooks/use-alerts-queries";
import { documentToDraft } from "@/features/alerts/lib/authoring";

/**
 * Edit path.
 *
 * Two lossless routes, never a lossy one:
 *   - If the stored definition is fully representable, the structured form is
 *     opened, and it merges the modeled fields onto the LOADED document, so
 *     keys it does not model survive a save.
 *   - If it is not representable, the advanced YAML/JSON editor is opened
 *     instead of a form that would silently drop a `sequence`, a pair operand
 *     or an unmodeled key. Opening a lossy form would look like a successful
 *     save while destroying data.
 */
export function AlertsEditPage({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const workflowQuery = useAlertsWorkflow(workflowId, scope);
  const searchParams = useSearchParams();
  const forceAdvanced = searchParams?.get("advanced") === "1";

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

  if (!conversion.ok || forceAdvanced) {
    return (
      <AdvancedDefinitionEditor
        workflowId={workflowId}
        scope={scope}
        name={workflow.name}
        expectedRevision={expectedRevision}
        initialYaml={workflow.yaml ?? null}
        initialDocument={workflow.document}
        reason={conversion.ok ? undefined : conversion.reason}
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted-foreground">
        Editing in the structured form. It keeps every field the form does not model.{" "}
        <Link href={`/alerts/${workflowId}/edit?advanced=1`} className="underline">
          Edit as YAML/JSON instead
        </Link>
      </p>
      <AlertWizard
        scope={scope}
        initialDraft={conversion.draft}
        baseDocument={workflow.document}
        edit={{ workflowId, expectedRevision }}
      />
    </div>
  );
}
