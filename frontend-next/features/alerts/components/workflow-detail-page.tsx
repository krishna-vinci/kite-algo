"use client";

import Link from "next/link";
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  NetworkIcon,
  PencilIcon,
  PlayIcon,
  RotateCcwIcon,
} from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { SectionLabel } from "@/components/operator/section-label";
import { sessionLabel } from "@/features/alerts/lib/authoring";
import { StatusBadge } from "@/components/operator/status-badge";
import { ReadableDefinition } from "@/features/alerts/components/readable-definition";
import {
  WorkflowDeliveriesPanel,
  WorkflowEventsPanel,
} from "@/features/alerts/components/workflow-activity-panels";
import { FreshnessBadge, KindBadge, LifecycleBadge } from "@/features/alerts/components/workflow-badges";
import { WorkflowHealthPanel } from "@/features/alerts/components/workflow-health-panel";
import { OperatorIssueList } from "@/features/alerts/components/operator-issue-list";
import {
  useAlertsLifecycle,
  useAlertsWorkflow,
  useAlertsWorkflowRevisions,
} from "@/features/alerts/hooks/use-alerts-queries";
import { alertsErrorMessage, isNotFound } from "@/features/alerts/lib/errors";
import { formatTimestamp } from "@/features/alerts/lib/format";
import type { AlertsIssue } from "@/features/alerts/types";

function RevisionPicker({
  workflowId,
  scope,
  activeRevision,
}: Readonly<{ workflowId: string; scope: string | null; activeRevision: number | null }>) {
  const revisionsQuery = useAlertsWorkflowRevisions(workflowId, scope);
  const { activate } = useAlertsLifecycle(workflowId, scope);

  const revisions = revisionsQuery.data?.revisions ?? [];
  const candidates = revisions.filter((revision) => revision.revision !== activeRevision);

  // Activation is silent by design when the condition is already true, so the
  // response's own note is surfaced rather than a generic success toast.
  const [note, setNote] = useState<string | null>(null);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={activate.isPending}
          onClick={() =>
            activate.mutate(null, {
              onSuccess: (result) => setNote(result.note ?? "Activated."),
            })
          }
        >
          <PlayIcon className="size-4" aria-hidden />
          Activate latest
        </Button>
        {activate.error ? (
          <span className="text-xs text-rose-300">
            {alertsErrorMessage(activate.error, "Activation failed")}
          </span>
        ) : null}
      </div>

      {candidates.length > 0 ? (
        <details className="rounded-lg border border-border/60 p-3">
          {/* These are revisions other than the active one — a newer draft as
              well as an older revision — so the wording says "another", not
              "earlier". Activating one makes it the revision in force. */}
          <summary className="cursor-pointer text-xs text-muted-foreground">
            Switch to another revision ({candidates.length})
          </summary>
          <ul className="mt-2 flex flex-col gap-1">
            {candidates.map((revision) => (
              <li key={revision.revision_id ?? revision.revision} className="flex items-center gap-3 text-sm">
                <span className="font-mono">r{revision.revision}</span>
                <StatusBadge tone={revision.status === "active" ? "positive" : "neutral"}>
                  {revision.status}
                </StatusBadge>
                <span className="text-xs text-muted-foreground">
                  {formatTimestamp(revision.created_at) ?? "—"}
                </span>
                <span className="font-mono text-[10px] text-muted-foreground">
                  {(revision.canonical_hash ?? "").slice(0, 12)}
                </span>
                <Button
                  variant="outline"
                  size="xs"
                  disabled={activate.isPending}
                  onClick={() =>
                    activate.mutate(revision.revision, {
                      onSuccess: (result) =>
                        setNote(
                          `Switched to r${revision.revision}. ${
                            result.subscriptions_created != null
                              ? `${result.subscriptions_created} subscription(s) materialized. `
                              : ""
                          }${result.note ?? ""}`,
                        ),
                    })
                  }
                >
                  <RotateCcwIcon className="size-3" aria-hidden />
                  Activate
                </Button>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {note ? (
        <Alert>
          <AlertTitle>Activation</AlertTitle>
          <AlertDescription>{note}</AlertDescription>
        </Alert>
      ) : null}
    </div>
  );
}

export function WorkflowDetailPage({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const workflowQuery = useAlertsWorkflow(workflowId, scope);
  const { pause, resume, archive } = useAlertsLifecycle(workflowId, scope);
  const [actionError, setActionError] = useState<string | null>(null);

  if (workflowQuery.isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  if (workflowQuery.error || !workflowQuery.data) {
    // A 404 means "not here" (which also covers a foreign owner id, by design).
    // Anything else — 403 scope, 503 dependency, network — is a different
    // problem and must not be presented as "this alert does not exist".
    const notFound = !workflowQuery.error || isNotFound(workflowQuery.error);
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>{notFound ? "Alert not found" : "Could not load this alert"}</AlertTitle>
        <AlertDescription>
          {notFound
            ? "This alert does not exist in the selected scope."
            : alertsErrorMessage(workflowQuery.error, "The request failed.")}
        </AlertDescription>
      </Alert>
    );
  }

  const workflow = workflowQuery.data;
  const activeRevision = workflow.active_revision?.revision ?? null;
  const isActive = Boolean(workflow.active_revision);
  const archived = Boolean(workflow.archived);
  const issues: AlertsIssue[] = workflow.warnings ?? [];

  const editHref =
    workflow.kind === "screener"
      ? `/alerts/screeners/${workflowId}/edit`
      : `/alerts/${workflowId}/edit`;

  const run = (mutation: { mutate: (arg?: never, opts?: { onError?: (e: unknown) => void }) => void }) =>
    mutation.mutate(undefined, {
      onError: (error) => setActionError(alertsErrorMessage(error, "Action failed")),
    });

  return (
    <div className="flex flex-col gap-6 pb-8">
      <div>
        <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
          <Link href="/alerts">
            <ArrowLeftIcon className="size-4" aria-hidden />
            All alerts
          </Link>
        </Button>

        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex flex-col gap-2">
            <SectionLabel eyebrow="Alert" title={workflow.name} />
            <div className="flex flex-wrap items-center gap-2">
              <KindBadge kind={workflow.kind} />
              <LifecycleBadge workflow={workflow} />
              <FreshnessBadge freshness={workflow.freshness} />
            </div>
            <p className="text-sm text-muted-foreground">
              {workflow.instrument_summary ?? "no coverage"} · {sessionLabel(workflow.session ?? "")} ·{" "}
              {workflow.subscription_count} subscription(s)
            </p>
            {/* The identifier is an implementation detail: useful when quoting a
                specific definition, never the headline. */}
            <details className="text-xs text-muted-foreground">
              <summary className="cursor-pointer">Technical details</summary>
              <p className="mt-1 font-mono text-[10px]">{workflow.workflow_id}</p>
            </details>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {/* The edit and canvas pages are otherwise unreachable: nothing
                else links to them for alerts. */}
            {!archived ? (
              <>
                <Button asChild variant="outline" size="sm">
                  <Link href={editHref}>
                    <PencilIcon className="size-4" aria-hidden />
                    Edit
                  </Link>
                </Button>
                <Button asChild variant="outline" size="sm">
                  <Link href={`/alerts/${workflowId}/canvas`}>
                    <NetworkIcon className="size-4" aria-hidden />
                    Canvas
                  </Link>
                </Button>
              </>
            ) : null}

            {archived ? (
              // Archiving clears the active revision, so Resume would 409.
              // The only lifecycle action left is to activate a revision again.
              <span className="text-xs text-muted-foreground">
                Archived — activate a revision to bring it back.
              </span>
            ) : isActive ? (
              <Button
                variant="outline"
                size="sm"
                disabled={pause.isPending}
                onClick={() => run(pause as never)}
              >
                Pause
              </Button>
            ) : (
              <Button
                variant="outline"
                size="sm"
                disabled={resume.isPending}
                onClick={() => run(resume as never)}
              >
                Resume
              </Button>
            )}

            {!archived ? (
              <Button
                variant="outline"
                size="sm"
                disabled={archive.isPending}
                onClick={() => run(archive as never)}
              >
                Archive
              </Button>
            ) : null}
          </div>
        </div>
      </div>

      {issues.length > 0 ? <OperatorIssueList issues={issues} /> : null}

      {actionError ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Action failed</AlertTitle>
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      ) : null}

      <RevisionPicker workflowId={workflowId} scope={scope} activeRevision={activeRevision} />

      <Tabs defaultValue="definition">
        <TabsList variant="line">
          <TabsTrigger value="definition">Definition</TabsTrigger>
          <TabsTrigger value="yaml">YAML</TabsTrigger>
          <TabsTrigger value="health">Health</TabsTrigger>
          <TabsTrigger value="events">Events</TabsTrigger>
          <TabsTrigger value="deliveries">Deliveries</TabsTrigger>
        </TabsList>

        <TabsContent value="definition" className="pt-4">
          <ReadableDefinition document={workflow.document} />
        </TabsContent>

        <TabsContent value="yaml" className="pt-4">
          {workflow.yaml ? (
            <div className="flex flex-col gap-2">
              <p className="text-xs text-muted-foreground">
                This YAML parses back to the canonical hash{" "}
                <span className="font-mono">
                  {(workflow.revision_in_force?.canonical_hash ?? "").slice(0, 16)}
                </span>
                . The form editor, the canvas and this text are views of one definition.
              </p>
              <pre className="max-h-[36rem] overflow-auto rounded-lg border border-border/60 bg-background/60 p-4 text-xs">
                {workflow.yaml}
              </pre>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              {workflow.yaml_error
                ? `The stored definition could not be rendered as YAML: ${workflow.yaml_error}`
                : "No YAML available for this revision."}
            </p>
          )}
        </TabsContent>

        <TabsContent value="health" className="pt-4">
          <WorkflowHealthPanel workflowId={workflowId} scope={scope} />
        </TabsContent>

        <TabsContent value="events" className="pt-4">
          <WorkflowEventsPanel workflowId={workflowId} scope={scope} />
        </TabsContent>

        <TabsContent value="deliveries" className="pt-4">
          <WorkflowDeliveriesPanel workflowId={workflowId} scope={scope} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
