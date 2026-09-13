"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { AlertCircleIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { AdvancedDefinitionEditor } from "@/features/alerts/components/advanced-definition-editor";
import { ScreenerEditor } from "@/features/alerts/components/screener-editor";
import { useAlertsScope, useAlertsWorkflow } from "@/features/alerts/hooks/use-alerts-queries";
import { documentToScreenerDraft } from "@/features/alerts/lib/screener-authoring";

/**
 * Screener edit path.
 *
 * Two lossless routes, never a lossy one: the structured form (which merges on
 * the loaded document so unmodeled fields survive), or — when the definition
 * is not representable — the advanced YAML/JSON editor, rather than a form that
 * would silently drop a field on save.
 */
export default function EditScreenerPage() {
  const params = useParams<{ workflowId: string }>();
  const searchParams = useSearchParams();
  const { scope, isLoading } = useAlertsScope();
  const workflowId = typeof params?.workflowId === "string" ? params.workflowId : "";
  const workflowQuery = useAlertsWorkflow(workflowId, scope);
  const forceAdvanced = searchParams?.get("advanced") === "1";

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
        <Link href={`/alerts/screeners/${workflowId}/edit?advanced=1`} className="underline">
          Edit as YAML/JSON instead
        </Link>
      </p>
      <ScreenerEditor
        scope={scope}
        initialDraft={conversion.draft}
        baseDocument={workflow.document}
        edit={{ workflowId, expectedRevision }}
      />
    </div>
  );
}
