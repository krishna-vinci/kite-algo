"use client";

import Link from "next/link";
import { AlertCircleIcon, ArrowLeftIcon, PlayIcon } from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
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
import {
  useAlertsCapabilities,
  useAlertsScreenerAttachments,
  useAlertsScreenerMutations,
  useAlertsScreenerRun,
  useAlertsScreenerRuns,
  useAlertsWorkflow,
} from "@/features/alerts/hooks/use-alerts-queries";
import { alertsErrorMessage, isNotFound } from "@/features/alerts/lib/errors";
import { formatTimestamp } from "@/features/alerts/lib/format";

function runStatusTone(status: string): "positive" | "warning" | "danger" | "neutral" {
  if (status === "complete") return "positive";
  if (status === "partial") return "warning";
  if (status === "failed") return "danger";
  return "neutral";
}

/** Compact rendering of a member's computed values (score inputs / outputs). */
function summarizeValues(values: Record<string, unknown>): string {
  const entries = Object.entries(values);
  if (entries.length === 0) return "—";
  return entries
    .map(([key, value]) =>
      typeof value === "number" ? `${key}=${Number(value).toFixed(2)}` : `${key}=${String(value)}`,
    )
    .join(" · ");
}

/** Coverage / data-freshness maps returned per run, rendered readably. */
function summarizeRecord(record: Record<string, unknown> | null | undefined): string | null {
  if (!record) return null;
  const entries = Object.entries(record).filter(([, value]) => value !== null && value !== undefined);
  if (entries.length === 0) return null;
  return entries.map(([key, value]) => `${key}: ${String(value)}`).join(" · ");
}

function RunMembers({ runId, scope }: Readonly<{ runId: string; scope: string | null }>) {
  const runQuery = useAlertsScreenerRun(runId, scope);
  if (runQuery.isLoading) return <Skeleton className="h-32 w-full rounded-xl" />;
  if (runQuery.error || !runQuery.data) {
    // Previously returned null on any non-data state, so a failed member fetch
    // rendered as nothing at all instead of as an error.
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Could not load run members</AlertTitle>
        <AlertDescription>
          {alertsErrorMessage(runQuery.error, "The run detail request failed.")}
        </AlertDescription>
      </Alert>
    );
  }

  const { members, member_count, run } = runQuery.data;
  const coverage = summarizeRecord(run.coverage);
  const freshness = summarizeRecord(run.data_freshness);

  return (
    <Panel tone="subtle">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Run {runId.slice(0, 8)}</span>
        <StatusBadge tone={runStatusTone(run.status)}>{run.status}</StatusBadge>
        <span className="text-xs text-muted-foreground">{member_count} member rows</span>
      </div>

      {/* A partial run is NOT a complete membership replacement. */}
      {run.status === "partial" ? (
        <p className="mt-2 text-xs text-amber-300">
          This run is PARTIAL: it is not a complete membership replacement, and a downstream
          universe keeps the last complete revision until a complete run supersedes it.
        </p>
      ) : null}

      {run.failure_reason ? (
        <p className="mt-2 text-xs text-rose-300">Failure reason: {run.failure_reason}</p>
      ) : null}

      {/* Coverage and data freshness are what make "complete" verifiable rather
          than asserted. */}
      {coverage ? (
        <p className="mt-2 text-xs text-muted-foreground">Coverage — {coverage}</p>
      ) : null}
      {freshness ? (
        <p className="mt-1 text-xs text-muted-foreground">Data freshness — {freshness}</p>
      ) : null}

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Rank</TableHead>
            <TableHead>Instrument</TableHead>
            <TableHead>Score</TableHead>
            <TableHead>Values</TableHead>
            <TableHead>Passed</TableHead>
            <TableHead>Exclusion reason</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {members.map((member) => (
            <TableRow key={member.instrument_key}>
              <TableCell className="font-mono text-sm">{member.rank ?? "—"}</TableCell>
              <TableCell className="font-mono text-sm">{member.instrument_key}</TableCell>
              <TableCell className="text-sm">
                {member.score === null ? "—" : member.score.toFixed(4)}
              </TableCell>
              <TableCell className="max-w-[22rem] truncate text-xs text-muted-foreground">
                {summarizeValues(member.values)}
              </TableCell>
              <TableCell>
                <Badge variant={member.passed ? "secondary" : "outline"}>
                  {member.passed ? "passed" : "excluded"}
                </Badge>
              </TableCell>
              {/* Carried through so the operator sees WHY, not just absence. */}
              <TableCell className="text-xs text-muted-foreground">
                {member.exclusion_reason ?? "—"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Panel>
  );
}

function AttachmentBaselines({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const attachmentsQuery = useAlertsScreenerAttachments(workflowId, scope);

  if (attachmentsQuery.isLoading) return <Skeleton className="h-32 w-full rounded-xl" />;
  if (attachmentsQuery.error || !attachmentsQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Attachment baselines unavailable</AlertTitle>
        <AlertDescription>
          {alertsErrorMessage(attachmentsQuery.error, "No data.")}
        </AlertDescription>
      </Alert>
    );
  }

  const { attachments, note, revision } = attachmentsQuery.data;

  if (attachments.length === 0) {
    return <p className="text-sm text-muted-foreground">This screener has no attachments.</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-muted-foreground">
        Revision r{revision}. {note}
      </p>
      {attachments.map((attachment) => (
        <Panel key={attachment.attachment_id} tone="subtle">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm">{attachment.attachment_id}</span>
            <Badge variant="outline">{attachment.trigger ?? "—"}</Badge>
            <span className="text-xs text-muted-foreground">
              channels: {attachment.channels.join(", ") || "none"}
            </span>
          </div>

          <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            {attachment.hysteresis.entry_rank !== null ? (
              <li>entry rank ≤ {attachment.hysteresis.entry_rank}</li>
            ) : null}
            {attachment.hysteresis.exit_rank !== null ? (
              <li>exit rank &gt; {attachment.hysteresis.exit_rank}</li>
            ) : null}
            {attachment.hysteresis.exit_after !== null ? (
              <li>exit after {attachment.hysteresis.exit_after} absent runs</li>
            ) : null}
            {attachment.hysteresis.top_n !== null ? <li>top {attachment.hysteresis.top_n}</li> : null}
            {attachment.hysteresis.rank_delta !== null ? (
              <li>rank delta {attachment.hysteresis.rank_delta}</li>
            ) : null}
            <li>initial match: {attachment.hysteresis.initial_match ? "yes" : "silent baseline"}</li>
          </ul>

          {attachment.members.length === 0 ? (
            <p className="mt-3 text-sm text-muted-foreground">
              No baseline rows yet — this attachment has never seen a complete run and will only
              initialize on the next one.
            </p>
          ) : (
            <Table className="mt-3">
              <TableHeader>
                <TableRow>
                  <TableHead>Instrument</TableHead>
                  <TableHead>Present</TableHead>
                  <TableHead>Last rank</TableHead>
                  <TableHead>Consecutive absent</TableHead>
                  <TableHead>Baseline from run</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {attachment.members.map((member) => (
                  <TableRow key={member.instrument_key}>
                    <TableCell className="font-mono text-sm">{member.instrument_key}</TableCell>
                    <TableCell>
                      <Badge variant={member.present ? "secondary" : "outline"}>
                        {member.present ? "in" : "out"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm">{member.last_rank ?? "—"}</TableCell>
                    <TableCell className="text-sm">{member.consecutive_absent}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {member.last_complete_run_id ? member.last_complete_run_id.slice(0, 8) : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Panel>
      ))}
    </div>
  );
}

export function ScreenerPage({
  workflowId,
  scope,
}: Readonly<{ workflowId: string; scope: string | null }>) {
  const workflowQuery = useAlertsWorkflow(workflowId, scope);
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const runsQuery = useAlertsScreenerRuns(workflowId, scope);
  const { trigger } = useAlertsScreenerMutations(workflowId, scope);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [tab, setTab] = useState<"runs" | "baselines">("runs");

  if (workflowQuery.isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  if (workflowQuery.error || !workflowQuery.data) {
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

  const workflow = workflowQuery.data;
  const runs = runsQuery.data?.runs ?? [];
  const runsNote = runsQuery.data?.note;

  return (
    <div className="flex flex-col gap-6 pb-8">
      <div>
        <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
          <Link href="/alerts">
            <ArrowLeftIcon className="size-4" aria-hidden />
            All alerts
          </Link>
        </Button>
        <SectionLabel eyebrow="Screener" title={workflow.name} />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge variant="secondary">screener</Badge>
          <span className="text-xs text-muted-foreground">
            {workflow.instrument_summary ?? "no coverage"}
          </span>
          <Button asChild size="xs" variant="outline">
            <Link href={`/alerts/${workflowId}`}>Open definition</Link>
          </Button>
          <Button asChild size="xs" variant="outline">
            <Link href={`/alerts/screeners/${workflowId}/edit`}>Edit screener</Link>
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          disabled={trigger.isPending}
          onClick={() =>
            // An idempotency key makes a double-click or a retry return the
            // original run instead of executing a second scan.
            trigger.mutate(crypto.randomUUID())
          }
        >
          <PlayIcon className="size-4" aria-hidden />
          {trigger.isPending ? "Starting…" : "Run now"}
        </Button>
        {trigger.error ? (
          <span className="text-xs text-rose-300">
            {alertsErrorMessage(trigger.error, "Manual run failed")}
          </span>
        ) : null}
        {/* A repeated idempotency key returns the existing run instead of
            executing a second scan; say so rather than looking like nothing
            happened. */}
        {trigger.data?.status === "already_finalized" ? (
          <span className="text-xs text-amber-300">
            This run already exists for that request; no second scan was started.
          </span>
        ) : null}
        {trigger.data?.status && trigger.data.status !== "already_finalized" ? (
          <span className="text-xs text-muted-foreground">
            Run {trigger.data.run_id ? trigger.data.run_id.slice(0, 8) : ""} accepted (
            {trigger.data.status}).
          </span>
        ) : null}
      </div>

      <div role="tablist" aria-label="Screener sections" className="flex flex-wrap gap-2">
        {([
          ["runs", "Runs"],
          ["baselines", "Attachment baselines"],
        ] as const).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            type="button"
            aria-selected={tab === value}
            onClick={() => setTab(value)}
            className={
              tab === value
                ? "rounded-full border border-primary/60 bg-primary/10 px-3 py-1 text-xs text-primary"
                : "rounded-full border border-border/60 px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
            }
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "runs" ? (
        <div className="flex flex-col gap-4">
          {runsQuery.isLoading ? (
            <Skeleton className="h-40 w-full rounded-xl" />
          ) : runsQuery.error ? (
            // A failed run fetch is not "no runs". Show the failure.
            <Alert variant="destructive">
              <AlertCircleIcon />
              <AlertTitle>Could not load runs</AlertTitle>
              <AlertDescription>
                {alertsErrorMessage(runsQuery.error, "The run history request failed.")}
              </AlertDescription>
            </Alert>
          ) : runs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No runs recorded yet.</p>
          ) : (
            <div className="rounded-xl border border-border/70 bg-card/60">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Status</TableHead>
                    <TableHead>Created</TableHead>
                    <TableHead>Completed</TableHead>
                    <TableHead>Triggered by</TableHead>
                    <TableHead>Failure reason</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((run) => {
                    const id = run.run_id;
                    return (
                      <TableRow key={id}>
                        <TableCell>
                          <StatusBadge tone={runStatusTone(run.status)}>{run.status}</StatusBadge>
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {formatTimestamp(run.created_at ?? null) ?? "—"}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {run.status === "running"
                            ? "running…"
                            : formatTimestamp(run.completed_at ?? null) ?? "—"}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {run.triggered_by ?? "—"}
                        </TableCell>
                        <TableCell className="text-xs text-rose-300">
                          {/* A failed run must say WHY rather than just failing. */}
                          {run.failure_reason ?? "—"}
                        </TableCell>
                        <TableCell>
                          <Button
                            size="xs"
                            variant="outline"
                            aria-expanded={selectedRun === id}
                            onClick={() => setSelectedRun(selectedRun === id ? null : id)}
                          >
                            {selectedRun === id ? "Hide" : "Members"}
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}

          {/* The runs-level caveat the server sends, shown on the list rather
              than only inside an expanded member panel. */}
          {runsNote ? <p className="text-xs text-muted-foreground">{runsNote}</p> : null}

          {selectedRun ? <RunMembers runId={selectedRun} scope={scope} /> : null}

          {/* Read from capabilities, not hard-coded: ties are broken by the
              server's ranker, so the UI must report what it actually does. */}
          <p className="text-xs text-muted-foreground">
            Tie-break: {capabilitiesQuery.data?.capabilities.screener.tie_break ?? "…"} — equal
            scores still have a stable order.
          </p>
        </div>
      ) : (
        <AttachmentBaselines workflowId={workflowId} scope={scope} />
      )}
    </div>
  );
}
