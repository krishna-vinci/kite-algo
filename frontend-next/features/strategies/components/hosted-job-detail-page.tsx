"use client";

import Link from "next/link";
import { useState } from "react";
import { OctagonIcon, ScrollTextIcon, ShieldAlertIcon, WrenchIcon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { SectionLabel } from "@/components/operator/section-label";
import {
  useHostedJob,
  useHostedJobLogPages,
  useHostedJobNotifications,
  useHostedReconciliation,
  useReconcileHostedJob,
  useStopHostedJob,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import {
  hostedErrorMessage,
  jobStatusLabel,
  jobStatusTone,
  stopStateLabel,
} from "@/features/strategies/lib/format";
import { executionModeLabel } from "@/features/strategies/lib/modes";
import type { JobLogEntry } from "@/lib/hosted-strategies/types";

const EXECUTION_QUIESCENCE_UNVERIFIED = "EXECUTION_QUIESCENCE_UNVERIFIED";

function StopCard({ strategyId, jobId, attempt }: Readonly<{
  strategyId: string;
  jobId: string;
  attempt: number;
}>) {
  const stopMutation = useStopHostedJob(strategyId, jobId);

  async function stop() {
    try {
      await stopMutation.mutateAsync({ attempt });
      toast.success("Stop requested. Stop does not cancel orders or flatten positions.");
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  return (
    <div className="flex items-center gap-3">
      <Button variant="outline" onClick={stop} disabled={stopMutation.isPending}>
        <OctagonIcon className="size-4" aria-hidden />
        Stop attempt #{attempt}
      </Button>
      <p className="text-xs text-muted-foreground">
        Stop requests bounded local cleanup. It does not cancel orders or flatten positions.
      </p>
    </div>
  );
}

function LogsCard({ strategyId, jobId }: Readonly<{ strategyId: string; jobId: string }>) {
  // Each page is its own query, so "Load more" appends instead of replacing the
  // output the operator has already read.
  const [offsets, setOffsets] = useState<number[]>([0]);
  const results = useHostedJobLogPages(strategyId, jobId, offsets);
  const logs = results[0]?.data;
  const logsQuery = {
    isLoading: results.some((result) => result.isLoading),
    isError: results.some((result) => result.isError),
    error: results.find((result) => result.isError)?.error,
  };
  const collected = new Map<number, JobLogEntry>();
  for (const result of results) {
    for (const entry of result.data?.entries ?? []) collected.set(entry.seq, entry);
  }
  const page = [...collected.values()].sort((a, b) => a.seq - b.seq);
  const lastSeq = page.length > 0 ? page[page.length - 1].seq : 0;
  const lastPage = results[results.length - 1]?.data;
  const hasMore = (lastPage?.entries.length ?? 0) >= 200;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ScrollTextIcon className="size-4" aria-hidden />
          Logs
        </CardTitle>
        <CardDescription>
          Bounded output. Unknown state is not “no output”: an unavailable notice means logs were not
          collected.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {logsQuery.isLoading ? (
          <Skeleton className="h-24 w-full rounded-lg" />
        ) : logsQuery.isError ? (
          <Alert variant="destructive">
            <AlertTitle>Could not load logs</AlertTitle>
            <AlertDescription>{hostedErrorMessage(logsQuery.error)}</AlertDescription>
          </Alert>
        ) : !logs || !logs.available ? (
          <Alert>
            <AlertTitle>No logs collected</AlertTitle>
            <AlertDescription>{logs?.notice ?? "Logs are not available for this attempt."}</AlertDescription>
          </Alert>
        ) : (
          <>
            {logs.truncated ? (
              <Alert variant="destructive">
                <AlertTitle>Output was truncated</AlertTitle>
                <AlertDescription>
                  Stored output hit the size cap; some output was discarded and is not recoverable here.
                </AlertDescription>
              </Alert>
            ) : null}
            <pre className="max-h-96 overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-xs">
              {page.map((entry) => `[${entry.seq}] ${entry.content}`).join("")}
            </pre>
            <p className="text-xs text-muted-foreground">
              {logs.source === "post_termination"
                ? "Collected after the child terminated (live streaming is not implemented). "
                : `${logs.notice} `}
              Showing {page.length} chunk(s) up to seq {lastSeq}.
            </p>
            {hasMore ? (
              <Button
                size="sm"
                variant="outline"
                className="self-start"
                onClick={() => setOffsets((current) => [...current, lastPage?.next_seq ?? lastSeq])}
              >
                Load more
              </Button>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function NotificationsCard({ strategyId, jobId }: Readonly<{ strategyId: string; jobId: string }>) {
  const query = useHostedJobNotifications(strategyId, jobId);
  const events = query.data?.events ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Notifications</CardTitle>
        <CardDescription>
          Delivery status per channel. Provider acceptance is not proof of receipt.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {query.isLoading ? (
          <Skeleton className="h-20 w-full rounded-lg" />
        ) : events.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No notifications for this attempt{query.data?.run_id ? "" : " (no run yet)"}.
          </p>
        ) : (
          events.map((event) => (
            <div key={event.event_id} className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <p className="text-sm font-medium">{event.subject ?? "Run notification"}</p>
                <span className="text-xs text-muted-foreground">{event.fired_at ?? ""}</span>
              </div>
              <p className="text-sm text-muted-foreground">{event.text}</p>
              {event.deliveries.map((delivery) => (
                <div key={delivery.delivery_id} className="rounded-lg border px-3 py-2">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{delivery.channel_name ?? delivery.channel_id}</span>
                    <Badge variant={delivery.status === "delivered" ? "default" : "secondary"}>
                      {delivery.status}
                    </Badge>
                  </div>
                  {delivery.last_error ? (
                    <p className="mt-1 text-xs text-destructive">{delivery.last_error}</p>
                  ) : null}
                  <ul className="mt-1 flex flex-col gap-0.5 text-xs text-muted-foreground">
                    {delivery.attempt_history.map((attempt) => (
                      <li key={`${delivery.delivery_id}-${attempt.attempt_no}`}>
                        attempt {attempt.attempt_no}: {attempt.outcome}
                        {attempt.detail ? ` — ${attempt.detail}` : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              <Separator />
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function ReconciliationCard({ strategyId, jobId, attempt }: Readonly<{
  strategyId: string;
  jobId: string;
  attempt: number;
}>) {
  const query = useHostedReconciliation(strategyId, jobId);
  const reconcileMutation = useReconcileHostedJob(strategyId, jobId);
  const inspection = query.data;
  const assessment = inspection?.assessment;
  const quiescenceBlocked =
    assessment?.blocking_reasons?.includes(EXECUTION_QUIESCENCE_UNVERIFIED) ?? false;

  async function reconcile() {
    try {
      await reconcileMutation.mutateAsync({ attempt });
      toast.success("Reconciled: the replacement block is cleared.");
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <WrenchIcon className="size-4" aria-hidden />
          Reconciliation
        </CardTitle>
        <CardDescription>
          The server decides whether evidence supports unblocking; the browser cannot assert
          “flat” or “reconciled”.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {query.isLoading ? (
          <Skeleton className="h-20 w-full rounded-lg" />
        ) : query.isError ? (
          <Alert variant="destructive">
            <AlertTitle>Could not load reconciliation evidence</AlertTitle>
            <AlertDescription>{hostedErrorMessage(query.error)}</AlertDescription>
          </Alert>
        ) : assessment ? (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <Badge variant={assessment.allowed ? "default" : "secondary"}>
                {assessment.allowed ? "Ready to reconcile" : "Blocked"}
              </Badge>
              <span className="text-sm text-muted-foreground">case: {assessment.case}</span>
              <span className="text-sm text-muted-foreground">reason: {assessment.reason_code}</span>
            </div>
            {assessment.blocking_reasons.length > 0 ? (
              <Alert variant="destructive">
                <AlertTitle className="flex items-center gap-2">
                  <ShieldAlertIcon className="size-4" aria-hidden />
                  Blocking reasons
                </AlertTitle>
                <AlertDescription>
                  <ul className="list-disc pl-4">
                    {assessment.blocking_reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                  {quiescenceBlocked ? (
                    <p className="mt-2 text-xs">
                      Execution quiescence is unverified. This block is not dismissible: trading-capable
                      reconciliation stays blocked until the server can establish it.
                    </p>
                  ) : null}
                </AlertDescription>
              </Alert>
            ) : null}
            <details>
              <summary className="cursor-pointer text-sm text-muted-foreground">Evidence</summary>
              <pre className="mt-2 max-h-72 overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-xs">
                {JSON.stringify(inspection.evidence, null, 2)}
              </pre>
            </details>
            <Separator />
            <div className="flex items-center gap-3">
              <Button
                onClick={reconcile}
                disabled={!assessment.allowed || reconcileMutation.isPending}
              >
                Request reconciliation
              </Button>
              <p className="text-xs text-muted-foreground">
                {assessment.allowed
                  ? "Clears the replacement block for this attempt only."
                  : "Unavailable until the server’s evidence supports it."}
              </p>
            </div>
            {inspection.history.length > 0 ? (
              <div>
                <p className="mb-1 text-sm font-medium">History</p>
                <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
                  {inspection.history.map((entry) => (
                    <li key={entry.id}>
                      {entry.created_at ?? ""} — {entry.outcome} ({entry.reason_code}) by {entry.actor_id}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function HostedJobDetailPage({ strategyId, jobId }: Readonly<{ strategyId: string; jobId: string }>) {
  const jobQuery = useHostedJob(strategyId, jobId);

  if (jobQuery.isLoading) {
    return <Skeleton className="h-40 w-full rounded-xl" />;
  }
  if (jobQuery.isError || !jobQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load job</AlertTitle>
        <AlertDescription>{hostedErrorMessage(jobQuery.error)}</AlertDescription>
      </Alert>
    );
  }

  const job = jobQuery.data;
  const launched = job.handoff_at !== null;
  const cleanupConfirmed = job.process_cleanup_state === "confirmed";
  // Queued attempts are stoppable too: stopping one prevents its launch.
  const stoppable = ["queued", "starting", "running"].includes(job.status);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <Link href={`/strategies/${strategyId}`} className="text-xs underline">
            Back to strategy
          </Link>
          <SectionLabel
            eyebrow={`Hosted attempt · job ${job.job_id}`}
            title={`Attempt #${job.attempt}`}
          />
        </div>
        <span className={`inline-flex rounded-full border px-3 py-1 text-sm font-medium ${jobStatusTone(job.status)}`}>
          {jobStatusLabel(job.status)}
        </span>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Run state</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm text-muted-foreground">
            <p>desired: {job.desired_state}</p>
            <p>mode: {executionModeLabel(job.execution_mode)}</p>
            <p>scope: {job.account_scope}</p>
            <p>run: {job.run_id ?? "—"}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Process cleanup</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm text-muted-foreground">
            {!launched ? (
              <p>Not launched — no child process.</p>
            ) : (
              <>
                <p>state: {job.process_cleanup_state ?? "unconfirmed"}</p>
                <p>at: {job.process_cleanup_at ?? "—"}</p>
                <p className="text-xs">Unknown cleanup is not confirmed stopped.</p>
              </>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Replacement</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm text-muted-foreground">
            <p>{job.replacement_blocked ? "Blocked until reconciled" : "Clear"}</p>
            {job.recovery_required_at ? <p>recovery required at {job.recovery_required_at}</p> : null}
            {job.reconciled_at ? <p>reconciled at {job.reconciled_at}</p> : null}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Stop</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground" data-testid="stop-state">
            {stopStateLabel(job.stop.state, launched)} — {job.stop.note}
          </p>
          {stoppable ? (
            <StopCard strategyId={strategyId} jobId={jobId} attempt={job.attempt} />
          ) : (
            <p className="text-xs text-muted-foreground">
              {job.replacement_blocked
                ? "Already terminal; the replacement block stays until reconciliation."
                : "Already terminal; nothing to stop."}
            </p>
          )}
          {stoppable && !launched ? (
            <p className="text-xs text-muted-foreground">
              This attempt was never launched; stopping it prevents launch without needing any process
              cleanup.
            </p>
          ) : null}
          {launched && cleanupConfirmed ? (
            <p className="text-xs text-muted-foreground">Process cleanup confirmed.</p>
          ) : null}
        </CardContent>
      </Card>

      <LogsCard strategyId={strategyId} jobId={jobId} />
      <NotificationsCard strategyId={strategyId} jobId={jobId} />
      {job.replacement_blocked ? (
        <ReconciliationCard strategyId={strategyId} jobId={jobId} attempt={job.attempt} />
      ) : null}
    </div>
  );
}
