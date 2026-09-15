"use client";

import { AlertCircleIcon, CircleHelpIcon, PlusIcon, SendIcon } from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
  useAlertsProducerCredentials,
  useAlertsProducerMutations,
  useAlertsProducers,
  useAlertsSignalValues,
  useAlertsSignalsHealth,
  useAlertsTokenMutations,
  useAlertsTokenPresets,
  useAlertsTokens,
} from "@/features/alerts/hooks/use-alerts-queries";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";
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
        {upsert.error ? (
          <p className="mt-2 text-xs text-rose-300">
            {alertsErrorMessage(upsert.error, "Could not save the channel.")}
          </p>
        ) : null}
      </Panel>

      {channelsQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-xl" />
      ) : channelsQuery.error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Could not load destinations</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(channelsQuery.error, "The channels request failed.")}
          </AlertDescription>
        </Alert>
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
            {/* A missing environment variable is a 400 whose body names the
                variable, which is the actionable part (handoff §10). */}
            {alertsErrorMessage(test.error, "Unknown error")}
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
  // Advanced: individual actions for the supported alerts-platform surface.
  const [mode, setMode] = useState<"preset" | "custom">("preset");
  const [actions, setActions] = useState<string[]>([]);
  const [modes, setModes] = useState<string[]>(["paper"]);
  // The secret lives here, in component state, and nowhere else.
  const [secret, setSecret] = useState<string | null>(null);

  const tokens = tokensQuery.data?.tokens ?? [];
  const presets = presetsQuery.data?.presets ?? [];
  const allActions = presetsQuery.data?.all_actions ?? [];
  const allModes = presetsQuery.data?.modes ?? [];
  const accountScope = presetsQuery.data?.account_scope ?? scope ?? "—";

  const canCreate =
    Boolean(label) &&
    (mode === "preset" ? Boolean(preset ?? presets[0]?.id) : actions.length > 0) &&
    !create.isPending;

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
            <Label htmlFor="token-scope">Account scope (fixed)</Label>
            <Input id="token-scope" value={accountScope} readOnly aria-readonly="true" />
          </div>
        </div>

        <div role="radiogroup" aria-label="Token actions" className="mt-3 flex flex-wrap gap-2">
          {([
            ["preset", "Least-privilege preset"],
            ["custom", "Individual actions (advanced)"],
          ] as const).map(([value, text]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={mode === value}
              onClick={() => setMode(value)}
              className={
                mode === value
                  ? "rounded-full border border-primary/60 bg-primary/10 px-3 py-1 text-xs text-primary"
                  : "rounded-full border border-border/60 px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
              }
            >
              {text}
            </button>
          ))}
        </div>

        {mode === "preset" ? (
          <div className="mt-3 flex flex-col gap-1 sm:max-w-md">
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
        ) : (
          <div className="mt-3 flex flex-col gap-3">
            <fieldset className="flex flex-col gap-1">
              <legend className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
                Actions
              </legend>
              {allActions.map((action) => (
                <label key={action} className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={actions.includes(action)}
                    onCheckedChange={(checked) =>
                      setActions((current) =>
                        checked === true ? [...current, action] : current.filter((item) => item !== action),
                      )
                    }
                  />
                  <span className="font-mono text-xs">{action}</span>
                </label>
              ))}
              <p className="mt-1 text-xs text-muted-foreground">
                Only alerts-platform actions are listed. Execution actions (order submission, risk
                updates, runs, GTT) are not offered by this surface and are refused by the server.
              </p>
            </fieldset>
            <fieldset className="flex flex-col gap-1">
              <legend className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
                Modes
              </legend>
              <div className="flex flex-wrap gap-3">
                {allModes.map((option) => (
                  <label key={option} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={modes.includes(option)}
                      onCheckedChange={(checked) =>
                        setModes((current) =>
                          checked === true
                            ? [...current, option]
                            : current.filter((item) => item !== option),
                        )
                      }
                    />
                    {option}
                  </label>
                ))}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                A live-capable alerts token is not offered: alerts never place orders.
              </p>
            </fieldset>
          </div>
        )}

        <p className="mt-2 text-xs text-muted-foreground">
          Account scope is fixed server-side to <span className="font-mono">{accountScope}</span>.
          A token&apos;s scope <em>is</em> the alerts owner, which is why it cannot be chosen here —
          and why the generated token reads exactly the workflows this page shows.
        </p>
        <Button
          className="mt-3"
          size="sm"
          disabled={!canCreate}
          onClick={() =>
            create.mutate(
              mode === "preset"
                ? { label, preset: preset ?? presets[0]?.id }
                : { label, allowed_actions: actions, allowed_modes: modes },
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
        {presetsQuery.error ? (
          <p className="mt-2 text-xs text-rose-300">
            {alertsErrorMessage(presetsQuery.error, "Could not load the presets.")}
          </p>
        ) : null}
        {create.error ? (
          <p className="mt-2 text-xs text-rose-300">
            {alertsErrorMessage(create.error, "Could not create the token.")}
          </p>
        ) : null}
      </Panel>

      {tokensQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-xl" />
      ) : tokensQuery.error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Could not load worker tokens</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(tokensQuery.error, "The tokens request failed.")}
          </AlertDescription>
        </Alert>
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

      {revoke.error ? (
        <p className="text-xs text-rose-300">
          {alertsErrorMessage(revoke.error, "Could not revoke the token.")}
        </p>
      ) : null}

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
  const { create, revoke, issueCredential, revokeCredential } = useAlertsProducerMutations(scope);

  const [name, setName] = useState("");
  const [ttl, setTtl] = useState("");
  const [schemaText, setSchemaText] = useState("{}");
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);

  const producers = producersQuery.data?.producers ?? [];
  const limits = healthQuery.data?.limits;
  const producerHealth = healthQuery.data?.producers ?? [];

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
        <div className="mt-3 flex flex-col gap-1">
          <Label htmlFor="producer-schema">Value schema (JSON, optional)</Label>
          <Textarea
            id="producer-schema"
            rows={3}
            className="font-mono text-xs"
            value={schemaText}
            onChange={(event) => setSchemaText(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Declared fields the producer is allowed to submit (typed scalars only — there is no code
            execution in the ingestion path).
          </p>
          {schemaError ? <p className="text-xs text-rose-300">{schemaError}</p> : null}
        </div>
        <Button
          className="mt-3"
          size="sm"
          disabled={!name || create.isPending}
          onClick={() => {
            let valueSchema: Record<string, unknown> = {};
            try {
              const parsed = JSON.parse(schemaText || "{}");
              if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("value schema must be a JSON object");
              }
              valueSchema = parsed as Record<string, unknown>;
            } catch (error) {
              setSchemaError(error instanceof Error ? error.message : "invalid JSON");
              return;
            }
            setSchemaError(null);
            create.mutate({
              name,
              value_schema: valueSchema,
              default_ttl_s: ttl === "" ? null : Number(ttl),
            });
          }}
        >
          <PlusIcon className="size-4" aria-hidden />
          Register
        </Button>
        {create.error ? (
          <p className="mt-2 text-xs text-rose-300">
            {alertsErrorMessage(create.error, "Could not register the producer.")}
          </p>
        ) : null}
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
      ) : healthQuery.error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Could not load signal health</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(healthQuery.error, "The signals health request failed.")}
          </AlertDescription>
        </Alert>
      ) : null}

      {/* Per-producer counters: accepted/late, expired-now, last receipt,
          revoked/disabled — the operational half of the producer surface. */}
      {producerHealth.length > 0 ? (
        <Panel tone="subtle">
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
            Producer status
          </p>
          <ul className="mt-2 flex flex-col gap-1 text-xs text-muted-foreground">
            {producerHealth.map((entry) => {
              const name = String(entry.name ?? entry.producer ?? "—");
              const parts = Object.entries(entry)
                .filter(([key]) => key !== "name" && key !== "producer")
                .map(([key, value]) => `${key}: ${String(value)}`);
              return (
                <li key={name}>
                  <span className="font-mono text-foreground">{name}</span> — {parts.join(" · ")}
                </li>
              );
            })}
          </ul>
        </Panel>
      ) : null}

      {producersQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-xl" />
      ) : producersQuery.error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Could not load producers</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(producersQuery.error, "The producers request failed.")}
          </AlertDescription>
        </Alert>
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
                  {producer.value_schema && Object.keys(producer.value_schema).length > 0
                    ? ` · schema: ${Object.keys(producer.value_schema).join(", ")}`
                    : ""}
                  {producer.revoked_at
                    ? ` · revoked ${formatTimestamp(producer.revoked_at) ?? ""}`
                    : ""}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  size="xs"
                  variant="ghost"
                  aria-expanded={expanded === producer.name}
                  onClick={() => setExpanded(expanded === producer.name ? null : producer.name)}
                >
                  {expanded === producer.name ? "Hide details" : "Details"}
                </Button>
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
              {expanded === producer.name ? (
                <div className="w-full">
                  <ProducerDetails
                    producer={producer.name}
                    scope={scope}
                    onRevokeCredential={(tokenId) =>
                      revokeCredential.mutate({ name: producer.name, tokenId })
                    }
                    revoking={revokeCredential.isPending}
                  />
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {revoke.error || issueCredential.error || revokeCredential.error ? (
        <ul className="flex flex-col gap-1 text-xs text-rose-300">
          {revoke.error ? <li>{alertsErrorMessage(revoke.error, "Could not revoke the producer.")}</li> : null}
          {issueCredential.error ? (
            <li>{alertsErrorMessage(issueCredential.error, "Could not issue a credential.")}</li>
          ) : null}
          {revokeCredential.error ? (
            <li>{alertsErrorMessage(revokeCredential.error, "Could not revoke the credential.")}</li>
          ) : null}
        </ul>
      ) : null}

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

type ProducerDetailsProps = Readonly<{
  producer: string;
  scope: string | null;
  onRevokeCredential: (tokenId: string) => void;
  revoking: boolean;
}>;

/**
 * Credential metadata and recent values for one producer.
 *
 * Credentials are NON-SECRET metadata read from the API so an operator can
 * revoke one later by its token id; the secret itself is shown once at issue
 * time and is not recoverable. Values are shown with their `status` and
 * `expires_at` because a retained value may already be unusable by a rule.
 */
function ProducerDetails({ producer, scope, onRevokeCredential, revoking }: ProducerDetailsProps) {
  const credentialsQuery = useAlertsProducerCredentials(producer, scope);
  const valuesQuery = useAlertsSignalValues(producer, scope, 10);

  const credentials = credentialsQuery.data?.credentials ?? [];
  const values = valuesQuery.data?.values ?? [];

  return (
    <div className="mt-3 grid gap-4 border-t border-border/50 pt-3 lg:grid-cols-2">
      <div className="flex flex-col gap-2">
        <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">Credentials</p>
        {credentialsQuery.isLoading ? (
          <Skeleton className="h-16 w-full rounded-lg" />
        ) : credentialsQuery.error ? (
          <p className="text-xs text-rose-300">
            {alertsErrorMessage(credentialsQuery.error, "Could not load credentials.")}
          </p>
        ) : credentials.length === 0 ? (
          <p className="text-sm text-muted-foreground">No credentials issued.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {credentials.map((credential) => (
              <li
                key={credential.token_id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border/50 p-2"
              >
                <div>
                  <p className="font-mono text-xs">{credential.token_id}</p>
                  <p className="text-xs text-muted-foreground">
                    {credential.status}
                    {credential.created_at ? ` · issued ${formatTimestamp(credential.created_at)}` : ""}
                    {credential.last_used_at ? ` · used ${formatTimestamp(credential.last_used_at)}` : ""}
                  </p>
                </div>
                <Button
                  size="xs"
                  variant="outline"
                  disabled={revoking || credential.status !== "active"}
                  onClick={() => onRevokeCredential(credential.token_id)}
                >
                  Revoke
                </Button>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted-foreground">
          Metadata only — the secret is shown once at issue time and cannot be retrieved.
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">Recent values</p>
        {valuesQuery.isLoading ? (
          <Skeleton className="h-16 w-full rounded-lg" />
        ) : valuesQuery.error ? (
          <p className="text-xs text-rose-300">
            {alertsErrorMessage(valuesQuery.error, "Could not load values.")}
          </p>
        ) : values.length === 0 ? (
          <p className="text-sm text-muted-foreground">No values received yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {values.map((value) => (
              <li key={value.value_id} className="rounded-lg border border-border/50 p-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs">{value.instrument_key ?? "—"}</span>
                  <Badge variant={value.status === "accepted" ? "secondary" : "destructive"}>
                    {value.status}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  {value.event_time ? formatTimestamp(value.event_time) : "no event time"}
                  {value.expires_at ? ` · expires ${formatTimestamp(value.expires_at)}` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted-foreground">
          A value can be retained but already expired — &quot;accepted&quot; and &quot;still
          usable&quot; are different questions.
        </p>
      </div>
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

  const runtimeView = readRuntimeAvailability(healthQuery.data.runtime);
  const runtime = healthQuery.data.runtime;

  return (
    <div className="flex flex-col gap-4">
      <Panel tone="subtle">
        <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
          Evaluation worker
        </p>
        {runtimeView.kind === "unknown" ? (
          <p className="mt-2 text-sm">
            <span className="font-medium">Unknown</span> ({runtimeView.reason}). {runtimeView.note}
          </p>
        ) : (
          <ul className="mt-2 flex flex-wrap gap-4 text-sm">
            <li>quarantined: {runtimeView.quarantined}</li>
            <li>failing subscriptions: {runtimeView.failedSubscriptions}</li>
            <li>
              freshness policy:{" "}
              {typeof runtime.ltp_freshness_enabled === "boolean"
                ? runtime.ltp_freshness_enabled
                  ? "on"
                  : "off"
                : "not reported"}
            </li>
            {runtime.last_health_at ? (
              <li>last health: {formatTimestamp(runtime.last_health_at) ?? "—"}</li>
            ) : null}
          </ul>
        )}
        {runtime.startup_error ? (
          <p className="mt-2 text-sm text-rose-300">startup error: {runtime.startup_error}</p>
        ) : null}
      </Panel>

      {runtimeView.kind === "available" ? (
        <>
          <Panel tone="subtle">
            <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
              Required tasks
            </p>
            {Object.keys(runtime.tasks ?? {}).length === 0 ? (
              <p className="mt-2 text-sm text-muted-foreground">
                The worker did not report task state. Treat liveness as unknown rather than healthy.
              </p>
            ) : (
              <ul className="mt-2 flex flex-col gap-2">
                {Object.entries(runtime.tasks ?? {}).map(([name, task]) => (
                  <li key={name} className="flex flex-wrap items-center gap-3 text-sm">
                    <span className="font-mono text-xs">{name}</span>
                    <Badge variant={task.alive ? "secondary" : "destructive"}>
                      {task.alive ? "alive" : "not alive"}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      restarts: {task.restarts}
                      {task.backoff_s ? ` · backoff ${task.backoff_s}s` : ""}
                      {task.last_exit_reason ? ` · last exit: ${task.last_exit_reason}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel tone="subtle">
            <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
              Freshness counters
            </p>
            <ul className="mt-2 flex flex-wrap gap-4 text-sm">
              {/* An absent counter is UNKNOWN, not zero: reporting 0 would
                  tell the operator nothing is stale, which is the opposite of
                  what an unreported counter means. */}
              <li>
                stale-tick instruments:{" "}
                {typeof runtime.stale_tick_instruments === "number"
                  ? runtime.stale_tick_instruments
                  : "unknown"}
              </li>
              <li>
                never-ticked instruments:{" "}
                {typeof runtime.never_ticked_instruments === "number"
                  ? runtime.never_ticked_instruments
                  : "unknown"}
              </li>
              {Object.entries(runtime.rejected_ticks ?? {}).map(([reason, count]) => (
                <li key={reason}>
                  rejected · {reason}: {count}
                </li>
              ))}
            </ul>
          </Panel>
        </>
      ) : null}
    </div>
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
