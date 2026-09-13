"use client";

import Link from "next/link";
import { AlertCircleIcon, FileWarningIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { SectionLabel } from "@/components/operator/section-label";
import { AlertWizard } from "@/features/alerts/components/alert-wizard";
import { useAlertsWorkflow } from "@/features/alerts/hooks/use-alerts-queries";
import { documentToDraft } from "@/features/alerts/lib/authoring";

/**
 * Edit path.
 *
 * If the stored definition uses anything the structured editor does not model,
 * this page REFUSES to open a form and points at the read-only view instead.
 * Opening a form that silently omitted a `sequence` would look like a
 * successful save while destroying the field — so the honest failure is the
 * correct behaviour here.
 */
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

  if (!conversion.ok) {
    return (
      <div className="flex flex-col gap-4 pb-8">
        <SectionLabel eyebrow="Alerts" title={`Edit ${workflow.name}`} />
        <Alert>
          <FileWarningIcon />
          <AlertTitle>This definition is not editable in the structured form</AlertTitle>
          <AlertDescription>
            <p>{conversion.reason}</p>
            <p className="mt-2">
              Nothing has been changed. Open the definition in the read-only view, where the full
              canonical document and the YAML are both shown — editing there will not drop any
              field.
            </p>
            <Button asChild variant="outline" size="sm" className="mt-3">
              <Link href={`/alerts/${workflowId}`}>Open the read-only definition</Link>
            </Button>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  const expectedRevision =
    workflow.latest_revision?.revision ?? workflow.active_revision?.revision ?? 1;

  return (
    <AlertWizard
      scope={scope}
      initialDraft={conversion.draft}
      edit={{ workflowId, expectedRevision }}
    />
  );
}
