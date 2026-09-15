"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { AlertCircleIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { AdvancedDefinitionEditor } from "@/features/alerts/components/advanced-definition-editor";
import { AlertsScopeGate } from "@/features/alerts/components/alerts-scope-gate";
import { ScreenerEditor } from "@/features/alerts/components/screener-editor";
import { useAlertsWorkflow } from "@/features/alerts/hooks/use-alerts-queries";
import { alertsErrorMessage, isNotFound } from "@/features/alerts/lib/errors";
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
  const workflowId = typeof params?.workflowId === "string" ? params.workflowId : "";

  if (!workflowId) return <Skeleton className="h-96 w-full rounded-xl" />;

  return (
    <AlertsScopeGate>
      {(scope) => <EditScreenerContent workflowId={workflowId} scope={scope} />}
    </AlertsScopeGate>
  );
}

function EditScreenerContent({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const searchParams = useSearchParams();
  const workflowQuery = useAlertsWorkflow(workflowId, scope);
  const forceAdvanced = searchParams?.get("advanced") === "1";

  if (workflowQuery.isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  const workflow = workflowQuery.data;
  if (!workflow) {
    const notFound = !workflowQuery.error || isNotFound(workflowQuery.error);
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>{notFound ? "Screener not found" : "Could not load this screener"}</AlertTitle>
        <AlertDescription>
          {notFound
            ? "This screener does not exist in the selected scope."
            : alertsErrorMessage(workflowQuery.error, "The request failed.")}
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
