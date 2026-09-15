"use client";

import Link from "next/link";
import { useState } from "react";
import { PlayIcon, RefreshCcwIcon, ShieldPlusIcon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
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
  useHostedJobs,
  useHostedOptions,
  useHostedStrategy,
  useHostedVersions,
  useRunHostedStrategy,
  useUpdateHostedStrategy,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import { hostedErrorMessage, jobStatusLabel, jobStatusTone, newIdempotencyKey, runNowMessage } from "@/features/strategies/lib/format";
import { parseParamsInput } from "@/features/strategies/lib/params";
import type { HostedVersion } from "@/lib/hosted-strategies/types";

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
              <TableCell className="text-sm text-muted-foreground">{version.created_at ?? "—"}</TableCell>
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
  defaultAccountScope,
  defaultExecutionMode,
}: Readonly<{
  strategyId: string;
  versions: HostedVersion[];
  defaultAccountScope: string;
  defaultExecutionMode: string;
}>) {
  const mutation = useRunHostedStrategy(strategyId);
  const optionsQuery = useHostedOptions();
  // Versions arrive oldest-first; default the launch to the newest revision so
  // registering a new version does not silently run the previous one.
  const newestVersion = versions.length > 0 ? versions[versions.length - 1] : undefined;
  const [versionId, setVersionId] = useState(newestVersion?.version_id ?? "");
  const [params, setParams] = useState("{}");
  const [idempotencyKey, setIdempotencyKey] = useState(() => newIdempotencyKey());
  const [acknowledgedJob, setAcknowledgedJob] = useState<string | null>(null);

  const selected = versions.find((version) => version.version_id === versionId) ?? newestVersion;

  async function submit() {
    if (!selected) {
      toast.error("Register a version before running.");
      return;
    }
    const parsed = parseParamsInput(params);
    if (!parsed.ok) {
      toast.error(parsed.error);
      return;
    }
    try {
      const response = await mutation.mutateAsync({
        version_id: selected.version_id,
        params: parsed.value,
        idempotency_key: idempotencyKey,
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
          The supervisor picks the job up and launches it. Account scope <code>{defaultAccountScope}</code>{" "}
          (authorized) is pinned by the server.
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
          <div className="flex h-9 items-center gap-2 text-sm text-muted-foreground">
            <Badge variant="outline">{defaultExecutionMode}</Badge>
            <span>
              {optionsQuery.data
                ? `supported: ${optionsQuery.data.execution_modes.join(", ")}`
                : "loading modes…"}
            </span>
          </div>
        </div>
        <div className="grid gap-1.5 md:col-span-2">
          <Label htmlFor="run-params">Parameters (JSON)</Label>
          <Textarea
            id="run-params"
            rows={3}
            className="font-mono text-xs"
            value={params}
            onChange={(event) => setParams(event.target.value)}
          />
        </div>
        <div className="grid gap-1.5 md:col-span-2">
          <Label htmlFor="run-key">Idempotency key</Label>
          <div className="flex items-center gap-2">
            <Input
              id="run-key"
              readOnly
              value={idempotencyKey}
              className="font-mono text-xs"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setIdempotencyKey(newIdempotencyKey())}
            >
              <RefreshCcwIcon className="size-4" aria-hidden />
              New key
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Retries of this same launch reuse the key and replay the original job. Use “New key” only
            for a genuinely new launch.
          </p>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <Button onClick={submit} disabled={mutation.isPending || !selected}>
          <PlayIcon className="size-4" aria-hidden />
          Run now
        </Button>
        {acknowledgedJob ? (
          <Link href={`/strategies/${strategyId}/jobs/${acknowledgedJob}`} className="text-sm underline">
            View queued job
          </Link>
        ) : null}
      </div>
    </div>
  );
}

export function HostedStrategyDetailPage({ strategyId }: Readonly<{ strategyId: string }>) {
  const strategyQuery = useHostedStrategy(strategyId);
  const versionsQuery = useHostedVersions(strategyId);
  const jobsQuery = useHostedJobs(strategyId);
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

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <SectionLabel
          eyebrow={`Hosted strategy · ${strategy.strategy_id}`}
          title={strategy.name}
          description={strategy.description ?? undefined}
        />
        <div className="flex items-center gap-3">
          <Badge variant={enabled ? "default" : "secondary"}>{enabled ? "Enabled" : "Disabled"}</Badge>
          <Button variant="outline" size="sm" onClick={toggle} disabled={updateMutation.isPending}>
            {enabled ? "Disable" : "Enable"}
          </Button>
        </div>
      </div>

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
            {strategy.default_execution_mode} · {strategy.default_job_kind}
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
            defaultAccountScope={strategy.default_account_scope}
            defaultExecutionMode={strategy.default_execution_mode}
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
                    <TableCell className="text-sm text-muted-foreground">{job.created_at ?? "—"}</TableCell>
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
