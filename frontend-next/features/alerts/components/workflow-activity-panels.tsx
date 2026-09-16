"use client";

import { AlertCircleIcon } from "lucide-react";
import { Fragment, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import {
  useAlertsWorkflowDeliveries,
  useAlertsWorkflowEvents,
} from "@/features/alerts/hooks/use-alerts-queries";
import { formatTimestamp } from "@/features/alerts/lib/format";
import { deliveryStatusLabel } from "@/features/alerts/lib/health";

export function WorkflowEventsPanel({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const [offset, setOffset] = useState(0);
  const limit = 50;
  const eventsQuery = useAlertsWorkflowEvents(workflowId, scope, limit, offset);

  if (eventsQuery.isLoading) return <Skeleton className="h-40 w-full rounded-xl" />;
  if (eventsQuery.error || !eventsQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Events unavailable</AlertTitle>
        <AlertDescription>
          {eventsQuery.error instanceof Error ? eventsQuery.error.message : "No data."}
        </AlertDescription>
      </Alert>
    );
  }

  const { events, total } = eventsQuery.data;

  return (
    <div className="flex flex-col gap-3">
      {events.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No signal events recorded. Events are retained even after archiving.
        </p>
      ) : (
        <div className="rounded-xl border border-border/70 bg-card/60">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Fired</TableHead>
                <TableHead>Subscription</TableHead>
                <TableHead>Evidence</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((event) => {
                const evidence = Object.entries(event.evidence ?? {}).slice(0, 4);
                return (
                  <TableRow key={event.event_id}>
                    <TableCell className="whitespace-nowrap text-sm">
                      {formatTimestamp(event.fired_at) ?? "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {event.subscription_id ?? "workflow-level"}
                    </TableCell>
                    <TableCell className="text-xs">
                      <ul className="flex flex-wrap gap-x-3 gap-y-0.5">
                        {evidence.map(([key, value]) => (
                          <li key={key}>
                            <span className="text-muted-foreground">{key}:</span>{" "}
                            {typeof value === "object" ? JSON.stringify(value) : String(value)}
                          </li>
                        ))}
                      </ul>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        <span>
          {total} event{total === 1 ? "" : "s"}
        </span>
        <Button
          variant="outline"
          size="xs"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - limit))}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="xs"
          disabled={offset + limit >= total}
          onClick={() => setOffset(offset + limit)}
        >
          Next
        </Button>
      </div>
    </div>
  );
}

export function WorkflowDeliveriesPanel({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const deliveriesQuery = useAlertsWorkflowDeliveries(workflowId, scope, 50, 0, null);

  if (deliveriesQuery.isLoading) return <Skeleton className="h-40 w-full rounded-xl" />;
  if (deliveriesQuery.error || !deliveriesQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Deliveries unavailable</AlertTitle>
        <AlertDescription>
          {deliveriesQuery.error instanceof Error ? deliveriesQuery.error.message : "No data."}
        </AlertDescription>
      </Alert>
    );
  }

  const { deliveries, note } = deliveriesQuery.data;

  return (
    <div className="flex flex-col gap-3">
      {/* "delivered" means the PROVIDER ACCEPTED it — not that a human read it. */}
      <p className="text-xs text-muted-foreground">{note}</p>

      {deliveries.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No deliveries recorded for this workflow yet.
        </p>
      ) : (
        <div className="rounded-xl border border-border/70 bg-card/60">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Created</TableHead>
                <TableHead>Channel</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Attempts</TableHead>
                <TableHead>Provider id</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {deliveries.map((delivery) => {
                const status = deliveryStatusLabel(delivery.status);
                const lastAttempt = delivery.attempt_log[delivery.attempt_log.length - 1];
                return (
                  <Fragment key={delivery.delivery_id}>
                    <TableRow>
                      <TableCell className="whitespace-nowrap text-sm">
                        {formatTimestamp(delivery.created_at) ?? "—"}
                      </TableCell>
                      <TableCell className="text-sm">
                        {delivery.channel_name ?? "—"}{" "}
                        <span className="text-muted-foreground">· {delivery.provider ?? "?"}</span>
                      </TableCell>
                      <TableCell>
                        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
                      </TableCell>
                      <TableCell className="text-sm">
                        {delivery.attempts}
                        {delivery.last_error ? (
                          <span className="ml-2 text-xs text-rose-300" title={delivery.last_error}>
                            last error
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">
                        {lastAttempt?.provider_id ?? "—"}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="ghost"
                          size="xs"
                          aria-expanded={expanded === delivery.delivery_id}
                          onClick={() =>
                            setExpanded(expanded === delivery.delivery_id ? null : delivery.delivery_id)
                          }
                        >
                          {expanded === delivery.delivery_id ? "Hide" : "Attempts"}
                        </Button>
                      </TableCell>
                    </TableRow>
                    {expanded === delivery.delivery_id ? (
                      <TableRow>
                        <TableCell colSpan={6}>
                          <ul className="flex flex-col gap-1 text-xs">
                            {delivery.attempt_log.map((attempt) => (
                              <li key={attempt.attempt_no} className="flex flex-wrap gap-2">
                                <Badge variant="outline">#{attempt.attempt_no}</Badge>
                                <span>{attempt.outcome}</span>
                                <span className="text-muted-foreground">
                                  {formatTimestamp(attempt.created_at) ?? ""}
                                </span>
                                {attempt.detail ? (
                                  <span className="text-muted-foreground">{attempt.detail}</span>
                                ) : null}
                              </li>
                            ))}
                          </ul>
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
