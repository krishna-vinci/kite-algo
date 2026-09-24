"use client";

/**
 * Durable execution requests: what the strategy asked for, who authorised it,
 * and what the platform and the broker actually did.
 *
 * The wording is load-bearing. A request status describes the REQUEST ("queued"
 * is not "running"), and the executor's own outcome word describes the ORDER
 * ("dispatched" is not "filled"). An unresolved dispatch is shown as unresolved
 * and is never described as retried.
 */

import { useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  useExecutionRequestDecision,
  useExecutionRequests,
  useHostedPlan,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import {
  executionRequestStatusLabel,
  executionRequestStatusTone,
  formatTimestamp,
  hostedErrorMessage,
  outcomeStateLabel,
  withRefusalCopy,
} from "@/features/strategies/lib/format";
import { authorizationModeLabel } from "@/features/strategies/lib/modes";
import { previewPlanAdmission } from "@/lib/hosted-strategies/api";
import type { ExecutionRequestRow, PlanDetail } from "@/lib/hosted-strategies/types";

/** Fields that explain the request; technical identities stay out of the copy. */
const DETAIL_KEYS = [
  "outcome_state",
  "submitted_quantity",
  "filled_quantity",
  "remaining_quantity",
  "coverage",
  "reason",
  "detail",
] as const;

function PlanLegs({ plan }: Readonly<{ plan: PlanDetail }>) {
  const legs = Array.isArray((plan.resolved_plan as { legs?: unknown }).legs)
    ? ((plan.resolved_plan as { legs: Array<Record<string, unknown>> }).legs ?? [])
    : [];
  const invalidation = plan.invalidation_state as { valid?: boolean; reason_code?: string };
  return (
    <div className="flex flex-col gap-2 text-xs">
      <p className="text-muted-foreground">
        {plan.plan_kind}
        {invalidation.valid === false
          ? ` · this plan no longer holds${invalidation.reason_code ? ` (${invalidation.reason_code})` : ""}`
          : " · plan still holds against the current catalogue"}
      </p>
      {legs.length === 0 ? (
        <p className="text-muted-foreground">No legs are recorded on this plan.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {legs.map((leg, index) => (
            <li key={index} className="flex flex-wrap items-center gap-2">
              <span className="font-mono">
                {String(leg.tradingsymbol ?? leg.instrument_id ?? "leg")}
              </span>
              <span>
                {String(leg.signed_quantity ?? "—")} × {String(leg.product ?? "—")}
              </span>
              {leg.reference_price !== undefined && leg.reference_price !== null ? (
                <span className="text-muted-foreground">@ {String(leg.reference_price)}</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RequestRow({
  strategyId,
  request,
  onDecision,
  deciding,
}: Readonly<{
  strategyId: string;
  request: ExecutionRequestRow;
  onDecision: (request: ExecutionRequestRow, decision: "approve" | "reject") => void;
  deciding: string | null;
}>) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const planQuery = useHostedPlan(strategyId, open ? request.plan_id : null);
  const details = DETAIL_KEYS.filter((key) => key in (request.execution_detail ?? {})).map((key) => ({
    key,
    value: request.execution_detail[key],
  }));
  const awaiting = request.status === "awaiting_approval";
  const busy = deciding === request.request_id;

  return (
    <>
      <TableRow>
        <TableCell className="align-top text-xs text-muted-foreground">
          {formatTimestamp(request.created_at)}
        </TableCell>
        <TableCell className="align-top text-sm">
          <span
            className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${executionRequestStatusTone(
              request.status,
            )}`}
            data-testid={`request-status-${request.request_id}`}
          >
            {executionRequestStatusLabel(request.status)}
          </span>
          {request.outcome_state ? (
            <span className="mt-1 block text-xs text-muted-foreground">
              {outcomeStateLabel(request.outcome_state)}
            </span>
          ) : null}
          {request.refusal_code ? (
            <span className="mt-1 block text-xs text-destructive" data-testid="request-refusal">
              {withRefusalCopy(request.refusal_code)}
            </span>
          ) : null}
        </TableCell>
        <TableCell className="align-top text-xs text-muted-foreground">
          {authorizationModeLabel(request.authorization_mode)}
          {request.decision_kind ? (
            <span className="mt-1 block">
              {request.decision_kind === "automatic"
                ? "decided by your standing authorization"
                : request.decision_kind === "manual"
                  ? "decided by you"
                  : request.decision_kind}
            </span>
          ) : null}
        </TableCell>
        <TableCell className="align-top text-sm">
          {awaiting ? (
            <div className="flex flex-col gap-2">
              <Textarea
                aria-label="Decision note"
                rows={2}
                className="min-w-40 text-xs"
                placeholder="Note (optional)"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={() => onDecision(request, "approve")}
                  disabled={busy}
                >
                  {busy ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
                  Approve this plan
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onDecision(request, "reject")}
                  disabled={busy}
                >
                  Reject
                </Button>
              </div>
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">
              {request.terminal ? "No further dispatch" : "Still in the platform's queue"}
            </span>
          )}
        </TableCell>
        <TableCell className="align-top text-right">
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? (
              <ChevronDownIcon className="size-4" aria-hidden />
            ) : (
              <ChevronRightIcon className="size-4" aria-hidden />
            )}
            Plan
          </Button>
        </TableCell>
      </TableRow>
      {open ? (
        <TableRow>
          <TableCell colSpan={5} className="bg-muted/20">
            <div className="flex flex-col gap-3 py-2">
              {planQuery.isLoading ? (
                <Skeleton className="h-16 w-full rounded-md" />
              ) : planQuery.isError ? (
                <p className="text-xs text-destructive">{hostedErrorMessage(planQuery.error)}</p>
              ) : planQuery.data ? (
                <PlanLegs plan={planQuery.data} />
              ) : null}
              <PlanAdmission planId={request.plan_id} strategyId={strategyId} />
              {request.reservation_id ? (
                <p className="text-xs text-muted-foreground">
                  Capacity reserved: {request.reservation_id}
                </p>
              ) : null}
              {request.approval_id ? (
                <p className="text-xs text-muted-foreground">
                  Approval recorded: {request.approval_id}
                  {request.decision_kind === "automatic"
                    ? " (automatic, from your authorization)"
                    : ""}
                </p>
              ) : null}
              {details.length > 0 ? (
                <dl className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                  {details.map((entry) => (
                    <div key={entry.key}>
                      <dt className="font-medium text-foreground">{entry.key.replace(/_/g, " ")}</dt>
                      <dd>{typeof entry.value === "string" ? entry.value : JSON.stringify(entry.value)}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              <p className="text-xs text-muted-foreground">
                This request was made against version {request.version_number ?? "—"} in{" "}
                {request.execution_environment}.
              </p>
            </div>
          </TableCell>
        </TableRow>
      ) : null}
    </>
  );
}

function PlanAdmission({
  strategyId,
  planId,
}: Readonly<{ strategyId: string; planId: string }>) {
  const [verdict, setVerdict] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function check() {
    setBusy(true);
    try {
      const result = await previewPlanAdmission(strategyId, planId);
      setVerdict(
        result.admitted
          ? "Admission would allow this plan (a preview does not reserve capacity)."
          : `Admission would refuse this plan: ${withRefusalCopy(result.rejection_reason ?? "ADMISSION_REFUSED")}`,
      );
    } catch (error) {
      setVerdict(hostedErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" onClick={check} disabled={busy}>
          {busy ? <Loader2Icon className="size-3 animate-spin" aria-hidden /> : null}
          Check admission and margin
        </Button>
        {verdict ? <span className="text-xs">{verdict}</span> : null}
      </div>
      <p className="text-xs text-muted-foreground">
        A preview writes nothing: capacity is only claimed when the plan actually dispatches.
      </p>
    </div>
  );
}

export function HostedExecutionRequestsPanel({ strategyId }: Readonly<{ strategyId: string }>) {
  const requestsQuery = useExecutionRequests(strategyId);
  const decision = useExecutionRequestDecision(strategyId);
  const [deciding, setDeciding] = useState<string | null>(null);
  const requests = requestsQuery.data?.requests ?? [];
  const awaiting = requests.filter((row) => row.status === "awaiting_approval").length;

  async function onDecision(request: ExecutionRequestRow, decisionKind: "approve" | "reject") {
    setDeciding(request.request_id);
    try {
      await decision.mutateAsync({ requestId: request.request_id, decision: decisionKind });
      toast.success(
        decisionKind === "approve"
          ? "Approved. The platform queues exactly one dispatch for this plan."
          : "Rejected. Nothing will be sent for this plan.",
      );
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    } finally {
      setDeciding(null);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Alert>
        <AlertTitle>Reading these states</AlertTitle>
        <AlertDescription>
          <span className="block" data-testid="request-state-explainer">
            Queued means the platform has not started this request; a running process is a different
            thing entirely. Dispatched means the platform sent the work, not that the broker accepted it
            or that anything filled. An unresolved dispatch stays unresolved and is not retried on its
            own.
          </span>
          {awaiting > 0 ? (
            <span className="mt-2 block font-medium text-foreground">
              {awaiting === 1
                ? "One plan is waiting for your decision."
                : `${awaiting} plans are waiting for your decision.`}
            </span>
          ) : null}
        </AlertDescription>
      </Alert>
      {requestsQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-md" />
      ) : requestsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load execution requests</AlertTitle>
          <AlertDescription>{hostedErrorMessage(requestsQuery.error)}</AlertDescription>
        </Alert>
      ) : requests.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No execution requests yet. A strategy asks for execution with{" "}
          <code>request_execution</code> after it proposes a trade.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Created</TableHead>
              <TableHead>State</TableHead>
              <TableHead>Authorization</TableHead>
              <TableHead>Your decision</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {requests.map((request) => (
              <RequestRow
                key={request.request_id}
                strategyId={strategyId}
                request={request}
                onDecision={onDecision}
                deciding={deciding}
              />
            ))}
          </TableBody>
        </Table>
      )}
      <div className="flex items-center gap-2">
        <Badge variant="secondary">{requests.length}</Badge>
        <Button
          size="sm"
          variant="outline"
          onClick={() => void requestsQuery.refetch()}
          disabled={requestsQuery.isFetching}
        >
          Refresh
        </Button>
      </div>
    </div>
  );
}
