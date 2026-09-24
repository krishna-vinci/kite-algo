"use client";

import Link from "next/link";
import { useState } from "react";
import { PlusIcon, ServerCogIcon } from "lucide-react";
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
  useHostedOptions,
  useHostedStrategies,
  useUpdateHostedStrategy,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import { hostedErrorMessage } from "@/features/strategies/lib/format";
import {
  LIVE_MODE,
  executionModeLabel,
  isModeSupported,
  liveLaneSummary,
  preferredCreateMode,
} from "@/features/strategies/lib/modes";
import type { HostedStrategy, HostedStrategyOptions } from "@/lib/hosted-strategies/types";

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
  const strategies = strategiesQuery.data?.strategies ?? [];

  return (
    <div className="flex flex-col gap-6">
      <SectionLabel
        eyebrow="Hosted strategies"
        title="Strategies"
        description="Paste or drop a Python file on one page, choose what it may do and where it runs, then supervise the attempts."
      />

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
