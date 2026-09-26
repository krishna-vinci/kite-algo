"use client";

import Link from "next/link";
import { useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, PlusIcon, ServerCogIcon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { SectionLabel } from "@/components/operator/section-label";
import {
  useCreateHostedStrategy,
  useExecutionRequests,
  useHostedJobs,
  useHostedOptions,
  useHostedStrategies,
  useOptionRuns,
  usePendingApprovals,
  useUpdateHostedStrategy,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import { hostedErrorMessage, jobStatusLabel } from "@/features/strategies/lib/format";
import {
  LIVE_MODE,
  executionModeLabel,
  isModeSupported,
  liveLaneSummary,
  preferredCreateMode,
} from "@/features/strategies/lib/modes";
import { plainStrategyState, PLAIN_STATE_LABELS, type PlainStateKind } from "@/features/strategies/lib/plain-state";
import type { HostedStrategy, HostedStrategyOptions } from "@/lib/hosted-strategies/types";

/** Tailwind classes for the five plain-state badges. */
function plainStateTone(state: PlainStateKind): string {
  switch (state) {
    case "running":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400";
    case "stopped":
      return "border-border bg-muted/40 text-muted-foreground";
    case "waiting_for_you":
      return "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400";
    case "needs_attention":
      return "border-orange-500/40 bg-orange-500/10 text-orange-600 dark:text-orange-400";
    case "error":
      return "border-destructive/40 bg-destructive/10 text-destructive";
    default:
      return "border-border bg-muted/40 text-muted-foreground";
  }
}

/**
 * The plain-state badge + sentence for one strategy, with the raw job/request/
 * option-run codes available behind a "details" toggle. Per-row queries here
 * are the same ones the detail page already uses; they are cheap and cached.
 */
function PlainStrategyStatus({ strategy }: Readonly<{ strategy: HostedStrategy }>) {
  const jobsQuery = useHostedJobs(strategy.strategy_id);
  const requestsQuery = useExecutionRequests(strategy.strategy_id);
  const optionRunsQuery = useOptionRuns(strategy.strategy_id);
  const [showDetails, setShowDetails] = useState(false);

  const jobs = jobsQuery.data?.jobs ?? [];
  const latestJob = jobs[0];
  const requests = requestsQuery.data?.requests ?? [];
  const optionRuns = optionRunsQuery.data?.runs ?? [];

  const plain = plainStrategyState({
    strategyId: strategy.strategy_id,
    job: latestJob ? { status: latestJob.status, job_id: latestJob.job_id } : null,
    executionRequests: requests.map((row) => ({ status: row.status })),
    optionRuns: optionRuns.map((row) => ({ status: row.status, repairable: row.repairable })),
  });

  return (
    <div className="flex flex-col gap-1" data-testid={`plain-state-${strategy.strategy_id}`}>
      <div className="flex items-center gap-2">
        <span
          className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${plainStateTone(plain.state)}`}
        >
          {PLAIN_STATE_LABELS[plain.state]}
        </span>
        {plain.action ? (
          <Link href={plain.action.href ?? "#"} className="text-xs underline underline-offset-2">
            {plain.action.label}
          </Link>
        ) : null}
      </div>
      <p className="text-xs text-muted-foreground">{plain.sentence}</p>
      <button
        type="button"
        className="inline-flex items-center gap-1 self-start text-xs text-muted-foreground underline underline-offset-2"
        onClick={() => setShowDetails((value) => !value)}
      >
        {showDetails ? <ChevronDownIcon className="size-3" aria-hidden /> : <ChevronRightIcon className="size-3" aria-hidden />}
        Details
      </button>
      {showDetails ? (
        <dl className="grid gap-0.5 text-[11px] text-muted-foreground">
          <div>
            <dt className="inline font-medium">Latest job:</dt>{" "}
            <dd className="inline">{latestJob ? jobStatusLabel(latestJob.status) : "none yet"}</dd>
          </div>
          <div>
            <dt className="inline font-medium">Execution requests:</dt>{" "}
            <dd className="inline">
              {requests.length === 0 ? "none" : requests.map((row) => row.status).join(", ")}
            </dd>
          </div>
          <div>
            <dt className="inline font-medium">Option runs:</dt>{" "}
            <dd className="inline">
              {optionRuns.length === 0 ? "none" : optionRuns.map((row) => row.status).join(", ")}
            </dd>
          </div>
        </dl>
      ) : null}
    </div>
  );
}

function StatusBadge({ status }: Readonly<{ status: string }>) {
  return (
    <Badge variant={status === "active" ? "default" : "secondary"}>
      {status === "active" ? "Enabled" : "Disabled"}
    </Badge>
  );
}

function CreateStrategyForm({ options }: Readonly<{ options: HostedStrategyOptions }>) {
  const createMutation = useCreateHostedStrategy();
  const modes = options.execution_modes;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [accountScope, setAccountScope] = useState(options.account_scopes[0] ?? "");
  // A first-time default is `paper` whenever the deployment offers it — never
  // the live mode.
  const [mode, setMode] = useState(() => preferredCreateMode(modes));
  const [jobKind, setJobKind] = useState(options.job_kinds[0] ?? "finite");
  const [policy, setPolicy] = useState(options.stale_exit_policies[0] ?? "none");
  const [maxDuration, setMaxDuration] = useState("21600");
  const [deadline, setDeadline] = useState("600");
  const lanes = liveLaneSummary(options);
  const modeOffered = isModeSupported(options, mode);

  async function submit() {
    if (!name.trim()) {
      toast.error("A name is required.");
      return;
    }
    if (!accountScope) {
      toast.error("No authorized account scope is available.");
      return;
    }
    try {
      await createMutation.mutateAsync({
        name: name.trim(),
        description: description.trim() || null,
        execution_mode: mode,
        job_kind: jobKind,
        account_scope: accountScope,
        max_duration_s: Number(maxDuration) || 21600,
        progress_deadline_s: Number(deadline) || 600,
        stale_exit_policy: policy,
      });
      toast.success("Strategy created.");
      setName("");
      setDescription("");
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New hosted strategy</CardTitle>
        <CardDescription>
          Account scopes come from the server authorization allowlist — only scopes you may use are
          listed, and the execution modes are the ones this deployment actually offers. Live mode
          also needs your explicit approval of the plan; nothing is ever approved automatically.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-2">
        <div className="grid gap-1.5">
          <Label htmlFor="hs-name">Name</Label>
          <Input id="hs-name" value={name} onChange={(event) => setName(event.target.value)} />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="hs-scope">Account scope</Label>
          <Select value={accountScope} onValueChange={setAccountScope}>
            <SelectTrigger id="hs-scope" className="w-full">
              <SelectValue placeholder="Select an authorized scope" />
            </SelectTrigger>
            <SelectContent>
              {options.account_scopes.map((scope) => (
                <SelectItem key={scope} value={scope}>
                  {scope}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="hs-mode">Execution mode</Label>
          <Select value={mode} onValueChange={setMode}>
            <SelectTrigger id="hs-mode" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {modes.map((value) => (
                <SelectItem key={value} value={value}>
                  {executionModeLabel(value)}
                </SelectItem>
              ))}
              {!modeOffered ? (
                // The deployment stopped offering this mode after the form was
                // opened. Keeping the stored value visible is deliberate: the
                // selection is never silently rewritten to another mode.
                <SelectItem value={mode}>{executionModeLabel(mode)} (not offered here)</SelectItem>
              ) : null}
            </SelectContent>
          </Select>
          {!modeOffered ? (
            <p className="text-xs text-destructive">
              This deployment does not currently offer {executionModeLabel(mode)}. The selection is
              unchanged; a launch in this mode would be refused by the server.
            </p>
          ) : null}
          {mode === LIVE_MODE && modeOffered ? (
            <p className="text-xs text-muted-foreground">
              Live runs place real orders and need your explicit approval of the plan before anything
              is sent.
              {lanes ? ` Supported lanes: ${lanes}.` : ""}
            </p>
          ) : null}
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="hs-kind">Job kind</Label>
          <Select value={jobKind} onValueChange={setJobKind}>
            <SelectTrigger id="hs-kind" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {options.job_kinds.map((value) => (
                <SelectItem key={value} value={value}>
                  {value}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="hs-policy">Stale-exit policy</Label>
          <Select value={policy} onValueChange={setPolicy}>
            <SelectTrigger id="hs-policy" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {options.stale_exit_policies.map((value) => (
                <SelectItem key={value} value={value}>
                  {value}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="hs-max">Max duration (s)</Label>
            <Input
              id="hs-max"
              inputMode="numeric"
              value={maxDuration}
              onChange={(event) => setMaxDuration(event.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="hs-deadline">Progress deadline (s)</Label>
            <Input
              id="hs-deadline"
              inputMode="numeric"
              value={deadline}
              onChange={(event) => setDeadline(event.target.value)}
            />
          </div>
        </div>
        <div className="grid gap-1.5 md:col-span-2">
          <Label htmlFor="hs-description">Description</Label>
          <Textarea
            id="hs-description"
            rows={2}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>
        <div className="md:col-span-2">
          <Button onClick={submit} disabled={createMutation.isPending}>
            <PlusIcon className="size-4" aria-hidden />
            Create strategy
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function StrategyRow({
  strategy,
  options,
}: Readonly<{ strategy: HostedStrategy; options: HostedStrategyOptions | undefined }>) {
  const updateMutation = useUpdateHostedStrategy(strategy.strategy_id);
  const enabled = strategy.status === "active";

  async function toggle() {
    try {
      await updateMutation.mutateAsync({ status: enabled ? "disabled" : "active" });
      toast.success(enabled ? "Strategy disabled." : "Strategy enabled.");
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  return (
    <TableRow>
      <TableCell>
        <Link href={`/strategies/${strategy.strategy_id}`} className="font-medium hover:underline">
          {strategy.name}
        </Link>
        <p className="text-xs text-muted-foreground">{strategy.strategy_id}</p>
      </TableCell>
      <TableCell>
        <StatusBadge status={strategy.status} />
      </TableCell>
      <TableCell className="min-w-48">
        <PlainStrategyStatus strategy={strategy} />
      </TableCell>
      <TableCell
        className="text-sm text-muted-foreground"
        data-testid={`strategy-mode-${strategy.strategy_id}`}
      >
        <span>{executionModeLabel(strategy.default_execution_mode)}</span>
        {options && !isModeSupported(options, strategy.default_execution_mode) ? (
          <span className="ml-1 text-xs">(not offered here)</span>
        ) : null}
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">{strategy.default_account_scope}</TableCell>
      <TableCell className="text-sm text-muted-foreground">{strategy.stale_exit_policy}</TableCell>
      <TableCell className="text-right">
        <Button size="sm" variant="outline" onClick={toggle} disabled={updateMutation.isPending}>
          {enabled ? "Disable" : "Enable"}
        </Button>
      </TableCell>
    </TableRow>
  );
}

export function HostedStrategiesListPage() {
  const optionsQuery = useHostedOptions();
  const strategiesQuery = useHostedStrategies();
  const pendingApprovalsQuery = usePendingApprovals();
  const strategies = strategiesQuery.data?.strategies ?? [];
  const pendingApprovalsCount = pendingApprovalsQuery.data?.count ?? 0;

  return (
    <div className="flex flex-col gap-6">
      <SectionLabel
        eyebrow="Hosted strategies"
        title="Strategies"
        description="Paste or drop a Python file on one page, choose what it may do and where it runs, then supervise the attempts."
      />

      {pendingApprovalsCount > 0 ? (
        <Alert data-testid="pending-approvals-banner">
          <AlertTitle>
            {pendingApprovalsCount === 1
              ? "One execution request is waiting for your approval"
              : `${pendingApprovalsCount} execution requests are waiting for your approval`}
          </AlertTitle>
          <AlertDescription>
            <Link href="/strategies/approvals" className="font-medium underline underline-offset-2">
              Review approvals
            </Link>
          </AlertDescription>
        </Alert>
      ) : null}

      <div>
        <Button asChild>
          <Link href="/strategies/new">
            <PlusIcon className="size-4" aria-hidden />
            New strategy
          </Link>
        </Button>
      </div>

      {optionsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load options</AlertTitle>
          <AlertDescription>{hostedErrorMessage(optionsQuery.error)}</AlertDescription>
        </Alert>
      ) : null}

      {optionsQuery.data ? <CreateStrategyForm options={optionsQuery.data} /> : null}

      <Card>
        <CardHeader>
          <CardTitle>Registered strategies</CardTitle>
          <CardDescription>
            Disabling stops new attempts; it does not stop or reconcile a running attempt. A
            strategy pinned to a mode this deployment does not offer stays listed, and its launches
            stay refused.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {strategiesQuery.isLoading ? (
            <div className="flex flex-col gap-2">
              {Array.from({ length: 3 }).map((_, index) => (
                <Skeleton key={index} className="h-12 w-full rounded-lg" />
              ))}
            </div>
          ) : strategiesQuery.isError ? (
            <Alert variant="destructive">
              <AlertTitle>Could not load strategies</AlertTitle>
              <AlertDescription>{hostedErrorMessage(strategiesQuery.error)}</AlertDescription>
            </Alert>
          ) : strategies.length === 0 ? (
            <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border/70 px-6 py-10 text-center">
              <ServerCogIcon className="size-4 text-muted-foreground/50" aria-hidden />
              <p className="text-sm font-medium text-muted-foreground">No hosted strategies yet</p>
              <p className="max-w-md text-xs text-muted-foreground/70">
                Create one above to register its first source version.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Strategy</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Mode</TableHead>
                  <TableHead>Account scope</TableHead>
                  <TableHead>Stale exit</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {strategies.map((strategy) => (
                  <StrategyRow
                    key={strategy.strategy_id}
                    strategy={strategy}
                    options={optionsQuery.data}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
