"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertCircleIcon, FileWarningIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { SectionLabel } from "@/components/operator/section-label";
import { ScreenerEditor } from "@/features/alerts/components/screener-editor";
import { useAlertsScope, useAlertsWorkflow } from "@/features/alerts/hooks/use-alerts-queries";
import { documentToScreenerDraft } from "@/features/alerts/lib/screener-authoring";

/**
 * Screener edit path.
 *
 * Like the alert editor, this refuses to open a form it cannot fully represent
 * rather than saving a document with fields quietly missing.
 */
export default function EditScreenerPage() {
  const params = useParams<{ workflowId: string }>();
  const { scope, isLoading } = useAlertsScope();
  const workflowId = typeof params?.workflowId === "string" ? params.workflowId : "";
  const workflowQuery = useAlertsWorkflow(workflowId, scope, { includeYaml: false });

  if (isLoading || workflowQuery.isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  const workflow = workflowQuery.data;
  if (!workflow) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Screener not found</AlertTitle>
        <AlertDescription>
          {workflowQuery.error instanceof Error
            ? workflowQuery.error.message
            : "This screener does not exist in the selected scope."}
        </AlertDescription>
      </Alert>
    );
  }

  const conversion = documentToScreenerDraft(workflow.document);

  if (!conversion.ok) {
    return (
      <div className="flex flex-col gap-4 pb-8">
        <SectionLabel eyebrow="Screeners" title={`Edit ${workflow.name}`} />
        <Alert>
          <FileWarningIcon />
          <AlertTitle>This screener is not editable in the structured form</AlertTitle>
          <AlertDescription>
            <p>{conversion.reason}</p>
            <p className="mt-2">
              Nothing has been changed. The read-only view shows the full canonical document, where
              no field can be dropped.
            </p>
            <Button asChild variant="outline" size="sm" className="mt-3">
              <Link href={`/alerts/screeners/${workflowId}`}>Open the read-only view</Link>
            </Button>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  const expectedRevision =
    workflow.latest_revision?.revision ?? workflow.active_revision?.revision ?? 1;

  return (
    <ScreenerEditor
      scope={scope}
      initialDraft={conversion.draft}
      edit={{ workflowId, expectedRevision }}
    />
  );
}
