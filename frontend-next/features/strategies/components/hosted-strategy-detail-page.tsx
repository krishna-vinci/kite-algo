"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, PlayIcon, ShieldPlusIcon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
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
  useCreateHostedVersion,
  useExecutionRequests,
  useHostedJobs,
  useHostedOptions,
  useHostedStrategy,
  useHostedVersions,
  useOptionRuns,
  useRunHostedStrategy,
  useUpdateHostedStrategy,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import { HostedAuthorizationPanel } from "@/features/strategies/components/hosted-authorization-panel";
import { HostedExposurePanel } from "@/features/strategies/components/hosted-exposure-panel";
import { HostedExecutionRequestsPanel } from "@/features/strategies/components/hosted-execution-requests-panel";
import { HostedOptionsPanel } from "@/features/strategies/components/hosted-options-panel";
import {
  HostedParamInputs,
  useHostedParamValues,
} from "@/features/strategies/components/hosted-params-editor";
import { HostedSchedulePanel } from "@/features/strategies/components/hosted-schedule-panel";
import {
  formatTimestamp,
  hostedErrorMessage,
  jobStatusLabel,
  jobStatusTone,
  newIdempotencyKey,
  runNowMessage,
} from "@/features/strategies/lib/format";
import {
  APPROVAL_BASED,
  authorizationModeLabel,
  LIVE_MODE,
  executionModeLabel,
  liveLaneSummary,
  modeCapabilityState,
  runNowGate,
  supportedExecutionModes,
} from "@/features/strategies/lib/modes";
import { plainStrategyState, PLAIN_STATE_LABELS, type PlainStateKind } from "@/features/strategies/lib/plain-state";
import type { HostedJobSummary, HostedVersion } from "@/lib/hosted-strategies/types";

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
 * The one-sentence, one-action banner at the top of the detail page. Raw
 * job/request/option-run codes stay available behind "Details" — this never
 * replaces them, it just stops them from being the FIRST thing read.
 */
function PlainStateBanner({
  strategyId,
  latestJob,
}: Readonly<{ strategyId: string; latestJob: HostedJobSummary | undefined }>) {
  const requestsQuery = useExecutionRequests(strategyId);
  const optionRunsQuery = useOptionRuns(strategyId);
  const [showDetails, setShowDetails] = useState(false);

  const requests = requestsQuery.data?.requests ?? [];
  const optionRuns = optionRunsQuery.data?.runs ?? [];

  const plain = plainStrategyState({
    strategyId,
    job: latestJob ? { status: latestJob.status, job_id: latestJob.job_id } : null,
    executionRequests: requests.map((row) => ({ status: row.status })),
    optionRuns: optionRuns.map((row) => ({ status: row.status, repairable: row.repairable })),
  });

  return (
    <Card data-testid="plain-state-banner">
      <CardContent className="flex flex-col gap-2 pt-6">
        <div className="flex flex-wrap items-center gap-3">
          <span
            className={`inline-flex rounded-full border px-2.5 py-1 text-sm font-medium ${plainStateTone(plain.state)}`}
          >
            {PLAIN_STATE_LABELS[plain.state]}
          </span>
          <p className="text-sm">{plain.sentence}</p>
          {plain.action ? (
            <Button asChild size="sm" variant="outline">
              <Link href={plain.action.href ?? "#"}>{plain.action.label}</Link>
            </Button>
          ) : null}
        </div>
        <button
          type="button"
          className="inline-flex items-center gap-1 self-start text-xs text-muted-foreground underline underline-offset-2"
          onClick={() => setShowDetails((value) => !value)}
        >
          {showDetails ? <ChevronDownIcon className="size-3" aria-hidden /> : <ChevronRightIcon className="size-3" aria-hidden />}
          Details
        </button>
        {showDetails ? (
          <dl className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-3">
            <div>
              <dt className="font-medium text-foreground">Latest job</dt>
              <dd>{latestJob ? jobStatusLabel(latestJob.status) : "none yet"}</dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Execution requests</dt>
              <dd>{requests.length === 0 ? "none" : requests.map((row) => row.status).join(", ")}</dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Option runs</dt>
              <dd>{optionRuns.length === 0 ? "none" : optionRuns.map((row) => row.status).join(", ")}</dd>
            </div>
          </dl>
        ) : null}
      </CardContent>
    </Card>
  );
}

function RegisterVersionForm({ strategyId }: Readonly<{ strategyId: string }>) {
  const mutation = useCreateHostedVersion(strategyId);
  const [source, setSource] = useState("");
  const [schema, setSchema] = useState('{"type": "object", "properties": {}, "required": []}');
  const [capData, setCapData] = useState(true);
  const [capTrade, setCapTrade] = useState(false);
  const [capNotify, setCapNotify] = useState(false);

  async function submit() {
    if (!source.trim()) {
      toast.error("Source is required.");
      return;
    }
    let parametersSchema: Record<string, unknown> | undefined;
    try {
      const parsed = JSON.parse(schema);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("schema");
      }
      parametersSchema = parsed as Record<string, unknown>;
    } catch {
      toast.error("Parameters schema must be a JSON object.");
      return;
    }
    try {
      await mutation.mutateAsync({
        source,
        parameters_schema: parametersSchema,
        capabilities: { data: capData, trade: capTrade, notify: capNotify },
      });
      toast.success("Version registered.");
      setSource("");
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-1.5">
        <Label htmlFor="ver-source">Python source (define `def main(ctx)`)</Label>
        <Textarea
          id="ver-source"
          rows={10}
          className="font-mono text-xs"
          value={source}
          onChange={(event) => setSource(event.target.value)}
          placeholder={"def main(ctx):\n    ctx.progress(\"started\")\n"}
        />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="ver-schema">Parameters schema (JSON Schema object)</Label>
        <Textarea
          id="ver-schema"
          rows={4}
          className="font-mono text-xs"
          value={schema}
          onChange={(event) => setSchema(event.target.value)}
        />
      </div>
      <div className="flex flex-wrap items-center gap-5">
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={capData} onCheckedChange={(value) => setCapData(value === true)} />
          data
        </label>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={capTrade} onCheckedChange={(value) => setCapTrade(value === true)} />
          trade
        </label>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={capNotify} onCheckedChange={(value) => setCapNotify(value === true)} />
          notify
        </label>
        <Button onClick={submit} disabled={mutation.isPending}>
          <ShieldPlusIcon className="size-4" aria-hidden />
          Register version
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Registered versions are immutable: content is pinned by SHA-256 and a launch always binds to a
        specific version.
      </p>
    </div>
  );
}

function VersionTable({ versions }: Readonly<{ versions: HostedVersion[] }>) {
  if (versions.length === 0) {
    return <p className="text-sm text-muted-foreground">No versions registered yet.</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Version</TableHead>
          <TableHead>SHA-256</TableHead>
          <TableHead>Capabilities</TableHead>
          <TableHead>Created</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {[...versions].reverse().map((version) => {
          const capabilities = Object.entries((version.capabilities_snapshot.capabilities ?? {}) as Record<string, boolean>)
            .filter(([, enabled]) => enabled)
            .map(([key]) => key);
          return (
            <TableRow key={version.version_id}>
              <TableCell className="font-medium">v{version.version}</TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {version.source_sha256.slice(0, 12)}…
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">
                {capabilities.length > 0 ? capabilities.join(", ") : "—"}
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">
                {formatTimestamp(version.created_at)}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function RunNowForm({
  strategyId,
  versions,
  jobs,
  defaultAccountScope,
  defaultExecutionMode,
}: Readonly<{
  strategyId: string;
  versions: HostedVersion[];
  jobs: HostedJobSummary[];
  defaultAccountScope: string;
  defaultExecutionMode: string;
}>) {
  const mutation = useRunHostedStrategy(strategyId);
  const optionsQuery = useHostedOptions();
  const options = optionsQuery.data;
  // The mode is the strategy's own pinned mode — the server applies the same
  // value. It is never re-derived from the options query, so a deployment that
  // stops offering live leaves this strategy in live mode (and blocked).
  const pinnedMode = defaultExecutionMode;
  // Until the capability answers, the launch is held: the browser cannot prove
  // the pinned mode is offered, and the server stays the launch authority.
  const capability = modeCapabilityState(options, optionsQuery.isError);
  const gate = runNowGate({ mode: pinnedMode, options, jobs, capability });
  const lanes = liveLaneSummary(options);
  // Versions arrive oldest-first; default the launch to the newest revision so
  // registering a new version does not silently run the previous one.
  const newestVersion = versions.length > 0 ? versions[versions.length - 1] : undefined;
  const [versionId, setVersionId] = useState(newestVersion?.version_id ?? "");
  const [acknowledgedJob, setAcknowledgedJob] = useState<string | null>(null);
  // The retry identity stays INTERNAL: the operator never sees or types it. It
  // is regenerated only when the launch inputs actually change, so a retry of
  // the same request replays the original job and a changed request cannot
  // silently reuse the old key.
  const launchIdentity = useRef<{ fingerprint: string; key: string }>({
    fingerprint: "",
    key: newIdempotencyKey(),
  });

  const selected = versions.find((version) => version.version_id === versionId) ?? newestVersion;
  // The pinned version's own schema drives the inputs; a schema this app cannot
  // show field by field falls back to JSON. Either way the operator's values are
  // what is sent - the platform adds nothing to them.
  const paramValues = useHostedParamValues(selected?.parameters_schema);

  async function submit() {
    if (!selected) {
      toast.error("Register a version before running.");
      return;
    }
    if (!paramValues.valid) {
      toast.error(
        Object.values(paramValues.errors)[0] ?? "Check the parameters before running.",
      );
      return;
    }
    const fingerprint = `${selected.version_id}:${JSON.stringify(paramValues.value)}`;
    if (launchIdentity.current.fingerprint !== fingerprint) {
      launchIdentity.current = { fingerprint, key: newIdempotencyKey() };
    }
    try {
      const response = await mutation.mutateAsync({
        version_id: selected.version_id,
        params: paramValues.value,
        idempotency_key: launchIdentity.current.key,
      });
      setAcknowledgedJob(response.job.job_id);
      toast.success(runNowMessage(response.idempotent));
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Alert>
        <AlertTitle>Run now queues an attempt — it does not start the process</AlertTitle>
        <AlertDescription>
          {/* One child: the description is a grid, so inline elements would
              otherwise each be laid out as their own row. */}
          <span>
            The supervisor picks the job up and launches it. Account scope{" "}
            <code>{defaultAccountScope}</code> (authorized) and the strategy&apos;s mode (
            <strong>{executionModeLabel(pinnedMode)}</strong>) are pinned by the server — the launch
            request cannot switch them.
          </span>
        </AlertDescription>
      </Alert>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="grid gap-1.5">
          <Label htmlFor="run-version">Immutable version</Label>
          <Select value={selected?.version_id ?? ""} onValueChange={setVersionId}>
            <SelectTrigger id="run-version" className="w-full">
              <SelectValue placeholder="Select a version" />
            </SelectTrigger>
            <SelectContent>
              {versions.map((version) => (
                <SelectItem key={version.version_id} value={version.version_id}>
                  v{version.version}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label>Execution mode</Label>
          <div className="flex min-h-9 flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <Badge
              variant={pinnedMode === LIVE_MODE ? "destructive" : "outline"}
              data-testid="run-now-mode"
            >
              {executionModeLabel(pinnedMode)}
            </Badge>
            <span>
              {options
                ? `supported here: ${
                    supportedExecutionModes(options).map(executionModeLabel).join(", ") || "none reported"
                  }`
                : optionsQuery.isError
                  ? "mode capability unavailable"
                  : "loading modes…"}
            </span>
          </div>
        </div>
        <div className="md:col-span-2">
          <HostedParamInputs params={paramValues} idPrefix="run" />
        </div>
        <p className="text-xs text-muted-foreground md:col-span-2">
          Retrying an unchanged launch replays the original attempt instead of creating a second one.
          Changing the version or the parameters starts a new launch.
        </p>
      </div>
      {pinnedMode === LIVE_MODE ? (
        <Alert>
          <AlertTitle>
            {lanes
              ? `Live lanes this deployment supports: ${lanes}`
              : "Live lanes were not reported by this server"}
          </AlertTitle>
          <AlertDescription>
            A live attempt places real orders. It needs the owner&apos;s explicit approval of the
            strategy&apos;s plan before anything is sent — either your approval of that plan here, or the
            standing authorization you issue for this version, account, environment and limits. The
            platform never approves a plan by itself.
          </AlertDescription>
        </Alert>
      ) : null}
      <div className="flex items-center gap-3">
        <Button onClick={submit} disabled={mutation.isPending || !selected || gate.blocked}>
          <PlayIcon className="size-4" aria-hidden />
          Run now
        </Button>
        {acknowledgedJob ? (
          <Link href={`/strategies/${strategyId}/jobs/${acknowledgedJob}`} className="text-sm underline">
            View queued job
          </Link>
        ) : null}
      </div>
      {gate.blocked ? (
        <p className="text-xs text-muted-foreground" data-testid="run-now-blocked-reason">
          {gate.reason}
        </p>
      ) : null}
    </div>
  );
}

export function HostedStrategyDetailPage({ strategyId }: Readonly<{ strategyId: string }>) {
  const strategyQuery = useHostedStrategy(strategyId);
  const versionsQuery = useHostedVersions(strategyId);
  const jobsQuery = useHostedJobs(strategyId);
  const optionsQuery = useHostedOptions();
  const updateMutation = useUpdateHostedStrategy(strategyId);

  const strategy = strategyQuery.data;
  const versions = versionsQuery.data?.versions ?? [];
  const jobs = jobsQuery.data?.jobs ?? [];

  if (strategyQuery.isLoading) {
    return <Skeleton className="h-40 w-full rounded-xl" />;
  }
  if (strategyQuery.isError || !strategy) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load strategy</AlertTitle>
        <AlertDescription>{hostedErrorMessage(strategyQuery.error)}</AlertDescription>
      </Alert>
    );
  }

  const enabled = strategy.status === "active";

  async function toggle() {
    try {
      await updateMutation.mutateAsync({ status: enabled ? "disabled" : "active" });
      toast.success(enabled ? "Strategy disabled." : "Strategy enabled.");
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  // `[&>*]:min-w-0` lets each card shrink to the column and scroll its own
  // table, instead of a wide table setting the width of the whole page.
  return (
    <div className="flex flex-col gap-6 [&>*]:min-w-0">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <SectionLabel
          className="min-w-0 [&>p]:break-all"
          eyebrow={`Hosted strategy · ${strategy.strategy_id}`}
          title={strategy.name}
          description={strategy.description ?? undefined}
        />
        <div className="flex items-center gap-3">
          <Badge variant={enabled ? "default" : "secondary"}>{enabled ? "Enabled" : "Disabled"}</Badge>
          <Badge variant="outline">
            {authorizationModeLabel(strategy.authorization_mode ?? APPROVAL_BASED)}
          </Badge>
          <Button variant="outline" size="sm" onClick={toggle} disabled={updateMutation.isPending}>
            {enabled ? "Disable" : "Enable"}
          </Button>
        </div>
      </div>

      <PlainStateBanner strategyId={strategyId} latestJob={jobs[0]} />

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Account scope</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">{strategy.default_account_scope}</CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Default mode / kind</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {executionModeLabel(strategy.default_execution_mode)} · {strategy.default_job_kind}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Duration / progress deadline</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            {strategy.max_duration_s}s · {strategy.progress_deadline_s}s
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Run now</CardTitle>
          <CardDescription>Start a supervised attempt bound to an immutable version.</CardDescription>
        </CardHeader>
        <CardContent>
          <RunNowForm
            strategyId={strategyId}
            versions={versions}
            jobs={jobs}
            defaultAccountScope={strategy.default_account_scope}
            defaultExecutionMode={strategy.default_execution_mode}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Review or automate</CardTitle>
          <CardDescription>
            Who approves trades: your decision on every plan, or your standing authorization for this
            exact version, account, environment and limits.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HostedAuthorizationPanel
            strategy={strategy}
            versions={versions}
            options={optionsQuery.data}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Execution requests</CardTitle>
          <CardDescription>
            What the strategy asked the platform to do, and what actually happened.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HostedExecutionRequestsPanel strategyId={strategyId} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Schedule</CardTitle>
          <CardDescription>
            Start a new attempt of a pinned version at a fixed local time, with the platform&apos;s own
            missed-run and overlap rules.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HostedSchedulePanel strategy={strategy} versions={versions} options={optionsQuery.data} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Options</CardTitle>
          <CardDescription>
            Option runs this strategy owns, their frozen policies and refusals, governed repair for
            stranded runs, and the separated controls for stopping, cancelling, exiting or flattening.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HostedOptionsPanel strategyId={strategyId} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Strategy book</CardTitle>
          <CardDescription>
            Positions attributed to this strategy, not the account&apos;s net position.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HostedExposurePanel
            strategyId={strategyId}
            options={optionsQuery.data}
            defaultEnvironment={strategy.default_execution_mode}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Versions</CardTitle>
          <CardDescription>Immutable source revisions.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
          {versionsQuery.isLoading ? (
            <Skeleton className="h-20 w-full rounded-lg" />
          ) : (
            <VersionTable versions={versions} />
          )}
          <Separator />
          <RegisterVersionForm strategyId={strategyId} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Jobs</CardTitle>
          <CardDescription>Attempts for this strategy, newest first.</CardDescription>
        </CardHeader>
        <CardContent>
          {jobsQuery.isLoading ? (
            <Skeleton className="h-20 w-full rounded-lg" />
          ) : jobs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No attempts yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Attempt</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Mode</TableHead>
                  <TableHead>Replacement</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => (
                  <TableRow key={job.job_id}>
                    <TableCell>
                      <Link href={`/strategies/${strategyId}/jobs/${job.job_id}`} className="font-medium hover:underline">
                        #{job.attempt}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <span
                        className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${jobStatusTone(job.status)}`}
                      >
                        {jobStatusLabel(job.status)}
                      </span>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">{job.execution_mode}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {job.replacement_blocked ? "Blocked" : "Clear"}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatTimestamp(job.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
