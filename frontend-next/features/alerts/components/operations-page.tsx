"use client";

import { AlertCircleIcon, CircleHelpIcon, PlusIcon, SendIcon } from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel } from "@/components/operator/panel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { OneTimeSecretDialog } from "@/features/alerts/components/one-time-secret-dialog";
import {
  useAlertsChannelMutations,
  useAlertsChannels,
  useAlertsPlatformHealth,
  useAlertsProducerMutations,
  useAlertsProducers,
  useAlertsSignalsHealth,
  useAlertsTokenMutations,
  useAlertsTokenPresets,
  useAlertsTokens,
} from "@/features/alerts/hooks/use-alerts-queries";
import { formatTimestamp } from "@/features/alerts/lib/format";
import { readRuntimeAvailability } from "@/features/alerts/lib/health";

const PROVIDERS = ["telegram", "ntfy"];

// ---------------------------------------------------------------------------
// channels
// ---------------------------------------------------------------------------

export function ChannelsPanel({ scope }: Readonly<{ scope: string | null }>) {
  const channelsQuery = useAlertsChannels(scope);
  const { upsert, test } = useAlertsChannelMutations(scope);
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("telegram");
  const [secretEnv, setSecretEnv] = useState("");
  const [destination, setDestination] = useState("{}");
  const [revealedFor, setRevealedFor] = useState<string | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  const channels = channelsQuery.data?.channels ?? [];

  return (
    <div className="flex flex-col gap-4">
      <Panel tone="subtle">
        <h3 className="text-sm font-semibold">Add or update a destination</h3>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="channel-name">Name</Label>
            <Input id="channel-name" value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="channel-provider">Provider</Label>
            <Select value={provider} onValueChange={setProvider}>
              <SelectTrigger id="channel-provider">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PROVIDERS.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="channel-secret-env">secret_env (name, not a value)</Label>
            <Input
              id="channel-secret-env"
              value={secretEnv}
              placeholder="TELEGRAM_BOT_TOKEN"
              onChange={(event) => setSecretEnv(event.target.value)}
            />
          </div>
        </div>
        <div className="mt-3 flex flex-col gap-1">
          <Label htmlFor="channel-destination">destination (JSON)</Label>
          <Textarea
            id="channel-destination"
            rows={2}
            className="font-mono text-xs"
            value={destination}
            onChange={(event) => setDestination(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            secret_env names a server-side environment variable resolved at send time. The secret
            value is never stored here and never returned by the API.
          </p>
          {configError ? <p className="text-xs text-rose-300">{configError}</p> : null}
        </div>
        <Button
          className="mt-3"
          size="sm"
          disabled={!name || upsert.isPending}
          onClick={() => {
            try {
              const parsed = JSON.parse(destination || "{}");
              setConfigError(null);
              upsert.mutate({
                name,
                provider,
                secret_env: secretEnv || null,
                destination: parsed,
                enabled: true,
              });
            } catch (error) {
              setConfigError(error instanceof Error ? error.message : "invalid JSON");
            }
          }}
        >
          <PlusIcon className="size-4" aria-hidden />
          Save channel
        </Button>
      </Panel>

      {channelsQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-xl" />
      ) : channels.length === 0 ? (
        <p className="text-sm text-muted-foreground">No destinations configured yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {channels.map((channel) => (
            <li
              key={channel.channel_id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/60 p-3"
            >
              <div>
                <p className="text-sm font-medium">
                  {channel.name} <span className="text-muted-foreground">· {channel.provider}</span>
                </p>
                <p className="font-mono text-xs text-muted-foreground">
                  secret_env: {channel.secret_env ?? "none"}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant={channel.enabled ? "secondary" : "destructive"}>
                  {channel.enabled ? "enabled" : "disabled"}
                </Badge>
                {/* Sending a real message is a deliberate two-step action. */}
                {revealedFor === channel.channel_id ? (
                  <span className="flex items-center gap-2">
                    <span className="text-xs text-amber-300">This sends a real message.</span>
                    <Button
                      size="xs"
                      disabled={test.isPending}
                      onClick={() => {
                        test.mutate(channel.channel_id);
                        setRevealedFor(null);
                      }}
                    >
                      Confirm send
                    </Button>
                    <Button size="xs" variant="ghost" onClick={() => setRevealedFor(null)}>
                      Cancel
                    </Button>
                  </span>
                ) : (
                  <Button size="xs" variant="outline" onClick={() => setRevealedFor(channel.channel_id)}>
                    <SendIcon className="size-3" aria-hidden />
                    Send test
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {test.data ? (
        <Alert>
          <AlertTitle>Test result: {test.data.status}</AlertTitle>
          <AlertDescription>
            {test.data.detail || "Provider responded."}
            {test.data.provider_id ? ` (provider id ${test.data.provider_id})` : ""}
          </AlertDescription>
        </Alert>
      ) : null}
      {test.error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Test failed</AlertTitle>
          <AlertDescription>
            {test.error instanceof Error ? test.error.message : "Unknown error"}
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// worker tokens
// ---------------------------------------------------------------------------

export function TokensPanel({ scope }: Readonly<{ scope: string | null }>) {
  const tokensQuery = useAlertsTokens(scope);
  const presetsQuery = useAlertsTokenPresets(scope);
  const { create, revoke } = useAlertsTokenMutations(scope);

  const [label, setLabel] = useState("");
  const [preset, setPreset] = useState<string | null>(null);
  // The secret lives here, in component state, and nowhere else.
  const [secret, setSecret] = useState<string | null>(null);

  const tokens = tokensQuery.data?.tokens ?? [];
  const presets = presetsQuery.data?.presets ?? [];
  const accountScope = presetsQuery.data?.account_scope ?? scope ?? "—";

  return (
    <div className="flex flex-col gap-4">
      <Panel tone="subtle">
        <h3 className="text-sm font-semibold">Mint a worker token</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {presetsQuery.data?.note ??
            "Tokens carry least-privilege presets. No execution action is offered here."}
        </p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="token-label">Label</Label>
            <Input id="token-label" value={label} onChange={(event) => setLabel(event.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="token-preset">Preset</Label>
            <Select value={preset ?? presets[0]?.id} onValueChange={setPreset}>
              <SelectTrigger id="token-preset">
                <SelectValue placeholder="Select preset" />
              </SelectTrigger>
              <SelectContent>
                {presets.map((option) => (
                  <SelectItem key={option.id} value={option.id}>
                    {option.id} <span className="text-muted-foreground">· {option.description}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          Account scope is fixed server-side to <span className="font-mono">{accountScope}</span>.
          A token&apos;s scope <em>is</em> the alerts owner, which is why it cannot be chosen here —
          and why no execution scope is offered at all.
        </p>
        <Button
          className="mt-3"
          size="sm"
          disabled={!label || create.isPending}
          onClick={() =>
            create.mutate(
              { label, preset: preset ?? presets[0]?.id },
              {
                onSuccess: (data) => {
                  // Copy the secret into local state, then clear the mutation
                  // cache so it cannot be re-rendered later from memory.
                  setSecret(data.token);
                  create.reset();
                },
              },
            )
          }
        >
          <PlusIcon className="size-4" aria-hidden />
          Create token
        </Button>
      </Panel>

      {tokensQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-xl" />
      ) : tokens.length === 0 ? (
        <p className="text-sm text-muted-foreground">No worker tokens issued yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {tokens.map((token) => (
            <li
              key={token.token_id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/60 p-3"
            >
              <div className="min-w-0">
                <p className="text-sm font-medium">{token.label ?? token.token_id}</p>
                <p className="font-mono text-xs text-muted-foreground">
                  scope: {token.account_scope ?? "—"} · actions:{" "}
                  {token.allowed_actions.join(", ") || "none"}
                </p>
                {!token.scope_matches_operator ? (
                  // Surfaced rather than hidden: a mismatched scope silently
                  // reads an empty alerts view, which looks like "nothing is
                  // configured" instead of "wrong scope".
                  <p className="mt-1 text-xs text-amber-300">
                    This token&apos;s scope is not one you are authorized for — it will see a
                    different (possibly empty) set of alerts.
                  </p>
                ) : null}
              </div>
              <div className="flex items-center gap-2">
                <Badge variant={token.status === "active" ? "secondary" : "destructive"}>
                  {token.status ?? "unknown"}
                </Badge>
                <Button
                  size="xs"
                  variant="outline"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(token.token_id)}
                >
                  Revoke
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <OneTimeSecretDialog
        open={secret !== null}
        title="Worker token created"
        description="Store this token now. It is the only time it will be shown."
        secret={secret}
        onClose={() => setSecret(null)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// external producers
// ---------------------------------------------------------------------------

export function ProducersPanel({ scope }: Readonly<{ scope: string | null }>) {
  const producersQuery = useAlertsProducers(scope);
  const healthQuery = useAlertsSignalsHealth(scope);
  const { create, revoke, issueCredential } = useAlertsProducerMutations(scope);

  const [name, setName] = useState("");
  const [ttl, setTtl] = useState("");
  const [secret, setSecret] = useState<string | null>(null);

  const producers = producersQuery.data?.producers ?? [];
  const limits = healthQuery.data?.limits;

  return (
    <div className="flex flex-col gap-4">
      <Panel tone="subtle">
        <h3 className="text-sm font-semibold">Register a producer</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          A producer credential is <strong>not</strong> a worker token: it can only submit values
          for its own producer, and cannot read alerts, runs or orders.
        </p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="producer-name">Name</Label>
            <Input id="producer-name" value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="producer-ttl">Default TTL (seconds, optional)</Label>
            <Input
              id="producer-ttl"
              type="number"
              value={ttl}
              onChange={(event) => setTtl(event.target.value)}
            />
          </div>
        </div>
        <Button
          className="mt-3"
          size="sm"
          disabled={!name || create.isPending}
          onClick={() =>
            create.mutate({ name, default_ttl_s: ttl === "" ? null : Number(ttl) })
          }
        >
          <PlusIcon className="size-4" aria-hidden />
          Register
        </Button>
      </Panel>

      {limits ? (
        <Panel tone="subtle">
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
            Sampling &amp; limits
          </p>
          <ul className="mt-2 flex flex-wrap gap-4 text-xs text-muted-foreground">
            <li>max payload: {limits.max_payload_bytes} bytes</li>
            <li>max fields: {limits.max_fields}</li>
            <li>future skew: {limits.max_future_skew_s}s</li>
            <li>max lateness: {limits.max_lateness_s}s</li>
            <li>retention: {limits.retention_s}s</li>
            <li>rows/producer: {limits.max_rows_per_producer}</li>
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">{healthQuery.data?.note}</p>
        </Panel>
      ) : null}

      {producersQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-xl" />
      ) : producers.length === 0 ? (
        <p className="text-sm text-muted-foreground">No producers registered yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {producers.map((producer) => (
            <li
              key={producer.name}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border/60 p-3"
            >
              <div>
                <p className="text-sm font-medium">{producer.name}</p>
                <p className="text-xs text-muted-foreground">
                  ttl: {producer.default_ttl_s ?? "default"}
                  {producer.revoked_at
                    ? ` · revoked ${formatTimestamp(producer.revoked_at) ?? ""}`
                    : ""}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  size="xs"
                  variant="outline"
                  disabled={issueCredential.isPending}
                  onClick={() =>
                    issueCredential.mutate(producer.name, {
                      onSuccess: (data) => {
                        setSecret(data.secret);
                        issueCredential.reset();
                      },
                    })
                  }
                >
                  Issue credential
                </Button>
                <Button
                  size="xs"
                  variant="outline"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(producer.name)}
                >
                  Revoke producer
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <p className="flex items-start gap-2 text-xs text-muted-foreground">
        <CircleHelpIcon className="mt-0.5 size-3 shrink-0" aria-hidden />
        Values are SAMPLED by the consuming stage&apos;s candle clock and never trigger evaluation
        on their own, so a value can expire between evaluations. There is no fallback: a missing or
        expired value makes the condition unknown rather than false.
      </p>

      <OneTimeSecretDialog
        open={secret !== null}
        title="Producer credential issued"
        description="Store this now — it is a producer credential, not a worker token."
        secret={secret}
        onClose={() => setSecret(null)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// platform health
// ---------------------------------------------------------------------------

export function PlatformHealthPanel({ scope }: Readonly<{ scope: string | null }>) {
  const healthQuery = useAlertsPlatformHealth(scope);

  if (healthQuery.isLoading) return <Skeleton className="h-24 w-full rounded-xl" />;
  if (!healthQuery.data) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Platform health unavailable</AlertTitle>
        <AlertDescription>
          {healthQuery.error instanceof Error ? healthQuery.error.message : "No data."}
        </AlertDescription>
      </Alert>
    );
  }

  const runtime = readRuntimeAvailability(healthQuery.data.runtime);

  return (
    <Panel tone="subtle">
      <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
        Evaluation worker
      </p>
      {runtime.kind === "unknown" ? (
        <p className="mt-2 text-sm">
          <span className="font-medium">Unknown</span> ({runtime.reason}). {runtime.note}
        </p>
      ) : (
        <ul className="mt-2 flex flex-wrap gap-4 text-sm">
          <li>quarantined: {runtime.quarantined}</li>
          <li>failing subscriptions: {runtime.failedSubscriptions}</li>
        </ul>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

export function OperationsPage({ scope }: Readonly<{ scope: string | null }>) {
  const [tab, setTab] = useState<"channels" | "tokens" | "producers" | "health">("channels");

  const tabs = [
    ["channels", "Channels"],
    ["tokens", "Worker tokens"],
    ["producers", "External producers"],
    ["health", "Platform health"],
  ] as const;

  return (
    <div className="flex flex-col gap-6 pb-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-[0.28em] text-foreground/40">Alerts</p>
          <h2 className="mt-1 text-lg font-semibold tracking-tight">Operations</h2>
          <p className="max-w-2xl text-sm text-foreground/60">
            Destinations, credentials and runtime health. No execution scope is introduced by
            anything on this page.
          </p>
        </div>
      </div>

      <div role="tablist" aria-label="Operations sections" className="flex flex-wrap gap-2">
        {tabs.map(([value, label]) => (
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

      <div>
        {tab === "channels" ? <ChannelsPanel scope={scope} /> : null}
        {tab === "tokens" ? <TokensPanel scope={scope} /> : null}
        {tab === "producers" ? <ProducersPanel scope={scope} /> : null}
        {tab === "health" ? <PlatformHealthPanel scope={scope} /> : null}
      </div>
    </div>
  );
}
