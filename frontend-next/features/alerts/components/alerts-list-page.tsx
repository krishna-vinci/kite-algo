"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { AlertCircleIcon, BellRingIcon, PlusIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SectionLabel } from "@/components/operator/section-label";
import { ScopeSelect } from "@/features/alerts/components/scope-select";
import {
  FreshnessBadge,
  KindBadge,
  LifecycleBadge,
  WarningBadge,
} from "@/features/alerts/components/workflow-badges";
import { summarizeList } from "@/features/alerts/lib/format";
import { useAlertsScope, useAlertsWorkflows } from "@/features/alerts/hooks/use-alerts-queries";
import type { AlertsWorkflowSummary } from "@/features/alerts/types";

function ListSkeleton() {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: 5 }).map((_, index) => (
        <Skeleton key={index} className="h-12 w-full rounded-lg" />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border/70 bg-background/40 px-6 py-12 text-center">
      <BellRingIcon className="size-4 text-muted-foreground/50" aria-hidden />
      <p className="text-sm font-medium text-muted-foreground">No alerts in this scope</p>
      <p className="max-w-md text-xs text-muted-foreground/70">
        An empty list under an authorized scope means no alerts exist for it — not that the
        scope is wrong. Create one to get started.
      </p>
      <Button asChild size="sm" className="mt-2">
        <Link href="/alerts/new">
          <PlusIcon className="size-4" aria-hidden />
          New alert
        </Link>
      </Button>
    </div>
  );
}

function WorkflowRow({ workflow }: Readonly<{ workflow: AlertsWorkflowSummary }>) {
  // Screeners have their own inspection surface (runs + attachment baselines);
  // sending them to the generic definition view would hide the run history.
  const href =
    workflow.kind === "screener"
      ? `/alerts/screeners/${workflow.workflow_id}`
      : `/alerts/${workflow.workflow_id}`;

  return (
    <TableRow>
      <TableCell className="max-w-[18rem]">
        <Link href={href} className="block truncate font-medium hover:underline">
          {workflow.name}
        </Link>
        {workflow.warnings.length > 0 ? (
          <p className="mt-1 truncate text-xs text-muted-foreground" title={workflow.warnings[0].message}>
            {workflow.warnings[0].message}
          </p>
        ) : null}
      </TableCell>
      <TableCell>
        <KindBadge kind={workflow.kind} />
      </TableCell>
      <TableCell>
        <div className="flex flex-col items-start gap-1">
          <LifecycleBadge workflow={workflow} />
          <WarningBadge workflow={workflow} />
        </div>
      </TableCell>
      <TableCell className="max-w-[16rem] text-sm text-muted-foreground">
        {workflow.instrument_summary ?? summarizeList(workflow.instruments)}
      </TableCell>
      <TableCell>
        <FreshnessBadge freshness={workflow.freshness} />
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {workflow.channels.length > 0 ? summarizeList(workflow.channels, 2) : "—"}
      </TableCell>
    </TableRow>
  );
}

export function AlertsListPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const includeArchived = searchParams?.get("archived") === "true";
  const { scope, scopes, setScope, isLoading: scopesLoading, fellBack } = useAlertsScope();
  const workflowsQuery = useAlertsWorkflows(scope, includeArchived);

  const workflows = workflowsQuery.data?.workflows ?? [];

  const setIncludeArchived = (next: boolean) => {
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    if (next) params.set("archived", "true");
    else params.delete("archived");
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname);
  };

  return (
    <div className="flex flex-col gap-6 pb-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionLabel
          eyebrow="Alerts"
          title="Alerts and screeners"
          description="Definitions, lifecycle and evaluation freshness. Alerts never place orders."
        />
        <div className="flex items-center gap-3">
          <ScopeSelect
            scopes={scopes}
            value={scope}
            onChange={setScope}
            disabled={scopesLoading}
          />
          <Button asChild size="sm" variant="outline">
            <Link href="/alerts/universes">Universes</Link>
          </Button>
          <Button asChild size="sm" variant="outline">
            <Link href="/alerts/screeners/new">
              <PlusIcon className="size-4" aria-hidden />
              New screener
            </Link>
          </Button>
          <Button asChild size="sm">
            <Link href="/alerts/new">
              <PlusIcon className="size-4" aria-hidden />
              New alert
            </Link>
          </Button>
        </div>
      </div>

      {fellBack ? (
        <Alert>
          <AlertCircleIcon />
          <AlertTitle>Scope not authorized</AlertTitle>
          <AlertDescription>
            The requested scope is not in your authorized list. Showing the default scope
            instead; the server would have rejected it.
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="flex items-center gap-2">
        <Checkbox
          id="alerts-include-archived"
          checked={includeArchived}
          onCheckedChange={(checked) => setIncludeArchived(checked === true)}
        />
        <label htmlFor="alerts-include-archived" className="text-sm text-muted-foreground">
          Include archived
        </label>
      </div>

      {scopesLoading || workflowsQuery.isLoading ? (
        <ListSkeleton />
      ) : workflowsQuery.error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Failed to load alerts</AlertTitle>
          <AlertDescription>
            {workflowsQuery.error instanceof Error
              ? workflowsQuery.error.message
              : "Unknown error"}
          </AlertDescription>
        </Alert>
      ) : workflows.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="rounded-xl border border-border/70 bg-card/60">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Alert</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Lifecycle</TableHead>
                <TableHead>Coverage</TableHead>
                <TableHead>Evaluation freshness</TableHead>
                <TableHead>Channels</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {workflows.map((workflow) => (
                <WorkflowRow key={workflow.workflow_id} workflow={workflow} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
