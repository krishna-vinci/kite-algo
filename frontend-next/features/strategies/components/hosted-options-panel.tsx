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

import { useEffect, useState } from "react";
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
import { Input } from "@/components/ui/input";
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
  useCancelPendingWork,
  useDeadSubmission,
  useFlattenStatus,
  useHostedJobs,
  useHostedStrategy,
  useOptionExitAssessment,
  useOptionRun,
  useOptionRunRepairAssessment,
  useOptionRuns,
  usePendingWork,
  useResolveDeadSubmission,
  useStopHostedJob,
  useSubmitFlatten,
  useSubmitOptionExit,
  useSubmitOptionRunRepair,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import {
  deadSubmissionDispositionLabel,
  flattenDoneConditionLabel,
  flattenItemKindLabel,
  flattenItemLabel,
  flattenItemReasonCode,
  flattenItemStateLabel,
  flattenItemStateTone,
  flattenStatusLabel,
  formatTimestamp,
  hostedErrorMessage,
  hostedRefusalCode,
  hostedRefusalDeadSubmissionSteps,
  hostedRefusalProtectiveStages,
  optionLegStateLabel,
  optionLegStateTone,
  optionRunStatusLabel,
  optionRunStatusTone,
  withRefusalCopy,
} from "@/features/strategies/lib/format";
import type {
  DeadSubmissionDisposition,
  DeadSubmissionEvidence,
  FlattenDeadSubmissionStepRef,
  FlattenDoneConditions,
  FlattenManifestItem,
  FlattenOperationResult,
  FlattenStopView,
  HostedJobSummary,
  OptionExitAssessment,
  OptionRun,
  OptionRunLeg,
  OptionRunRepairActionPayload,
  PendingWorkItem,
  ProtectionOwner,
} from "@/lib/hosted-strategies/types";

// ---------------------------------------------------------------------------
// Protection owner (design §5: option runs carry `protection_owner`)
// ---------------------------------------------------------------------------

/**
 * `null` is a neutral fact — no protective stage currently owns this run.
 * `{ state: "unknown" }` is NOT neutral: the platform could not read who (if
 * anyone) owns this run's protection, so it is shown as a warning, because new
 * exposure is blocked while ownership is unreadable.
 */
function ProtectionOwnerLine({ owner, optionRunId }: Readonly<{ owner: ProtectionOwner; optionRunId: string }>) {
  if (owner === null) {
    return (
      <p className="text-xs text-muted-foreground" data-testid={`protection-owner-none-${optionRunId}`}>
        No protection owner.
      </p>
    );
  }
  if (!("owner_run_id" in owner)) {
    return (
      <Alert variant="destructive" data-testid={`protection-owner-unknown-${optionRunId}`}>
        <AlertTitle>Protection ownership unreadable</AlertTitle>
        <AlertDescription>
          The platform could not read who owns this run&apos;s protection. New exposure is blocked until it
          can.
        </AlertDescription>
      </Alert>
    );
  }
  return (
    <p className="text-xs text-muted-foreground" data-testid={`protection-owner-info-${optionRunId}`}>
      Protection owner: <span className="font-mono">{owner.owner_run_id.slice(0, 8)}…</span> · epoch{" "}
      {owner.owner_epoch} · {owner.action_state} · policy{" "}
      <span className="font-mono">{owner.policy_version.slice(0, 8)}…</span>
    </p>
  );
}

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
// Dead-submission disposition (B2.6b §4)
// ---------------------------------------------------------------------------

function DeadSubmissionEvidencePanel({ evidence }: Readonly<{ evidence: DeadSubmissionEvidence }>) {
  return (
    <dl className="grid gap-2 text-xs sm:grid-cols-2">
      <div>
        <dt className="text-muted-foreground">Trail state</dt>
        <dd>{evidence.trail_state}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground">Source</dt>
        <dd>{evidence.source}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground">Platform status</dt>
        <dd>{evidence.status}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground">Filled quantity</dt>
        <dd>{evidence.filled_quantity ?? "—"}</dd>
      </div>
      <div>
        <dt className="text-muted-foreground">Remaining quantity</dt>
        <dd>{evidence.remaining_quantity ?? "—"}</dd>
      </div>
    </dl>
  );
}

/**
 * Resolve one unanswered plan-step submission. The owner never types an
 * outcome: only the `allowed_dispositions` the server names for THIS step's
 * own evidence are offered, and the POST is pinned to that evidence's digest.
 * Resolving it does not send an order; it lets takeover/repair proceed.
 */
export function ResolveDeadSubmissionDialog({
  strategyId,
  planId,
  stepNo,
  open,
  onOpenChange,
}: Readonly<{
  strategyId: string;
  planId: string;
  stepNo: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}>) {
  const evidenceQuery = useDeadSubmission(strategyId, planId, stepNo, open);
  const mutation = useResolveDeadSubmission(strategyId, planId, stepNo);
  const [disposition, setDisposition] = useState<DeadSubmissionDisposition | null>(null);
  const [reason, setReason] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  const evidence = evidenceQuery.data;

  function reset() {
    setDisposition(null);
    setReason("");
    setActionError(null);
  }

  async function confirm() {
    if (!evidence || !disposition) return;
    setActionError(null);
    try {
      await mutation.mutateAsync({
        evidence_digest: evidence.evidence_digest,
        disposition,
        reason: reason.trim(),
      });
      toast.success("Disposition recorded. Takeover/repair can proceed once this settles.");
      onOpenChange(false);
      reset();
    } catch (error) {
      setActionError(hostedErrorMessage(error));
    } finally {
      void evidenceQuery.refetch();
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(openState) => {
        onOpenChange(openState);
        if (!openState) reset();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Resolve unanswered step</DialogTitle>
          <DialogDescription>
            This records what the platform can already prove about one unanswered submission. It does not
            send any order. You can only choose from the platform&apos;s own allowed outcomes below.
          </DialogDescription>
        </DialogHeader>
        {evidenceQuery.isLoading ? (
          <Skeleton className="h-24 w-full rounded-md" />
        ) : evidenceQuery.isError || !evidence ? (
          <Alert variant="destructive">
            <AlertTitle>Could not load this step&apos;s evidence</AlertTitle>
            <AlertDescription>{hostedErrorMessage(evidenceQuery.error)}</AlertDescription>
          </Alert>
        ) : (
          <>
            <DeadSubmissionEvidencePanel evidence={evidence} />
            {evidence.allowed_dispositions.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                The platform does not offer any disposition for this step yet.
              </p>
            ) : (
              <div className="flex flex-col gap-2">
                <p className="text-xs font-medium">Choose the outcome the platform allows:</p>
                <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Allowed disposition">
                  {evidence.allowed_dispositions.map((option) => (
                    <Button
                      key={option}
                      type="button"
                      size="sm"
                      variant={disposition === option ? "default" : "outline"}
                      aria-pressed={disposition === option}
                      onClick={() => setDisposition(option)}
                      data-testid={`dead-submission-disposition-${option}`}
                    >
                      {deadSubmissionDispositionLabel(option)}
                    </Button>
                  ))}
                </div>
              </div>
            )}
            <Input
              aria-label="Reason"
              placeholder="Reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </>
        )}
        {actionError ? (
          <p className="text-xs text-destructive" data-testid="dead-submission-error">
            {actionError}
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button
            onClick={confirm}
            disabled={!evidence || !disposition || !reason.trim() || mutation.isPending}
            data-testid="dead-submission-confirm"
          >
            {mutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
            Confirm disposition
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
  const [resolveStep, setResolveStep] = useState<{ planId: string; stepNo: number } | null>(null);

  const assessment = assessmentQuery.data;
  const unresolvedSteps = assessment?.unresolved_steps ?? [];

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
          {unresolvedSteps.length > 0 ? (
            <div
              className="flex flex-col gap-2 border-t pt-3"
              data-testid={`option-resolve-dead-submission-${optionRunId}`}
            >
              <h5 className="text-xs font-medium">Unanswered step{unresolvedSteps.length > 1 ? "s" : ""}</h5>
              <p className="text-xs text-muted-foreground">
                This run is ambiguous because a step of its adjust may still be submitting. If the platform
                can prove a step below is actually dead — never reached the broker, or already terminal —
                resolving it lets takeover/repair proceed. It does not send any order, and it cannot be typed
                in: only the platform&apos;s own allowed outcomes are offered.
              </p>
              <div className="flex flex-col gap-2">
                {unresolvedSteps.map((step) => (
                  <div
                    key={`${step.plan_id}-${step.step_no}`}
                    className="flex flex-wrap items-center gap-2 text-xs"
                  >
                    <span className="font-mono">
                      {step.plan_id} · step {step.step_no}
                    </span>
                    <span className="text-muted-foreground">{step.state || "unreadable"}</span>
                    <span className="font-mono text-muted-foreground">{step.order_id ?? "—"}</span>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setResolveStep({ planId: step.plan_id, stepNo: step.step_no })}
                      data-testid={`option-resolve-dead-submission-trigger-${step.plan_id}-${step.step_no}`}
                    >
                      Resolve unanswered step
                    </Button>
                  </div>
                ))}
              </div>
              {resolveStep ? (
                <ResolveDeadSubmissionDialog
                  strategyId={strategyId}
                  planId={resolveStep.planId}
                  stepNo={resolveStep.stepNo}
                  open
                  onOpenChange={(openState) => {
                    if (!openState) setResolveStep(null);
                  }}
                />
              ) : null}
            </div>
          ) : null}
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
// Exit structure (B2.6b S2 — single-run governed exit, design §2)
// ---------------------------------------------------------------------------

/** True while a new owner-exit submission would be refused before it is sent. */
function exitBlocked(assessment: OptionExitAssessment): boolean {
  return (
    assessment.adjust_owner_state !== "finished" ||
    assessment.protective_stage_state !== "resolved" ||
    assessment.state === "ambiguous"
  );
}

function OptionExitClosePlanTable({ legs }: Readonly<{ legs: OptionExitAssessment["close_plan"] }>) {
  if (legs.length === 0) {
    return <p className="text-xs text-muted-foreground">This run&apos;s own fills show nothing left to close.</p>;
  }
  return (
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
        {legs.map((leg, index) => (
          <TableRow key={`${leg.tradingsymbol}-${index}`}>
            <TableCell className="font-mono text-xs">{leg.tradingsymbol}</TableCell>
            <TableCell className="text-sm">{leg.transaction_type}</TableCell>
            <TableCell className="text-sm">{leg.quantity}</TableCell>
            <TableCell className="text-sm text-muted-foreground">{leg.product ?? "—"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * Governed exit of this run's own confirmed fills (design §2). The engine
 * enforces short-first closure and proof-before-hedge-release; a POST here is
 * exactly one accepted stage, never the whole exit. `accepted` keeps the run
 * open, `complete` means it is exited, and `blocked` (either a named refusal
 * in the response or a 409) is shown as actionable text, not silence.
 */
function OptionRunExitPanel({
  strategyId,
  optionRunId,
}: Readonly<{ strategyId: string; optionRunId: string }>) {
  const [open, setOpen] = useState(false);
  const assessmentQuery = useOptionExitAssessment(strategyId, optionRunId, open);
  const mutation = useSubmitOptionExit(strategyId, optionRunId);
  const [actionError, setActionError] = useState<string | null>(null);
  const [stage, setStage] = useState<"complete" | "accepted" | null>(null);

  const assessment = assessmentQuery.data;
  const blocked = assessment ? exitBlocked(assessment) : true;
  const canContinue = stage === "accepted" && Boolean(assessment) && !blocked && (assessment?.close_plan.length ?? 0) > 0;

  function reset() {
    setActionError(null);
    setStage(null);
  }

  async function runExit(evidenceDigest: string) {
    setActionError(null);
    try {
      const result = await mutation.mutateAsync({ evidence_digest: evidenceDigest, reason: "owner_exit" });
      if (result.status === "complete") {
        setStage("complete");
        toast.success("Run exited.");
      } else if (result.status === "accepted") {
        setStage("accepted");
        toast.success("Stage submitted. This run stays open until every stage completes.");
      } else {
        setStage(null);
        setActionError(result.refusal ? withRefusalCopy(result.refusal) : "The platform blocked this exit.");
      }
    } catch (error) {
      setStage(null);
      setActionError(hostedErrorMessage(error));
    } finally {
      void assessmentQuery.refetch();
    }
  }

  async function confirmExit() {
    if (!assessment) return;
    await runExit(assessment.evidence_digest);
  }

  async function continueExit() {
    const fresh = await assessmentQuery.refetch();
    const data = fresh.data;
    if (!data) return;
    if (exitBlocked(data)) return;
    await runExit(data.evidence_digest);
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border p-3">
      <h4 className="text-sm font-medium">Exit structure</h4>
      <p className="text-xs text-muted-foreground">
        Governed exit of this run&apos;s own confirmed fills. Shorts close first; hedges are released only
        after every short in this run is proven closed — a run may need more than one stage to fully exit.
      </p>
      <Button
        size="sm"
        variant="outline"
        onClick={() => setOpen(true)}
        data-testid={`option-exit-structure-trigger-${optionRunId}`}
      >
        Exit structure
      </Button>
      <Dialog
        open={open}
        onOpenChange={(openState) => {
          setOpen(openState);
          if (!openState) reset();
        }}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Exit this option run?</DialogTitle>
            <DialogDescription>
              This sends the platform&apos;s own governed exit for this run&apos;s confirmed fills — never a
              freshly built order list. Hedges stay in place until every short is proven closed.
            </DialogDescription>
          </DialogHeader>
          {assessmentQuery.isLoading ? (
            <Skeleton className="h-24 w-full rounded-md" />
          ) : assessmentQuery.isError || !assessment ? (
            <Alert variant="destructive">
              <AlertTitle>Could not load the exit assessment</AlertTitle>
              <AlertDescription>{hostedErrorMessage(assessmentQuery.error)}</AlertDescription>
            </Alert>
          ) : (
            <>
              {assessment.protective_stage_state !== "resolved" ? (
                <Alert variant="destructive" data-testid="option-exit-structure-protective-unresolved">
                  <AlertTitle>Protective exit unresolved</AlertTitle>
                  <AlertDescription>{withRefusalCopy("OPTION_PROTECTIVE_EXIT_UNRESOLVED")}</AlertDescription>
                </Alert>
              ) : null}
              {assessment.adjust_owner_state !== "finished" ? (
                <Alert variant="destructive" data-testid="option-exit-structure-adjust-in-flight">
                  <AlertTitle>Adjustment in flight</AlertTitle>
                  <AlertDescription>{withRefusalCopy("OPTION_RUN_ADJUST_IN_FLIGHT")}</AlertDescription>
                </Alert>
              ) : null}
              {assessment.state === "ambiguous" ? (
                <Alert variant="destructive" data-testid="option-exit-structure-ambiguous">
                  <AlertTitle>Evidence ambiguous</AlertTitle>
                  <AlertDescription>{withRefusalCopy("OPTION_RUN_EVIDENCE_AMBIGUOUS")}</AlertDescription>
                </Alert>
              ) : null}
              {assessment.reason_code ? (
                <p className="text-xs text-muted-foreground">{withRefusalCopy(assessment.reason_code)}</p>
              ) : null}
              {assessment.reasons && assessment.reasons.length > 0 ? (
                <ul className="list-disc pl-5 text-xs text-muted-foreground">
                  {assessment.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              ) : null}
              <div>
                <h5 className="text-xs font-medium">This stage&apos;s close plan (shorts first)</h5>
                <OptionExitClosePlanTable legs={assessment.close_plan} />
              </div>
              <p className="text-xs text-muted-foreground">
                This is a multi-stage exit: hedges in this run are released only once every short is proven
                closed by confirmed fills. Confirming submits exactly this stage — not the whole exit.
              </p>
              {stage === "accepted" ? (
                <Alert data-testid="option-exit-structure-stage-message">
                  <AlertTitle>Stage submitted</AlertTitle>
                  <AlertDescription>
                    This run stays open until every stage completes.
                    {canContinue
                      ? " The next stage is now releasable."
                      : " Waiting for the next stage to become releasable."}
                  </AlertDescription>
                </Alert>
              ) : null}
              {stage === "complete" ? (
                <Alert data-testid="option-exit-structure-complete-message">
                  <AlertTitle>Run exited</AlertTitle>
                  <AlertDescription>This run&apos;s own fills are flat and it is now exited.</AlertDescription>
                </Alert>
              ) : null}
            </>
          )}
          {actionError ? (
            <p className="text-xs text-destructive" data-testid={`option-exit-structure-error-${optionRunId}`}>
              {actionError}
            </p>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
            {stage === "accepted" ? (
              <Button onClick={continueExit} disabled={!canContinue || mutation.isPending} data-testid="option-exit-structure-continue">
                {mutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
                Continue exit
              </Button>
            ) : (
              <Button
                onClick={confirmExit}
                disabled={!assessment || blocked || mutation.isPending || stage === "complete"}
                data-testid="option-exit-structure-confirm"
              >
                {mutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
                Confirm exit
              </Button>
            )}
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
        <ProtectionOwnerLine owner={run.protection_owner} optionRunId={run.option_run_id} />
        <OptionLegsTable legs={run.legs} />
        {open ? (
          <div id={`option-run-detail-${run.option_run_id}`} className="flex flex-col gap-4 border-t pt-4">
            <OptionRunDetailSection strategyId={strategyId} optionRunId={run.option_run_id} />
            <OptionRunExitPanel strategyId={strategyId} optionRunId={run.option_run_id} />
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
// Cancel pending work (B2.6b §1)
// ---------------------------------------------------------------------------

function PendingWorkPreviewTable({ items }: Readonly<{ items: PendingWorkItem[] }>) {
  const eligible = items.filter((item) => item.eligibility === "eligible");
  const ineligible = items.filter((item) => item.eligibility !== "eligible");
  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-muted-foreground">
        Protective (hedge) orders and exit/reduction orders are never cancelled by this action.
      </p>
      <div>
        <h5 className="text-xs font-medium">Eligible to cancel ({eligible.length})</h5>
        {eligible.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">
            No pending entry work currently qualifies for cancellation.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Plan / step</TableHead>
                <TableHead>Order id</TableHead>
                <TableHead>Remaining qty</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {eligible.map((item) => (
                <TableRow key={`${item.plan_id}-${item.step_no}`} data-testid="pending-work-eligible-row">
                  <TableCell className="font-mono text-xs">
                    {item.plan_id} · step {item.step_no}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{item.order_id ?? "—"}</TableCell>
                  <TableCell className="text-sm">{item.remaining_quantity ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      <div>
        <h5 className="text-xs font-medium">Not cancellable ({ineligible.length})</h5>
        {ineligible.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">Every candidate qualifies.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Plan / step</TableHead>
                <TableHead>Order id</TableHead>
                <TableHead>Remaining qty</TableHead>
                <TableHead>Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ineligible.map((item) => (
                <TableRow key={`${item.plan_id}-${item.step_no}`} data-testid="pending-work-ineligible-row">
                  <TableCell className="font-mono text-xs">
                    {item.plan_id} · step {item.step_no}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{item.order_id ?? "—"}</TableCell>
                  <TableCell className="text-sm">{item.remaining_quantity ?? "—"}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {item.reason_code ? withRefusalCopy(item.reason_code) : "Not eligible."}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}

function CancelPendingWorkControl({ strategyId }: Readonly<{ strategyId: string }>) {
  const [open, setOpen] = useState(false);
  const previewQuery = usePendingWork(strategyId, open);
  const mutation = useCancelPendingWork(strategyId);
  const [actionError, setActionError] = useState<string | null>(null);
  const [promptRefresh, setPromptRefresh] = useState(false);

  const preview = previewQuery.data;
  const coverageUnknown = preview?.coverage === "unknown";
  const eligibleCount = preview?.items.filter((item) => item.eligibility === "eligible").length ?? 0;
  const confirmDisabled = mutation.isPending || !preview || coverageUnknown || eligibleCount === 0;

  async function confirmCancel() {
    if (!preview) return;
    setActionError(null);
    setPromptRefresh(false);
    try {
      await mutation.mutateAsync({ evidence_digest: preview.evidence_digest, reason: "owner_cancel" });
      toast.success("Cancellation submitted for the eligible pending work.");
      setOpen(false);
    } catch (error) {
      setActionError(hostedErrorMessage(error));
      setPromptRefresh(hostedRefusalCode(error) === "CANCEL_EVIDENCE_CHANGED");
    } finally {
      void previewQuery.refetch();
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        onClick={() => setOpen(true)}
        data-testid="option-control-cancel-pending"
      >
        Cancel pending work
      </Button>
      <Dialog
        open={open}
        onOpenChange={(openState) => {
          setOpen(openState);
          if (!openState) {
            setActionError(null);
            setPromptRefresh(false);
          }
        }}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Cancel pending work</DialogTitle>
            <DialogDescription>
              Cancels only pending, exposure-increasing entry orders this strategy owns. Protective (hedge)
              orders and exit/reduction orders are never cancelled by this action.
            </DialogDescription>
          </DialogHeader>
          {previewQuery.isLoading ? (
            <Skeleton className="h-24 w-full rounded-md" />
          ) : previewQuery.isError || !preview ? (
            <Alert variant="destructive">
              <AlertTitle>Could not load the preview</AlertTitle>
              <AlertDescription>{hostedErrorMessage(previewQuery.error)}</AlertDescription>
            </Alert>
          ) : (
            <>
              {coverageUnknown ? (
                <Alert variant="destructive" data-testid="pending-work-coverage-warning">
                  <AlertTitle>Coverage unknown</AlertTitle>
                  <AlertDescription>
                    The platform could not prove it read every candidate, so cancellation stays disabled
                    until it can.
                  </AlertDescription>
                </Alert>
              ) : null}
              <PendingWorkPreviewTable items={preview.items} />
            </>
          )}
          {actionError ? (
            <div className="flex flex-col gap-2" data-testid="cancel-pending-error">
              <p className="text-xs text-destructive">{actionError}</p>
              {promptRefresh ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void previewQuery.refetch()}
                  data-testid="cancel-pending-refresh"
                >
                  Refresh preview
                </Button>
              ) : null}
            </div>
          ) : null}
          {!coverageUnknown && preview && eligibleCount === 0 ? (
            <p className="text-xs text-muted-foreground">Nothing eligible to cancel right now.</p>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
            <Button onClick={confirmCancel} disabled={confirmDisabled} data-testid="cancel-pending-confirm">
              {mutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
              Confirm cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

// ---------------------------------------------------------------------------
// Flatten (B2.6b S3, design §3)
// ---------------------------------------------------------------------------

/** What the owner should do next for one blocked manifest item, by its kind. */
function blockedItemGuidance(item: FlattenManifestItem): ReactNode {
  if (item.kind === "option_exit") {
    return (
      <span className="text-foreground">
        Open this run&apos;s <span className="font-medium">Details</span>, then use its own{" "}
        <span className="font-medium">Exit structure</span> (or resolve its unanswered step) below.
      </span>
    );
  }
  if (item.kind === "cancel_pending") {
    return (
      <span className="text-foreground">
        Review this candidate through <span className="font-medium">Cancel pending work</span> above.
      </span>
    );
  }
  if (item.kind === "nonoption_reduction") {
    return <span className="text-foreground">Check the reason above before retrying flatten.</span>;
  }
  return null;
}

function FlattenManifestTable({ items }: Readonly<{ items: FlattenManifestItem[] }>) {
  if (items.length === 0) {
    return <p className="text-xs text-muted-foreground">No manifest items recorded yet.</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Item</TableHead>
          <TableHead>Kind</TableHead>
          <TableHead>State</TableHead>
          <TableHead>Reason</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => {
          const reasonCode = flattenItemReasonCode(item);
          return (
            <TableRow key={item.key} data-testid="flatten-manifest-row">
              <TableCell className="text-sm">
                <div className="flex flex-col">
                  <span>{flattenItemLabel(item)}</span>
                  <span className="font-mono text-xs text-muted-foreground">{item.key}</span>
                </div>
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">{flattenItemKindLabel(item.kind)}</TableCell>
              <TableCell>
                <span
                  className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${flattenItemStateTone(item.state)}`}
                >
                  {flattenItemStateLabel(item.state)}
                </span>
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {item.state === "blocked" ? (
                  <div className="flex flex-col gap-1" data-testid={`flatten-item-blocked-${item.key}`}>
                    <span>{reasonCode ? withRefusalCopy(reasonCode) : "Blocked."}</span>
                    {blockedItemGuidance(item)}
                  </div>
                ) : (
                  "—"
                )}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

/** The evaluator-stop half of a flatten operation (design §3 step 1). */
function FlattenStopSummary({ stop }: Readonly<{ stop: FlattenStopView | null | undefined }>) {
  if (!stop) return null;
  return (
    <p className="text-xs text-muted-foreground" data-testid="flatten-stop-state">
      Evaluator stop:{" "}
      <span className="font-medium text-foreground">{stop.requested ? "requested" : "not requested"}</span> ·
      state {stop.state}
      {stop.jobs.length > 0 ? ` · ${stop.jobs.length} job(s)` : ""}
      {stop.reason ? ` · ${stop.reason}` : ""}
    </p>
  );
}

/** The checklist flatten's own `done` proof requires (design §3, "Done means…"). */
function FlattenDoneConditionsList({
  doneConditions,
  missing,
}: Readonly<{ doneConditions: FlattenDoneConditions | null | undefined; missing: string[] }>) {
  if (!doneConditions) return null;
  const entries = Object.entries(doneConditions);
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-col gap-1" data-testid="flatten-done-conditions">
      <h5 className="text-xs font-medium">Done conditions</h5>
      <ul className="flex flex-col gap-0.5 text-xs">
        {entries.map(([key, met]) => (
          <li key={key} className="flex items-center gap-2">
            <span
              aria-hidden
              className={met ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground"}
            >
              {met ? "✓" : "○"}
            </span>
            <span className={met ? "" : "text-muted-foreground"}>{flattenDoneConditionLabel(key)}</span>
          </li>
        ))}
      </ul>
      {missing.length > 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="flatten-missing">
          Still needed: {missing.map((key) => flattenDoneConditionLabel(key)).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Strategy-scoped flatten orchestration (design §3): stops the evaluator
 * first, cancels only eligible pending entries, exits option structures one
 * at a time (shorts first), and closes other positions with governed
 * reductions. This is never a whole-account liquidation. A fresh flatten
 * needs the owner to type the strategy's own name; resuming an already
 * confirmed, blocked operation does not re-prompt for it.
 */
function FlattenControl({
  strategyId,
  strategyName,
}: Readonly<{ strategyId: string; strategyName: string }>) {
  const [open, setOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [unresolvedSteps, setUnresolvedSteps] = useState<FlattenDeadSubmissionStepRef[]>([]);
  const [protectiveStages, setProtectiveStages] = useState<unknown[]>([]);
  const [result, setResult] = useState<FlattenOperationResult | null>(null);

  const statusQuery = useFlattenStatus(strategyId, open);
  const mutation = useSubmitFlatten(strategyId);

  useEffect(() => {
    if (statusQuery.data) setResult(statusQuery.data);
  }, [statusQuery.data]);

  const items = result?.items ?? [];
  const missing = result?.missing ?? [];
  const nameLoaded = strategyName.trim().length > 0;
  const confirmed = nameLoaded && confirmText.trim() === strategyName.trim();

  function reset() {
    setConfirmText("");
    setActionError(null);
    setUnresolvedSteps([]);
    setProtectiveStages([]);
  }

  async function runFlatten() {
    setActionError(null);
    setUnresolvedSteps([]);
    setProtectiveStages([]);
    try {
      const response = await mutation.mutateAsync({ reason: "owner_flatten", stop_evaluator: true });
      setResult(response);
      toast.success(
        response.status === "complete"
          ? "Flatten complete. This strategy has no qualifying pending entry, open option risk or attributed exposure left."
          : "Flatten started: evaluator stopped, then pending entries, option structures and other positions are worked through in order.",
      );
      setConfirmText("");
    } catch (error) {
      const code = hostedRefusalCode(error);
      if (code === "DEAD_SUBMISSION_UNRESOLVED") {
        setUnresolvedSteps(hostedRefusalDeadSubmissionSteps(error));
        setProtectiveStages(hostedRefusalProtectiveStages(error));
      }
      setActionError(hostedErrorMessage(error));
    } finally {
      void statusQuery.refetch();
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant="destructive"
        onClick={() => setOpen(true)}
        data-testid="option-control-flatten"
      >
        Flatten
      </Button>
      <Dialog
        open={open}
        onOpenChange={(openState) => {
          setOpen(openState);
          if (!openState) reset();
        }}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Flatten this strategy?</DialogTitle>
            <DialogDescription>
              This is a governed sequence, not one instant order, and it is{" "}
              <span className="font-medium text-foreground">not</span> an account-wide liquidation — it acts
              only on this strategy&apos;s own attributed exposure.
            </DialogDescription>
          </DialogHeader>
          <ul className="list-disc pl-5 text-xs text-muted-foreground">
            <li>Stops the evaluator first, so no new plan can race this operation.</li>
            <li>Cancels only pending entry work this strategy can prove it owns — never protective or reduction orders.</li>
            <li>Exits open option structures one at a time, shorts first; hedges release only once proven closed.</li>
            <li>Closes other positions with governed, risk-reducing orders.</li>
          </ul>

          {result && items.length > 0 ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <h5 className="text-xs font-medium">Manifest</h5>
                <span
                  className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${flattenItemStateTone(
                    result.status === "complete" ? "done" : result.status === "blocked" ? "blocked" : "in_progress",
                  )}`}
                  data-testid="flatten-status"
                >
                  {flattenStatusLabel(result.status)}
                </span>
              </div>
              <FlattenManifestTable items={items} />
            </div>
          ) : null}

          {result ? <FlattenStopSummary stop={result.stop} /> : null}
          {result ? <FlattenDoneConditionsList doneConditions={result.done_conditions} missing={missing} /> : null}

          {result && (result.status === "in_progress" || result.status === "accepted") ? (
            <p className="text-xs text-muted-foreground" data-testid="flatten-running-note">
              This operation is still running. The manifest above refreshes automatically until it settles.
            </p>
          ) : null}

          {!result || result.status === "blocked" ? (
            result && result.status === "blocked" ? (
              <p className="text-xs text-muted-foreground">
                This operation is blocked on the item(s) above. Resolve what each one names, then resume —
                completed work stays completed.
              </p>
            ) : (
              <div className="flex flex-col gap-2">
                <p className="text-xs text-muted-foreground">
                  Type this strategy&apos;s name to confirm —{" "}
                  <span className="font-mono text-foreground">
                    {nameLoaded ? strategyName : "loading…"}
                  </span>
                  .
                </p>
                <Input
                  aria-label="Type the strategy name to confirm flatten"
                  placeholder={nameLoaded ? strategyName : ""}
                  value={confirmText}
                  onChange={(event) => setConfirmText(event.target.value)}
                  disabled={!nameLoaded}
                  data-testid="flatten-confirm-name-input"
                />
              </div>
            )
          ) : null}

          {unresolvedSteps.length > 0 || protectiveStages.length > 0 ? (
            <div
              className="flex flex-col gap-2 rounded-md border border-destructive/40 p-2"
              data-testid="flatten-unresolved-steps"
            >
              <p className="text-xs font-medium text-destructive">
                {withRefusalCopy("DEAD_SUBMISSION_UNRESOLVED")}
              </p>
              {unresolvedSteps.length > 0 ? (
                <div>
                  <p className="text-xs font-medium">Steps to resolve:</p>
                  <ul className="list-disc pl-5 text-xs">
                    {unresolvedSteps.map((step) => (
                      <li key={`${step.plan_id}-${step.step_no}`} className="font-mono">
                        {step.plan_id} · step {step.step_no}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {protectiveStages.length > 0 ? (
                <div data-testid="flatten-protective-stages">
                  <p className="text-xs font-medium">Protective stages still open:</p>
                  <ul className="list-disc pl-5 text-xs">
                    {protectiveStages.map((stage, index) => (
                      <li key={index} className="font-mono break-all">
                        {typeof stage === "string" ? stage : JSON.stringify(stage)}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}

          {actionError ? (
            <p className="text-xs text-destructive" data-testid="flatten-error">
              {actionError}
            </p>
          ) : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
            {result && result.status === "blocked" ? (
              <Button onClick={runFlatten} disabled={mutation.isPending} data-testid="flatten-resume">
                {mutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
                Resume flatten
              </Button>
            ) : result && result.status === "complete" ? (
              <Button variant="outline" onClick={() => setResult(null)} data-testid="flatten-restart">
                Start a new flatten
              </Button>
            ) : !result ? (
              <Button
                variant="destructive"
                onClick={runFlatten}
                disabled={!confirmed || mutation.isPending}
                data-testid="flatten-confirm"
              >
                {mutation.isPending ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
                Confirm flatten
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
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
  strategyName,
  jobs,
}: Readonly<{ strategyId: string; strategyName: string; jobs: HostedJobSummary[] }>) {
  // Stop evaluator uses the existing per-attempt job stop
  // (`POST /{strategy_id}/jobs/{job_id}/stop`); its own note is explicit that
  // it "does not cancel orders or flatten" — exactly the "stop evaluator"
  // semantics asked for here. Cancel pending work (B2.6b §1) is wired below.
  // Exit structure (B2.6b S2) is wired per option run — see
  // `OptionRunExitPanel` on each run's expanded card — because it acts on
  // exactly one run's own fills at a time. This top-level card stays purely
  // informational rather than acting on a run it cannot itself pick. Flatten
  // (B2.6b S3) is wired below through `FlattenControl`.
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
        <CancelPendingWorkControl strategyId={strategyId} />
      </ControlCard>
      <ControlCard
        title="Exit structure"
        description="Exits one option run's structure at a time — never every run at once."
      >
        <p className="text-xs text-muted-foreground" data-testid="option-control-exit-structure">
          Open a run&apos;s <span className="font-medium text-foreground">Details</span> below, then use its
          own <span className="font-medium text-foreground">Exit structure</span> control. Each run exits
          separately, so hedges are never released across runs.
        </p>
      </ControlCard>
      <ControlCard
        title="Flatten"
        description="Stops the evaluator, then closes this strategy's own exposure — never a whole-account liquidation."
      >
        <FlattenControl strategyId={strategyId} strategyName={strategyName} />
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
  const strategyQuery = useHostedStrategy(strategyId);
  const runs = runsQuery.data?.runs ?? [];
  const jobs = jobsQuery.data?.jobs ?? [];
  const coverage = runsQuery.data?.coverage;

  return (
    <div className="flex flex-col gap-6">
      <OptionsControls
        strategyId={strategyId}
        strategyName={strategyQuery.data?.name ?? ""}
        jobs={jobs}
      />
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
