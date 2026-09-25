"use client";

/**
 * Owner-facing "Options" operations view (B2.6a): the list of option runs for
 * this strategy, a per-run detail (edges, frozen policies, refusals, greeks,
 * P&L), a governed repair panel for stranded runs, and the four separated
 * controls.
 *
 * `coverage: "unknown"` on the list means the list is NOT complete. It is
 * never rendered as "no structures" — a short or empty list under unknown
 * coverage says so explicitly instead of going quiet.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { ChevronDownIcon, ChevronRightIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
  useHostedJobs,
  useOptionRun,
  useOptionRunRepairAssessment,
  useOptionRuns,
  useStopHostedJob,
  useSubmitOptionRunRepair,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import {
  formatTimestamp,
  hostedErrorMessage,
  optionLegStateLabel,
  optionLegStateTone,
  optionRunStatusLabel,
  optionRunStatusTone,
  withRefusalCopy,
} from "@/features/strategies/lib/format";
import type {
  HostedJobSummary,
  OptionRun,
  OptionRunLeg,
  OptionRunRepairActionPayload,
} from "@/lib/hosted-strategies/types";

// ---------------------------------------------------------------------------
// Legs table
// ---------------------------------------------------------------------------

function OptionLegsTable({ legs }: Readonly<{ legs: OptionRunLeg[] }>) {
  if (legs.length === 0) {
    return <p className="text-sm text-muted-foreground">No legs recorded on this run.</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Tradingsymbol</TableHead>
          <TableHead>Side</TableHead>
          <TableHead>Role</TableHead>
          <TableHead>Ratio</TableHead>
          <TableHead>Target qty</TableHead>
          <TableHead>Own open qty</TableHead>
          <TableHead>State</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {legs.map((leg) => (
          <TableRow key={leg.leg_id}>
            <TableCell className="font-mono text-xs">{leg.tradingsymbol}</TableCell>
            <TableCell className="text-sm">{leg.side}</TableCell>
            <TableCell className="text-sm text-muted-foreground">{leg.role ?? "—"}</TableCell>
            <TableCell className="text-sm">{leg.ratio}</TableCell>
            <TableCell className="text-sm">{leg.quantity}</TableCell>
            <TableCell className="text-sm">
              {leg.own_open_quantity === null ? (
                <span className="text-amber-600 dark:text-amber-400">unreadable</span>
              ) : (
                leg.own_open_quantity
              )}
            </TableCell>
            <TableCell className="text-sm">
              <span className={optionLegStateTone(leg.state)}>{optionLegStateLabel(leg.state)}</span>
              {leg.state === "pending" || leg.state === "failed" ? (
                <span aria-hidden className="ml-1">
                  {leg.state === "pending" ? "⏳" : "⚠"}
                </span>
              ) : null}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Run detail (edges, frozen policies, refusals, greeks, P&L)
// ---------------------------------------------------------------------------

function OptionRunDetailSection({
  strategyId,
  optionRunId,
}: Readonly<{ strategyId: string; optionRunId: string }>) {
  const detailQuery = useOptionRun(strategyId, optionRunId);

  if (detailQuery.isLoading) {
    return <Skeleton className="h-24 w-full rounded-md" />;
  }
  if (detailQuery.isError || !detailQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load run detail</AlertTitle>
        <AlertDescription>{hostedErrorMessage(detailQuery.error)}</AlertDescription>
      </Alert>
    );
  }

  const detail = detailQuery.data;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h4 className="text-sm font-medium">Edges timeline</h4>
        {detail.edges.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">No edges recorded yet.</p>
        ) : (
          <ul className="mt-1 flex flex-col gap-1 text-xs">
            {detail.edges.map((edge, index) => (
              <li key={`${edge.plan_id}-${index}`} className="flex flex-wrap items-center gap-2">
                <Badge variant="outline">{edge.phase}</Badge>
                <span className="font-mono text-muted-foreground">{edge.plan_id}</span>
                <span className="text-muted-foreground">{formatTimestamp(edge.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h4 className="text-sm font-medium">Frozen policies</h4>
        <dl className="mt-1 grid gap-2 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-muted-foreground">Protection policy</dt>
            <dd>{detail.frozen.protection_policy ? JSON.stringify(detail.frozen.protection_policy) : "not declared"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Max loss</dt>
            <dd>{detail.frozen.max_loss ? JSON.stringify(detail.frozen.max_loss) : "not declared"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Expiry policy</dt>
            <dd>{detail.frozen.expiry_policy ?? "not declared"}</dd>
          </div>
        </dl>
      </div>
      <div>
        <h4 className="text-sm font-medium">Refusals</h4>
        {detail.refusals.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">No refusals recorded for this run.</p>
        ) : (
          <ul className="mt-1 flex flex-col gap-1 text-xs">
            {detail.refusals.map((refusal) => (
              <li key={refusal.request_id}>
                <span className="font-mono">{refusal.refusal_code}</span> —{" "}
                {withRefusalCopy(refusal.refusal_code)}
                <span className="ml-1 text-muted-foreground">({formatTimestamp(refusal.at)})</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <h4 className="text-sm font-medium">Greeks</h4>
          {detail.greeks.available ? (
            <p className="mt-1 text-xs">
              delta {detail.greeks.delta} · gamma {detail.greeks.gamma} · theta {detail.greeks.theta} · vega{" "}
              {detail.greeks.vega}
            </p>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              not available{detail.greeks.reason ? ` (${detail.greeks.reason})` : ""}
            </p>
          )}
        </div>
        <div>
          <h4 className="text-sm font-medium">P&amp;L</h4>
          {detail.pnl.available ? (
            <p className="mt-1 text-xs">
              premium {detail.pnl.premium} · mtm {detail.pnl.mtm}
            </p>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              not available{detail.pnl.reason ? ` (${detail.pnl.reason})` : ""}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Repair panel
// ---------------------------------------------------------------------------

function OptionRunRepairPanel({
  strategyId,
  optionRunId,
}: Readonly<{ strategyId: string; optionRunId: string }>) {
  const assessmentQuery = useOptionRunRepairAssessment(strategyId, optionRunId);
  const mutation = useSubmitOptionRunRepair(strategyId, optionRunId);
  const [confirmAction, setConfirmAction] = useState<OptionRunRepairActionPayload["action"] | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const assessment = assessmentQuery.data;

  async function confirm() {
    if (!assessment || !confirmAction) return;
    setActionError(null);
    try {
      await mutation.mutateAsync({ action: confirmAction, evidence_digest: assessment.evidence_digest });
      toast.success(
        confirmAction === "close_flat" ? "Run closed (flat)." : "Residual close submitted.",
      );
      setConfirmAction(null);
    } catch (error) {
      setActionError(hostedErrorMessage(error));
    } finally {
      void assessmentQuery.refetch();
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border p-3">
      <h4 className="text-sm font-medium">Repair</h4>
      {assessmentQuery.isLoading ? (
        <Skeleton className="h-16 w-full rounded-md" />
      ) : assessmentQuery.isError || !assessment ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load the repair assessment</AlertTitle>
          <AlertDescription>{hostedErrorMessage(assessmentQuery.error)}</AlertDescription>
        </Alert>
      ) : (
        <>
          <p className="text-xs text-muted-foreground" data-testid={`option-repair-state-${optionRunId}`}>
            State: <span className="font-medium text-foreground">{assessment.state}</span>
            {assessment.reason_code ? ` — ${withRefusalCopy(assessment.reason_code)}` : ""}
          </p>
          {assessment.reasons.length > 0 ? (
            <ul className="list-disc pl-5 text-xs text-muted-foreground">
              {assessment.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
          {assessment.state === "residual" && assessment.close_plan.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Tradingsymbol</TableHead>
                  <TableHead>Transaction</TableHead>
                  <TableHead>Qty</TableHead>
                  <TableHead>Product</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {assessment.close_plan.map((leg, index) => (
                  <TableRow key={`${leg.tradingsymbol}-${index}`}>
                    <TableCell className="font-mono text-xs">{leg.tradingsymbol}</TableCell>
                    <TableCell className="text-sm">{leg.transaction_type}</TableCell>
                    <TableCell className="text-sm">{leg.quantity}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{leg.product ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            {assessment.state === "flat" ? (
              <Button
                size="sm"
                onClick={() => setConfirmAction("close_flat")}
                disabled={mutation.isPending}
              >
                Close (flat)
              </Button>
            ) : null}
            {assessment.state === "residual" ? (
              <Button
                size="sm"
                onClick={() => setConfirmAction("close_residual")}
                disabled={mutation.isPending}
              >
                Close residual
              </Button>
            ) : null}
            {assessment.state === "ambiguous" || assessment.state === "not_repairable" ? (
              <p className="text-xs text-muted-foreground">
                No repair action is available while this run&apos;s state is {assessment.state}.
              </p>
            ) : null}
          </div>
          {actionError ? (
            <p className="text-xs text-destructive" data-testid="option-repair-error">
              {actionError}
            </p>
          ) : null}
        </>
      )}
      <Dialog
        open={confirmAction !== null}
        onOpenChange={(openState) => {
          if (!openState) setConfirmAction(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {confirmAction === "close_flat" ? "Close this run as flat?" : "Submit the residual close?"}
            </DialogTitle>
            <DialogDescription>
              {confirmAction === "close_flat"
                ? "This run's own confirmed fills show nothing open. Confirming records the run as closed; no orders are sent."
                : "This sends exactly the closing orders below through the platform's governed exit."}
            </DialogDescription>
          </DialogHeader>
          {confirmAction === "close_residual" && assessment ? (
            <ul className="flex flex-col gap-1 text-xs">
              {assessment.close_plan.map((leg, index) => (
                <li key={`${leg.tradingsymbol}-${index}`} className="font-mono">
                  {leg.transaction_type} {leg.quantity} × {leg.tradingsymbol}
                </li>
              ))}
            </ul>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmAction(null)}>
              Cancel
            </Button>
            <Button onClick={confirm} disabled={mutation.isPending}>
              {mutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
              Confirm
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// One run card
// ---------------------------------------------------------------------------

function OptionRunCard({
  strategyId,
  run,
}: Readonly<{ strategyId: string; run: OptionRun }>) {
  const [open, setOpen] = useState(false);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="font-mono text-sm">{run.option_run_id}</CardTitle>
            <span
              className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${optionRunStatusTone(run.status)}`}
              data-testid={`option-run-status-${run.option_run_id}`}
            >
              {optionRunStatusLabel(run.status)}
            </span>
            <Badge variant="outline">gen {run.structure_generation}</Badge>
          </div>
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={open}
            aria-controls={`option-run-detail-${run.option_run_id}`}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? (
              <ChevronDownIcon className="size-4" aria-hidden />
            ) : (
              <ChevronRightIcon className="size-4" aria-hidden />
            )}
            Details
          </Button>
        </div>
        <CardDescription>
          {run.underlying} · {run.expiry} · {run.product}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {run.protective_exit_unresolved ? (
          <Alert variant="destructive" data-testid={`option-run-protective-warning-${run.option_run_id}`}>
            <AlertTitle>Protective exit unresolved</AlertTitle>
            <AlertDescription>
              This run&apos;s protective exit has not resolved. Treat this run&apos;s risk as unmanaged
              until it does.
            </AlertDescription>
          </Alert>
        ) : null}
        {run.coverage === "unknown" ? (
          <p className="text-xs text-amber-600 dark:text-amber-400">
            This run&apos;s own leg coverage could not be fully verified — the legs below may be
            incomplete.
          </p>
        ) : null}
        <p className="text-xs text-muted-foreground">protection owner: not yet available</p>
        <OptionLegsTable legs={run.legs} />
        {open ? (
          <div id={`option-run-detail-${run.option_run_id}`} className="flex flex-col gap-4 border-t pt-4">
            <OptionRunDetailSection strategyId={strategyId} optionRunId={run.option_run_id} />
            {run.repairable ? (
              <OptionRunRepairPanel strategyId={strategyId} optionRunId={run.option_run_id} />
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Coverage warning
// ---------------------------------------------------------------------------

function OptionRunsCoverageWarning({ coverageReason }: Readonly<{ coverageReason: string }>) {
  return (
    <Alert variant="destructive" data-testid="option-runs-coverage-warning">
      <AlertTitle>This list may be incomplete</AlertTitle>
      <AlertDescription>
        <span>
          The platform could not prove it read every option run for this strategy
          {coverageReason ? ` (${coverageReason})` : ""}. An empty or short list here is NOT proof that
          no structures are open — retry, or check the server, before assuming there is nothing left.
        </span>
      </AlertDescription>
    </Alert>
  );
}

// ---------------------------------------------------------------------------
// Four controls
// ---------------------------------------------------------------------------

function ControlCard({
  title,
  description,
  children,
}: Readonly<{ title: string; description: string; children: ReactNode }>) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border p-3">
      <h4 className="text-sm font-medium">{title}</h4>
      <p className="text-xs text-muted-foreground">{description}</p>
      {children}
    </div>
  );
}

export function OptionsControls({
  strategyId,
  jobs,
}: Readonly<{ strategyId: string; jobs: HostedJobSummary[] }>) {
  // The only owner-facing route among the four that already exists is the
  // per-attempt job stop (`POST /{strategy_id}/jobs/{job_id}/stop`); its own
  // note is explicit that it "does not cancel orders or flatten" — exactly
  // the "stop evaluator" semantics asked for here. Cancel-pending, exit
  // structure and flatten have no owner-facing route yet, so those three stay
  // disabled rather than invent one.
  const activeJob = jobs.find((job) =>
    ["queued", "starting", "running", "fencing"].includes(job.status),
  );
  const stopMutation = useStopHostedJob(strategyId, activeJob?.job_id ?? "");
  const [confirmStop, setConfirmStop] = useState(false);

  async function confirmStopEvaluator() {
    if (!activeJob) return;
    try {
      await stopMutation.mutateAsync({ attempt: activeJob.attempt });
      toast.success(
        "Stop requested. No new evaluations will start; holdings and protection stay as they are.",
      );
      setConfirmStop(false);
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <ControlCard title="Stop evaluator" description="No new evaluations; holdings and protection stay.">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setConfirmStop(true)}
          disabled={!activeJob || stopMutation.isPending}
          data-testid="option-control-stop-evaluator"
        >
          Stop evaluator
        </Button>
        {!activeJob ? <p className="text-xs text-muted-foreground">No active attempt to stop.</p> : null}
      </ControlCard>
      <ControlCard
        title="Cancel pending work"
        description="Cancels pending entries only; never protective or exit orders."
      >
        <Button size="sm" variant="outline" disabled data-testid="option-control-cancel-pending">
          Cancel pending work
        </Button>
        <p className="text-xs text-muted-foreground">Not available yet.</p>
      </ControlCard>
      <ControlCard title="Exit structure" description="Governed exit of one option run.">
        <Button size="sm" variant="outline" disabled data-testid="option-control-exit-structure">
          Exit structure
        </Button>
        <p className="text-xs text-muted-foreground">Not available yet.</p>
      </ControlCard>
      <ControlCard title="Flatten" description="Closes all of this strategy's exposure.">
        <Button size="sm" variant="destructive" disabled data-testid="option-control-flatten">
          Flatten
        </Button>
        <p className="text-xs text-muted-foreground">Not available yet.</p>
      </ControlCard>
      <Dialog open={confirmStop} onOpenChange={setConfirmStop}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop the evaluator?</DialogTitle>
            <DialogDescription>
              This stops attempt #{activeJob?.attempt} from starting new evaluations. It does not cancel
              orders, exit any structure, or touch protection — those stay exactly as they are.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmStop(false)}>
              Cancel
            </Button>
            <Button onClick={confirmStopEvaluator} disabled={stopMutation.isPending}>
              {stopMutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
              Confirm stop
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top-level panel
// ---------------------------------------------------------------------------

export function HostedOptionsPanel({ strategyId }: Readonly<{ strategyId: string }>) {
  const runsQuery = useOptionRuns(strategyId);
  const jobsQuery = useHostedJobs(strategyId);
  const runs = runsQuery.data?.runs ?? [];
  const jobs = jobsQuery.data?.jobs ?? [];
  const coverage = runsQuery.data?.coverage;

  return (
    <div className="flex flex-col gap-6">
      <OptionsControls strategyId={strategyId} jobs={jobs} />
      {runsQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-md" />
      ) : runsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load option runs</AlertTitle>
          <AlertDescription>{hostedErrorMessage(runsQuery.error)}</AlertDescription>
        </Alert>
      ) : (
        <>
          {coverage === "unknown" ? (
            <OptionRunsCoverageWarning coverageReason={runsQuery.data?.coverage_reason ?? ""} />
          ) : null}
          {runs.length === 0 && coverage !== "unknown" ? (
            <p className="text-sm text-muted-foreground">No option runs for this strategy.</p>
          ) : null}
          {runs.length > 0 ? (
            <div className="flex flex-col gap-4">
              {runs.map((run) => (
                <OptionRunCard key={run.option_run_id} strategyId={strategyId} run={run} />
              ))}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
