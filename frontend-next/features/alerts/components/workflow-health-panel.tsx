"use client";

import { AlertCircleIcon, CircleHelpIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAlertsWorkflowHealth } from "@/features/alerts/hooks/use-alerts-queries";
import { formatAgeAgo } from "@/features/alerts/lib/format";
import {
  describeStaleReason,
  describeSuppression,
  readRuntimeAvailability,
} from "@/features/alerts/lib/health";

/**
 * Per-workflow health: durable facts from the database merged with runtime
 * facts from the worker's health file.
 *
 * The two are never blended. Durable facts are always shown; runtime facts
 * render as UNKNOWN when the health file is unreachable, because reporting
 * "0 quarantined" when the state is unknowable would tell the operator the
 * opposite of the truth.
 */
export function WorkflowHealthPanel({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const healthQuery = useAlertsWorkflowHealth(workflowId, scope);

  if (healthQuery.isLoading) return <Skeleton className="h-48 w-full rounded-xl" />;

  if (healthQuery.error || !healthQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Health unavailable</AlertTitle>
        <AlertDescription>
          {healthQuery.error instanceof Error ? healthQuery.error.message : "No health data returned."}
        </AlertDescription>
      </Alert>
    );
  }

  const health = healthQuery.data;
  const runtime = readRuntimeAvailability(health.runtime);

  return (
    <div className="flex flex-col gap-4">
      <Panel tone="subtle">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={health.lifecycle.active ? "positive" : "neutral"}>
            {health.lifecycle.active ? "active" : "not active"}
          </StatusBadge>
          {health.lifecycle.archived ? <StatusBadge tone="neutral">archived</StatusBadge> : null}
          <span className="text-xs text-muted-foreground">
            revision {health.lifecycle.active_revision?.revision ?? "—"}
          </span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">{health.note}</p>
      </Panel>

      <Panel tone="subtle">
        <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
          Worker runtime
        </p>
        {runtime.kind === "unknown" ? (
          <p className="mt-2 flex items-start gap-2 text-sm text-muted-foreground">
            <CircleHelpIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span>
              <span className="font-medium text-foreground">Unknown</span> ({runtime.reason}).{" "}
              {runtime.note}
            </span>
          </p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-4 text-sm">
            <li>
              quarantined: <span className="text-foreground">{runtime.quarantined}</span>
            </li>
            <li>
              failing subscriptions:{" "}
              <span className="text-foreground">{runtime.failedSubscriptions}</span>
            </li>
          </ul>
        )}
      </Panel>

      <Panel tone="subtle">
        <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
          Suppressions (durable)
        </p>
        {Object.keys(health.suppressions ?? {}).length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">
            No notification has been suppressed for this workflow.
          </p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-4 text-sm">
            {Object.entries(health.suppressions ?? {})
              .sort(([, a], [, b]) => b - a)
              .map(([reason, count]) => (
                <li key={reason} title={describeSuppression(reason)}>
                  {describeSuppression(reason)}: <span className="text-foreground">{count}</span>
                </li>
              ))}
          </ul>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          These counts are persisted when a notification is skipped, so they are available even when
          the worker runtime section above reads as unknown.
        </p>
      </Panel>

      <div className="rounded-xl border border-border/70 bg-card/60">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Instrument</TableHead>
              <TableHead>State</TableHead>
              <TableHead>Last evaluated</TableHead>
              <TableHead>Data</TableHead>
              <TableHead>Failures</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {health.subscriptions.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-sm text-muted-foreground">
                  No active subscriptions. Activate a revision to materialize them.
                </TableCell>
              </TableRow>
            ) : (
              health.subscriptions.map((subscription) => {
                const reason = describeStaleReason(subscription.stale_reason);
                return (
                  <TableRow key={subscription.subscription_id}>
                    <TableCell className="font-mono text-sm">{subscription.instrument_key}</TableCell>
                    <TableCell>
                      <StatusBadge tone={subscription.state === "active" ? "positive" : "neutral"}>
                        {subscription.state}
                      </StatusBadge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatAgeAgo(subscription.evaluation_age_s)}
                    </TableCell>
                    <TableCell className="text-sm">
                      {/* `not_an_ltp_subscription` is informational, not a problem. */}
                      {reason ? (
                        <span
                          className={
                            subscription.stale ? "text-rose-300" : "text-muted-foreground"
                          }
                          title={`${reason.detail} ${reason.action}`}
                        >
                          {reason.label}
                        </span>
                      ) : (
                        <span className="text-emerald-300">
                          flowing
                          {subscription.tick_age_s !== null
                            ? ` · ${formatAgeAgo(subscription.tick_age_s)}`
                            : ""}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-sm">
                      {subscription.failures > 0 ? (
                        <span className="text-rose-300" title={subscription.last_error ?? undefined}>
                          {subscription.failures}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">0</span>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
