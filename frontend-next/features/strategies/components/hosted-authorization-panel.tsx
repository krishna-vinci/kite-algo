"use client";

/**
 * Who approves trades, and the exact authorization behind automatic trading.
 *
 * The panel never issues an authorization on its own: switching mode is one
 * owner click and issuing the grant is another, and the grant is only ever
 * created after the owner has read the account, version, environment and limits
 * it will be bound to. Revocation is described as what it is - it stops later
 * dispatch, it does not cancel an order a broker already holds.
 */

import { useMemo, useState } from "react";
import { KeyRoundIcon, Loader2Icon, ShieldCheckIcon, ShieldOffIcon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  LimitsFields,
  type LimitDraft,
  limitsDraftFromPolicy,
  limitsPayload,
} from "@/features/strategies/components/hosted-limits-fields";
import {
  useAdmissionPolicy,
  useAuthorization,
  useExecutionGrants,
  useIssueExecutionGrant,
  useRevokeExecutionGrant,
  useSaveAdmissionPolicy,
  useSetAuthorizationMode,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import {
  formatTimestamp,
  hostedErrorMessage,
  newIdempotencyKey,
} from "@/features/strategies/lib/format";
import {
  APPROVAL_BASED,
  AUTONOMOUS,
  authorizationModeExplanation,
  authorizationModeLabel,
  environmentLabel,
  supportedExecutionModes,
} from "@/features/strategies/lib/modes";
import type {
  ExecutionGrant,
  HostedStrategy,
  HostedStrategyOptions,
  HostedVersion,
  PolicySnapshot,
} from "@/lib/hosted-strategies/types";

function limitsSummary(snapshot: PolicySnapshot | null | undefined): string {
  const admission = snapshot?.admission;
  if (!admission) return "No capital limits are recorded.";
  const parts = [
    admission.allocation_inr === null ? null : `allocation ${admission.allocation_inr} INR`,
    admission.per_instrument_notional_inr === null
      ? null
      : `per instrument ${admission.per_instrument_notional_inr} INR`,
    admission.gross_notional_inr === null ? null : `gross ${admission.gross_notional_inr} INR`,
    admission.max_open_instruments === null ? null : `${admission.max_open_instruments} instruments`,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(", ") : "No capital limits are recorded.";
}

function GrantFacts({ grant }: Readonly<{ grant: ExecutionGrant }>) {
  return (
    <dl className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
      <div>
        <dt className="font-medium text-foreground">Version</dt>
        <dd>
          v{grant.version_number} · {grant.source_sha256.slice(0, 12)}…
        </dd>
      </div>
      <div>
        <dt className="font-medium text-foreground">Account and environment</dt>
        <dd>
          {grant.account_id} · {environmentLabel(grant.execution_environment)}
        </dd>
      </div>
      <div>
        <dt className="font-medium text-foreground">Limits in force</dt>
        <dd>{limitsSummary(grant.policy_snapshot)}</dd>
      </div>
      <div>
        <dt className="font-medium text-foreground">Issued</dt>
        <dd>
          {formatTimestamp(grant.issued_at)}
          {grant.expires_at ? ` · expires ${formatTimestamp(grant.expires_at)}` : " · no expiry"}
        </dd>
      </div>
    </dl>
  );
}

export function HostedAuthorizationPanel({
  strategy,
  versions,
  options,
}: Readonly<{
  strategy: HostedStrategy;
  versions: HostedVersion[];
  options: HostedStrategyOptions | undefined;
}>) {
  const strategyId = strategy.strategy_id;
  const authorizationQuery = useAuthorization(strategyId);
  const grantsQuery = useExecutionGrants(strategyId);
  const policyQuery = useAdmissionPolicy(strategyId);
  const setMode = useSetAuthorizationMode(strategyId);
  const issueGrant = useIssueExecutionGrant(strategyId);
  const revokeGrant = useRevokeExecutionGrant(strategyId);
  const savePolicy = useSaveAdmissionPolicy(strategyId);

  const status = authorizationQuery.data;
  const newestVersion = versions.length > 0 ? versions[versions.length - 1] : undefined;
  // Drafts are held separately from the server state they default from, so
  // nothing has to be copied into component state inside an effect.
  const [versionOverride, setVersionOverride] = useState<string | null>(null);
  const [environmentOverride, setEnvironmentOverride] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState("");
  const [limitsDraft, setLimitsDraft] = useState<LimitDraft | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [revokeReason, setRevokeReason] = useState("");

  const versionId = versionOverride ?? newestVersion?.version_id ?? "";
  const environment = environmentOverride ?? strategy.default_execution_mode;
  const limits =
    limitsDraft ??
    limitsDraftFromPolicy(
      (policyQuery.data as unknown as Record<string, unknown> | null) ?? null,
    );
  const selectedVersion = versions.find((version) => version.version_id === versionId) ?? newestVersion;
  // The version's own snapshot says whether it can propose a trade at all. A
  // version that cannot trade has nothing for anyone to approve, and the panel
  // says so instead of presenting the two lanes as if it did.
  const hasTradeCapability = Boolean(selectedVersion?.capabilities_snapshot?.trade);
  const mode = status?.authorization_mode ?? strategy.authorization_mode ?? APPROVAL_BASED;
  const grant = status?.active_grant ?? null;
  const grantUsable = Boolean(status?.grant_usable);
  const limitsEntered = Object.keys(limitsPayload(limits)).length > 0;

  const summary = useMemo(() => {
    const versionLabel = selectedVersion ? `v${selectedVersion.version}` : "an unselected version";
    const account = strategy.default_account_scope;
    const entered = limitsPayload(limits);
    const limitText =
      Object.keys(entered).length > 0
        ? Object.entries(entered)
            .map(([key, value]) => `${key.replace(/_inr$/, " (INR)").replace(/_/g, " ")} ${value}`)
            .join(", ")
        : "no limits yet";
    return `Authorizes ${versionLabel} of ${strategy.name} to trade automatically on ${account} in ${environmentLabel(
      environment,
    )}, inside: ${limitText}${expiresAt ? `, until ${expiresAt}` : ", until you revoke it"}.`;
  }, [environment, expiresAt, limits, selectedVersion, strategy.default_account_scope, strategy.name]);

  async function changeMode(next: string) {
    try {
      const result = await setMode.mutateAsync({
        mode: next === AUTONOMOUS ? AUTONOMOUS : APPROVAL_BASED,
        reason: "changed in the strategy page",
      });
      toast.success(
        result.changed
          ? next === AUTONOMOUS
            ? "Automatic trading selected. It still needs an authorization before anything is placed."
            : "Back to review-first. The previous authorization was superseded."
          : "No change needed.",
      );
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  async function authorize() {
    if (!selectedVersion) {
      toast.error("Register a version before authorizing automatic trading.");
      return;
    }
    if (!limitsEntered) {
      toast.error("Enter your own limits first: the authorization is bound to them.");
      return;
    }
    try {
      if (mode !== AUTONOMOUS) {
        await setMode.mutateAsync({ mode: AUTONOMOUS, reason: "required by the authorization" });
      }
      await savePolicy.mutateAsync(limitsPayload(limits));
      const created = await issueGrant.mutateAsync({
        idempotency_key: newIdempotencyKey("grant"),
        version_id: selectedVersion.version_id,
        execution_environment: environment,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      });
      toast.success(
        created.idempotent
          ? "This authorization already existed; the original record was returned."
          : "Authorization issued. You can revoke it here at any time.",
      );
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  async function confirmRevoke() {
    try {
      await revokeGrant.mutateAsync({ grant_id: grant?.grant_id ?? null, reason: revokeReason || null });
      toast.success(
        "Authorization revoked. Later trades are refused; an order the broker already holds is not cancelled.",
      );
      setRevoking(false);
      setRevokeReason("");
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  if (authorizationQuery.isLoading) {
    return <Skeleton className="h-40 w-full rounded-lg" />;
  }
  if (authorizationQuery.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the authorization</AlertTitle>
        <AlertDescription>{hostedErrorMessage(authorizationQuery.error)}</AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="grid gap-3 md:grid-cols-2">
        <button
          type="button"
          onClick={() => changeMode(APPROVAL_BASED)}
          aria-pressed={mode === APPROVAL_BASED}
          disabled={setMode.isPending}
          className={`rounded-lg border p-3 text-left text-sm transition ${
            mode === APPROVAL_BASED ? "border-primary/60 bg-primary/5" : "border-border/70 hover:bg-muted/40"
          }`}
        >
          <span className="font-semibold">{authorizationModeLabel(APPROVAL_BASED)}</span>
          <span className="mt-1 block text-xs text-muted-foreground">
            {authorizationModeExplanation(APPROVAL_BASED)}
          </span>
        </button>
        <button
          type="button"
          onClick={() => changeMode(AUTONOMOUS)}
          aria-pressed={mode === AUTONOMOUS}
          disabled={setMode.isPending}
          className={`rounded-lg border p-3 text-left text-sm transition ${
            mode === AUTONOMOUS ? "border-primary/60 bg-primary/5" : "border-border/70 hover:bg-muted/40"
          }`}
        >
          <span className="font-semibold">{authorizationModeLabel(AUTONOMOUS)}</span>
          <span className="mt-1 block text-xs text-muted-foreground">
            {authorizationModeExplanation(AUTONOMOUS)}
          </span>
        </button>
      </div>

      {!status?.policy_concrete ? (
        <Alert>
          <AlertTitle>No capital limits are recorded for this strategy</AlertTitle>
          <AlertDescription>
            An authorization without limits would not enforce anything, so the platform refuses
            automatic trading until you record your own numbers below.
          </AlertDescription>
        </Alert>
      ) : null}

      {selectedVersion && !hasTradeCapability ? (
        <p className="text-xs text-muted-foreground" data-testid="authorization-no-trade-capability">
          The selected version has no &quot;Propose trades&quot; permission, so it never asks anyone to
          approve a trade and nothing can be placed for it. Your mode choice and limits are still
          recorded here for the strategy.
        </p>
      ) : null}

      {grant ? (
        <div className="flex flex-col gap-3 rounded-lg border border-border/70 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="flex items-center gap-2 text-sm font-semibold">
              <ShieldCheckIcon className="size-4" aria-hidden />
              {grantUsable
                ? "Authorization active"
                : `Authorization ${grant.status} (not usable)`}
            </span>
            {grantUsable ? (
              revoking ? (
                <div className="flex items-center gap-2">
                  <Button variant="ghost" size="sm" onClick={() => setRevoking(false)}>
                    Cancel
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={confirmRevoke}
                    disabled={revokeGrant.isPending}
                  >
                    {revokeGrant.isPending ? (
                      <Loader2Icon className="size-4 animate-spin" aria-hidden />
                    ) : (
                      <ShieldOffIcon className="size-4" aria-hidden />
                    )}
                    Confirm revoke
                  </Button>
                </div>
              ) : (
                <Button variant="outline" size="sm" onClick={() => setRevoking(true)}>
                  <ShieldOffIcon className="size-4" aria-hidden />
                  Revoke
                </Button>
              )
            ) : null}
          </div>
          <GrantFacts grant={grant} />
          {revoking ? (
            <Input
              aria-label="Revocation reason"
              placeholder="Reason (optional)"
              value={revokeReason}
              onChange={(event) => setRevokeReason(event.target.value)}
            />
          ) : null}
          {(status?.blocking_reasons.length ?? 0) > 0 ? (
            <ul className="list-disc pl-4 text-xs text-muted-foreground">
              {status?.blocking_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
          <p className="text-xs text-muted-foreground">
            Revoking stops later trades. It does not cancel an order the broker already holds.
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-4 rounded-lg border border-border/70 p-4">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <KeyRoundIcon className="size-4" aria-hidden />
            Issue an authorization
          </p>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="grid gap-1.5">
              <Label htmlFor="grant-version">Version</Label>
              <Select value={selectedVersion?.version_id ?? ""} onValueChange={setVersionOverride}>
                <SelectTrigger id="grant-version" className="w-full">
                  <SelectValue placeholder="Select a version" />
                </SelectTrigger>
                <SelectContent>
                  {versions.map((version) => (
                    <SelectItem key={version.version_id} value={version.version_id}>
                      v{version.version} · {version.source_sha256.slice(0, 8)}…
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="grant-environment">Environment</Label>
              <Select value={environment} onValueChange={setEnvironmentOverride}>
                <SelectTrigger id="grant-environment" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {supportedExecutionModes(options).map((modeCode) => (
                    <SelectItem key={modeCode} value={modeCode}>
                      {environmentLabel(modeCode)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5 md:col-span-2">
              <Label htmlFor="grant-expiry">Expires (optional)</Label>
              <Input
                id="grant-expiry"
                type="datetime-local"
                value={expiresAt}
                onChange={(event) => setExpiresAt(event.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Leave empty for “until revoked or until the version, account or limits change”.
              </p>
            </div>
          </div>
          <LimitsFields limits={limits} onChange={setLimitsDraft} idPrefix="auth-limit" />
          <p className="text-sm" data-testid="grant-summary">
            {summary}
          </p>
          <div className="flex items-center gap-3">
            <Button
              onClick={authorize}
              disabled={issueGrant.isPending || savePolicy.isPending || setMode.isPending}
            >
              {issueGrant.isPending || savePolicy.isPending || setMode.isPending ? (
                <Loader2Icon className="size-4 animate-spin" aria-hidden />
              ) : (
                <ShieldCheckIcon className="size-4" aria-hidden />
              )}
              Authorize automatic trading
            </Button>
            {!limitsEntered ? (
              <span className="text-xs text-muted-foreground">
                Fill in at least one limit; nothing is assumed for you.
              </span>
            ) : null}
          </div>
        </div>
      )}

      {(grantsQuery.data?.length ?? 0) > 0 ? (
        <div className="flex flex-col gap-2">
          <p className="text-sm font-medium">Authorization history</p>
          <ul className="flex flex-col gap-2" data-testid="grant-history">
            {grantsQuery.data?.slice(0, 5).map((row) => (
              <li
                key={row.grant_id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/60 px-3 py-2 text-xs text-muted-foreground"
              >
                <span>
                  v{row.version_number} · {environmentLabel(row.execution_environment)} · {row.status}
                  {row.expires_at ? ` · expires ${formatTimestamp(row.expires_at)}` : ""}
                </span>
                <span>
                  {row.revoked_at
                    ? `revoked ${formatTimestamp(row.revoked_at)}`
                    : row.superseded_at
                      ? `superseded ${formatTimestamp(row.superseded_at)}`
                      : formatTimestamp(row.issued_at)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
