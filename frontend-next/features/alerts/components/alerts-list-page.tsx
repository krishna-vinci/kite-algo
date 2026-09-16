"use client";

/**
 * The alert list, shaped around decisions rather than implementation columns.
 *
 * A row answers: what is this alert, where is the price now, how far is it from
 * the level, is it running, where does it notify, and when was it last checked —
 * plus the two actions an operator actually takes from a list (pause/resume,
 * edit). Technical fields (revision numbers, hashes, subscription counts, the
 * workflow id) live behind a per-row diagnostics disclosure, never as content.
 *
 * Rows subscribe to the live feed only while they are on screen: a long list
 * scrolled to the bottom holds no subscriptions for the rows above it.
 */

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { AlertCircleIcon, BellRingIcon, PlusIcon } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/operator/status-badge";
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
import { alertsKeys } from "@/features/alerts/hooks/keys";
import {
  useAlertsScope,
  useAlertsLifecycle,
  useAlertsWorkflows,
} from "@/features/alerts/hooks/use-alerts-queries";
import {
  AlertsMarketStreamProvider,
  useInViewport,
  useMarketQuote,
  useQuotePresentation,
} from "@/features/alerts/hooks/use-market-stream";
import { deriveLifecycle } from "@/features/alerts/lib/status";
import { describeAlertState, describeCoverage, describeLastChecked } from "@/features/alerts/lib/alert-state";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";
import { summarizeList } from "@/features/alerts/lib/format";
import { OPERATOR_LABELS } from "@/features/alerts/lib/authoring";
import { describeRule, formatPrice } from "@/features/alerts/lib/plain-language";
import type { AlertsWorkflowSummary } from "@/features/alerts/types";

type Filter = "all" | "active" | "paused" | "draft" | "attention" | "archived";

const FILTERS: Array<{ value: Filter; label: string }> = [
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
  { value: "draft", label: "Draft" },
  { value: "attention", label: "Needs attention" },
  { value: "archived", label: "Archived" },
  { value: "all", label: "All" },
];

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

function symbolOf(key: string | undefined): string {
  if (!key) return "";
  const parts = key.split(":");
  return parts.length > 1 ? parts.slice(1).join(":") : key;
}

/** The live price line for one row: price, freshness, and distance to the level. */
function RowQuote({
  workflow,
  inViewport,
}: {
  workflow: AlertsWorkflowSummary;
  inViewport: boolean;
}) {
  const rule = workflow.rule ?? null;
  const single = !workflow.has_universe && workflow.instruments.length === 1;
  const instrumentKey = single ? workflow.instruments[0] : null;
  // Registering with a null key is a no-op, so a multi-instrument row holds no
  // single-instrument subscription.
  const { quote, status } = useMarketQuote(inViewport ? instrumentKey : null);
  const presentation = useQuotePresentation(quote, status);

  if (!single) {
    return (
      <span className="text-sm text-muted-foreground">
        {describeCoverage(workflow.instruments, workflow.has_universe, [])}
      </span>
    );
  }

  return (
    <div className="flex flex-col gap-0.5">
      <span className="flex items-center gap-2">
        <span className="text-sm font-medium tabular-nums">{formatPrice(presentation.price)}</span>
        <StatusBadge tone={presentation.tone}>{presentation.label}</StatusBadge>
      </span>
      {rule ? (
        <span className="text-xs text-muted-foreground">
          {describeTargetLine(rule.value, presentation.price, rule.operator)}
        </span>
      ) : null}
    </div>
  );
}

function describeTargetLine(
  target: number | null | undefined,
  price: number | null | undefined,
  operator: string,
): string {
  if (target === null || target === undefined) return "";
  if (price === null || price === undefined) return `target ${formatPrice(target)}`;
  const distance = target - price;
  const direction = distance >= 0 ? "below" : "above";
  const upward = ["crosses_above", "gt", "gte", "rises_pct", "rose_pct"].includes(operator);
  const already = upward ? price > target : price < target;
  if (already) return `price is already past ${formatPrice(target)}`;
  return `${formatPrice(Math.abs(distance))} ${direction} target`;
}

function WorkflowRow({
  workflow,
  scope,
}: Readonly<{ workflow: AlertsWorkflowSummary; scope: string | null }>) {
  // Screeners have their own inspection surface (runs + attachment baselines);
  // sending them to the generic definition view would hide the run history.
  const href =
    workflow.kind === "screener"
      ? `/alerts/screeners/${workflow.workflow_id}`
      : `/alerts/${workflow.workflow_id}`;
  const { ref, inViewport } = useInViewport<HTMLTableRowElement>();
  const single = !workflow.has_universe && workflow.instruments.length === 1;
  const { quote } = useMarketQuote(inViewport ? (single ? workflow.instruments[0] : null) : null);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const lifecycle = deriveLifecycle(workflow);
  const rule = workflow.rule ?? null;
  const view = describeAlertState({
    lifecycle,
    hasErrorWarning: workflow.warnings.some((warning) => warning.severity === "error"),
    hasWarning: workflow.warnings.length > 0,
    lastEvaluatedAt: workflow.freshness.last_evaluated_at ?? null,
    quote,
  });
  const { pause, resume } = useAlertsLifecycle(workflow.workflow_id, scope);
  const busy = pause.isPending || resume.isPending;

  return (
    <>
      <TableRow ref={ref}>
        <TableCell className="max-w-[22rem]">
          <Link href={href} className="block truncate font-medium hover:underline">
            {rule
              ? describeRule(
                  workflow.instrument_summary ?? symbolOf(workflow.instruments[0]),
                  OPERATOR_LABELS[rule.operator] ?? rule.operator,
                  rule.value,
                )
              : workflow.name}
          </Link>
          {rule ? (
            <p className="truncate text-xs text-muted-foreground">{workflow.name}</p>
          ) : (
            <p className="truncate text-xs text-muted-foreground">
              {workflow.instrument_summary ?? summarizeList(workflow.instruments)}
            </p>
          )}
        </TableCell>
        <TableCell>
          <RowQuote workflow={workflow} inViewport={inViewport} />
        </TableCell>
        <TableCell>
          <div className="flex flex-col items-start gap-1">
            <StatusBadge tone={view.tone}>{view.label}</StatusBadge>
            <span className="text-xs text-muted-foreground">
              {workflow.channels.length > 0 ? summarizeList(workflow.channels, 2) : "no destination"}
            </span>
            <span className="text-xs text-muted-foreground">
              {describeLastChecked(
                workflow.freshness.evaluation_age_s,
                workflow.freshness.last_evaluated_at ?? null,
              )}
            </span>
          </div>
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap items-center gap-2">
            {lifecycle === "paused" ? (
              <Button size="xs" variant="outline" disabled={busy} onClick={() => resume.mutate()}>
                Resume
              </Button>
            ) : lifecycle === "active" ? (
              <Button size="xs" variant="outline" disabled={busy} onClick={() => pause.mutate()}>
                Pause
              </Button>
            ) : null}
            {workflow.kind !== "screener" ? (
              <Button asChild size="xs" variant="outline">
                <Link href={`/alerts/${workflow.workflow_id}/edit`}>Edit</Link>
              </Button>
            ) : null}
            <Button
              size="xs"
              variant="ghost"
              aria-expanded={showDiagnostics}
              onClick={() => setShowDiagnostics((value) => !value)}
            >
              {showDiagnostics ? "Hide details" : "Details"}
            </Button>
          </div>
          {pause.error || resume.error ? (
            <p className="mt-1 text-xs text-rose-300" role="alert">
              {alertsErrorMessage(pause.error ?? resume.error, "the change was refused")}
            </p>
          ) : null}
        </TableCell>
      </TableRow>
      {showDiagnostics ? (
        <TableRow>
          <TableCell colSpan={4} className="bg-background/40 text-xs text-muted-foreground">
            <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-3">
              <div>
                <dt className="inline font-medium">Revision </dt>
                <dd className="inline">
                  {workflow.active_revision?.revision ?? "—"} active /{" "}
                  {workflow.latest_revision?.revision ?? "—"} latest
                </dd>
              </div>
              <div>
                <dt className="inline font-medium">Subscriptions </dt>
                <dd className="inline">{workflow.freshness.subscription_count}</dd>
              </div>
              <div>
                <dt className="inline font-medium">Stale subscriptions </dt>
                <dd className="inline">{workflow.freshness.stale_subscriptions}</dd>
              </div>
              <div>
                <dt className="inline font-medium">Session </dt>
                <dd className="inline">{workflow.session ?? "—"}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="inline font-medium">Workflow id </dt>
                <dd className="inline font-mono">{workflow.workflow_id}</dd>
              </div>
            </dl>
            {workflow.warnings.length > 0 ? (
              <ul className="mt-2 list-disc pl-4">
                {workflow.warnings.map((warning) => (
                  <li key={`${warning.code}-${warning.where}`}>
                    {warning.message} <span className="font-mono">({warning.code})</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </TableCell>
        </TableRow>
      ) : null}
    </>
  );
}

/**
 * The list owns the stream for its rows: one connection for the page, opened
 * when the first visible row registers an instrument.
 */
export function AlertsListPage() {
  const { scope } = useAlertsScope();
  return (
    <AlertsMarketStreamProvider scope={scope}>
      <AlertsListPageInner />
    </AlertsMarketStreamProvider>
  );
}

function AlertsListPageInner() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const includeArchived = searchParams?.get("archived") === "true";
  const initialFilter = (searchParams?.get("filter") as Filter | null) ?? "active";
  const [filter, setFilter] = useState<Filter>(initialFilter);
  const [search, setSearch] = useState("");
  const {
    scope,
    scopes,
    setScope,
    isLoading: scopesLoading,
    isError: scopesError,
    error: scopesErrorValue,
    fellBack,
  } = useAlertsScope();
  // Archived rows are only meaningful when they are in scope, so asking for them
  // turns the archived filter on.
  const wantArchived = includeArchived || filter === "archived";
  const workflowsQuery = useAlertsWorkflows(scope, wantArchived);
  const queryClient = useQueryClient();

  const workflows = workflowsQuery.data?.workflows ?? [];
  const visible = workflows.filter((workflow) => {
    const lifecycle = deriveLifecycle(workflow);
    if (filter === "active" && lifecycle !== "active") return false;
    if (filter === "paused" && lifecycle !== "paused") return false;
    if (filter === "draft" && lifecycle !== "draft") return false;
    if (filter === "archived" && lifecycle !== "archived") return false;
    if (filter === "attention" && !workflow.warnings.some((warning) => warning.severity === "error")) {
      return false;
    }
    if (search.trim()) {
      const needle = search.trim().toLowerCase();
      const haystack = [
        workflow.name,
        workflow.instrument_summary ?? "",
        ...workflow.instruments,
      ]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });

  const setIncludeArchived = (next: boolean) => {
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    if (next) params.set("archived", "true");
    else params.delete("archived");
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname);
  };

  const refresh = useMutation({
    mutationFn: async () => {
      await queryClient.invalidateQueries({ queryKey: alertsKeys.workflows(scope, wantArchived) });
    },
  });

  return (
    <div className="flex flex-col gap-6 pb-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionLabel
          eyebrow="Alerts"
          title="Alerts and screeners"
          description="What is watching what, where the price is, and when it was last checked. Alerts never place orders."
        />
        <div className="flex items-center gap-3">
          <ScopeSelect
            scopes={scopes}
            value={scope}
            onChange={setScope}
            disabled={scopesLoading}
          />
          <Button asChild size="sm" variant="outline">
            <Link href="/alerts/operations">Operations</Link>
          </Button>
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

      <div className="flex flex-wrap items-center gap-3">
        <div role="tablist" aria-label="Filter alerts" className="flex flex-wrap gap-2">
          {FILTERS.map((option) => (
            <button
              key={option.value}
              type="button"
              role="tab"
              aria-selected={filter === option.value}
              onClick={() => setFilter(option.value)}
              className={
                filter === option.value
                  ? "rounded-full border border-primary/60 bg-primary/10 px-3 py-1 text-xs text-primary"
                  : "rounded-full border border-border/60 px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
              }
            >
              {option.label}
            </button>
          ))}
        </div>
        <Input
          aria-label="Search alerts by name or instrument"
          className="max-w-xs"
          placeholder="Search name or instrument"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="flex items-center gap-2">
          <Checkbox
            id="alerts-include-archived"
            checked={wantArchived}
            onCheckedChange={(checked) => setIncludeArchived(checked === true)}
          />
          <label htmlFor="alerts-include-archived" className="text-sm text-muted-foreground">
            Include archived
          </label>
        </div>
        <Button
          size="sm"
          variant="ghost"
          disabled={refresh.isPending}
          onClick={() => refresh.mutate()}
        >
          {refresh.isPending ? "Refreshing…" : "Refresh"}
        </Button>
      </div>

      {scopesLoading || workflowsQuery.isLoading ? (
        <ListSkeleton />
      ) : scopesError ? (
        // The authorized-scope list itself failed. Without it there is no
        // scope to query, so show the failure rather than an empty list that
        // would read as "you have no alerts".
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Could not load authorized scopes</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(scopesErrorValue, "The scopes request failed.")}
          </AlertDescription>
        </Alert>
      ) : workflowsQuery.error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Failed to load alerts</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(workflowsQuery.error, "Unknown error")}
          </AlertDescription>
        </Alert>
      ) : workflows.length === 0 ? (
        <EmptyState />
      ) : visible.length === 0 ? (
        <Alert>
          <AlertCircleIcon />
          <AlertTitle>No alerts match this filter</AlertTitle>
          <AlertDescription>
            {workflows.length} alert{workflows.length === 1 ? "" : "s"} exist in this scope but none
            match “{filter}”{search.trim() ? ` with “${search.trim()}”` : ""}.
          </AlertDescription>
        </Alert>
      ) : (
        <div className="rounded-xl border border-border/70 bg-card/60">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Alert</TableHead>
                <TableHead>Price and target</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visible.map((workflow) => (
                <WorkflowRow
                  key={workflow.workflow_id}
                  workflow={workflow}
                  scope={scope}
                        />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
