"use client";

/**
 * The one-screen hosted-strategy composer.
 *
 * The page is one scrollable form, not a wizard: source, permissions, account
 * and environment, approval, inputs, timing, then the compact meaning of the
 * whole thing above the single primary action.
 *
 * Two properties this file is careful about:
 *
 * 1. **What is submitted is what the operator entered.** Launch parameters come
 *    from the parameter values on this page (validated here and again on the
 *    server); the platform does not stamp extra keys into them, because a strict
 *    (`additionalProperties: false`) schema would reject that and the operator's
 *    parameter names are theirs.
 * 2. **A partial creation resumes, it does not restart.** Each completed step
 *    records the exact inputs it was bound to. Editing the code, schema or
 *    permissions after a version exists registers a NEW version and mints new
 *    request keys; changing the name, account, environment or timing after the
 *    strategy exists is refused with an explicit "start a new strategy" instead
 *    of silently adopting different defaults.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2Icon,
  ChevronDownIcon,
  ChevronRightIcon,
  FileCode2Icon,
  Loader2Icon,
  PlayIcon,
  UploadIcon,
} from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { AddParameterField, ReadinessView, type ReadinessState } from "@/features/strategies/components/hosted-composer-parts";
import {
  EMPTY_LIMITS,
  LimitsFields,
  type LimitDraft,
  limitsPayload,
} from "@/features/strategies/components/hosted-limits-fields";
import {
  HostedParamInputs,
  ParamValueInput,
  useHostedParamValues,
} from "@/features/strategies/components/hosted-params-editor";
import {
  knownTimezone,
  TIMEZONES,
  WEEKDAYS,
} from "@/features/strategies/components/hosted-schedule-panel";
import { hostedKeys } from "@/features/strategies/hooks/keys";
import {
  useHostedOptions,
  useHostedStrategies,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import { hostedErrorMessage, newIdempotencyKey } from "@/features/strategies/lib/format";
import {
  APPROVAL_BASED,
  AUTONOMOUS,
  LIVE_MODE,
  authorizationModeExplanation,
  authorizationModeLabel,
  environmentLabel,
  isModeSupported,
  liveLaneSummary,
  liveRequiresOwnerApproval,
  liveModeSupported,
  preferredCreateMode,
  supportedExecutionModes,
} from "@/features/strategies/lib/modes";
import {
  type SchemaField,
  buildParametersSchema,
  fieldConstraintSummary,
} from "@/features/strategies/lib/schema";
import { HOSTED_STARTER_SOURCE } from "@/features/strategies/lib/starter";
import { usePlatformStatus } from "@/features/platform/hooks/use-platform-queries";
import { ApiClientError } from "@/lib/api/client";
import {
  checkSourceReadiness,
  createHostedStrategy,
  createHostedVersion,
  fetchHostedVersions,
  issueExecutionGrant,
  runHostedStrategy,
  saveAdmissionPolicy,
  saveHostedSchedule,
  setAuthorizationMode,
} from "@/lib/hosted-strategies/api";
import type {
  CreateHostedVersionPayload,
  HostedStrategy,
  HostedVersion,
  ScheduleKind,
  SourceReadiness,
} from "@/lib/hosted-strategies/types";

/** "Run now" launches immediately; "On a schedule" saves a recurring start time. */
type RunStyle = "now" | "schedule";

type ScheduleDraft = {
  scheduleKind: ScheduleKind;
  atTime: string;
  weekday: number;
  dayOfMonth: number;
  calendarDates: string;
  timezone: string;
};

const DEFAULT_SCHEDULE_DRAFT: ScheduleDraft = {
  scheduleKind: "daily",
  atTime: "09:30",
  weekday: 1,
  dayOfMonth: 1,
  calendarDates: "",
  timezone: "Asia/Kolkata",
};

/** The server's own bound: `SourceReadinessRequest.source` is 256 KiB max. */
const MAX_SOURCE_BYTES = 256 * 1024;
const READINESS_DEBOUNCE_MS = 700;

/**
 * What each completed write step was bound to. A step is reused ONLY when the
 * inputs that determine it are unchanged.
 */
type CompletedSteps = {
  strategyId?: string;
  strategyFingerprint?: string;
  versionId?: string;
  versionFingerprint?: string;
  policyFingerprint?: string;
  serverMode?: string;
  /**
   * Set while a mode write is in flight and cleared only when its answer comes
   * back. If the answer is lost the server may already be in that mode, so the
   * page must not treat the mode as known and a later review-first choice has to
   * write it again.
   */
  modeUnconfirmed?: true;
  grantKey?: string;
  grantFingerprint?: string;
  launchKey?: string;
  launchFingerprint?: string;
};

function byteLength(value: string): number {
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(value).length;
  return value.length;
}

/** Problems the operator must fix: real refusals, never "unknown". */
function blockingIssues(result: SourceReadiness): string[] {
  const issues: string[] = [];
  if (!result.entrypoint.compatible && result.entrypoint.detail) {
    issues.push(
      `${result.entrypoint.detail}${result.entrypoint.remediation ? ` — ${result.entrypoint.remediation}` : ""}`,
    );
  }
  for (const check of result.checks) {
    if (check.status !== "blocked") continue;
    issues.push(`${check.detail}${check.remediation ? ` — ${check.remediation}` : ""}`);
  }
  for (const name of result.imports.missing) {
    issues.push(
      `${name} is not installed in the strategy runner. Remove the import or use one of the supported packages.`,
    );
  }
  const unique: string[] = [];
  for (const issue of issues) {
    if (unique.some((existing) => existing.includes(issue) || issue.includes(existing))) continue;
    unique.push(issue);
  }
  return unique.slice(0, 6);
}

/**
 * What cannot be certified. The server returns `ready` while an individual
 * check is `unknown` (dynamic imports, guarded optional imports), so the UI
 * must not turn that into a certified "Ready to run".
 */
function uncertaintyReasons(result: SourceReadiness): string[] {
  const reasons = result.checks
    .filter((check) => check.status === "unknown")
    .map((check) => `${check.detail}${check.remediation ? ` — ${check.remediation}` : ""}`);
  if (result.imports.dynamic) {
    reasons.push(
      "The source imports modules dynamically, so the static check cannot confirm what it will load.",
    );
  }
  for (const name of result.imports.optional_missing) {
    reasons.push(`${name} is imported behind an ImportError guard and is not in the runner.`);
  }
  return [...new Set(reasons)];
}

/** Fold one server answer into the state the page renders and gates on. */
function classifyReadiness(source: string, result: SourceReadiness): ReadinessState {
  const blocked = blockingIssues(result);
  if (blocked.length > 0 || result.status !== "ready") {
    return { kind: "blocked", source, result, reasons: blocked };
  }
  const reasons = uncertaintyReasons(result);
  if (reasons.length > 0) return { kind: "unknown", source, result, reasons };
  return { kind: "ready", source, result };
}

function strategyFingerprintOf(payload: Record<string, unknown>): string {
  return JSON.stringify([
    payload.name,
    payload.description ?? null,
    payload.execution_mode,
    payload.job_kind,
    payload.account_scope,
    payload.max_duration_s,
    payload.progress_deadline_s,
    payload.stale_exit_policy,
  ]);
}

/**
 * A stable string for a schema, whatever order a store handed the keys back in.
 * Postgres-backed JSON columns do not promise the order the browser wrote, so a
 * byte comparison would refuse a revision that really is the same one.
 */
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      a < b ? -1 : a > b ? 1 : 0,
    );
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

export function HostedStrategyComposer() {
  const router = useRouter();
  const client = useQueryClient();
  const optionsQuery = useHostedOptions();
  const strategiesQuery = useHostedStrategies();
  const options = optionsQuery.data;

  const [source, setSource] = useState("");
  // A refused INPUT ACTION (a rejected drop, an oversized paste or file), never
  // a problem with the strategy. It leaves the source exactly as it was, so it
  // is reported beside the editor and does NOT refuse the launch: the readiness
  // answer for the current source is what decides whether it can run.
  const [sourceNotice, setSourceNotice] = useState<string | null>(null);
  const [readiness, setReadiness] = useState<ReadinessState>({ kind: "idle" });
  const [acceptUnknown, setAcceptUnknown] = useState(false);
  const readinessRequest = useRef(0);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // Server-authorized defaults are DERIVED (never copied into state during a
  // render or an effect): an explicit operator choice wins, and the server's
  // first offer is used until they make one.
  const [accountChoice, setAccountChoice] = useState<string | null>(null);
  const [environmentChoice, setEnvironmentChoice] = useState<string | null>(null);
  const [jobKindChoice, setJobKindChoice] = useState<string | null>(null);
  const [policyChoice, setPolicyChoice] = useState<string | null>(null);
  const [maxDuration, setMaxDuration] = useState("21600");
  const [progressDeadline, setProgressDeadline] = useState("600");

  // Sensible defaults: a strategy can read data and propose trades from the
  // start, and review-first (below) means nothing trades without a decision.
  const [permissions, setPermissions] = useState({ data: true, trade: true, notify: false });
  const [authorization, setAuthorization] = useState(APPROVAL_BASED);
  const [limits, setLimits] = useState<LimitDraft>(EMPTY_LIMITS);

  const [fields, setFields] = useState<SchemaField[]>([]);
  const [advancedSchema, setAdvancedSchema] = useState(false);
  const [schemaText, setSchemaText] = useState("");

  // Everything with a sensible default (permissions, limits/authorization,
  // job kind, stale-exit policy, duration/deadline, description) lives behind
  // this disclosure so the primary flow is name -> code -> params -> mode ->
  // run style -> Start.
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const [runStyle, setRunStyle] = useState<RunStyle>("now");
  const [scheduleDraft, setScheduleDraft] = useState<ScheduleDraft>(DEFAULT_SCHEDULE_DRAFT);
  const platformStatusQuery = usePlatformStatus();
  // Only an explicit "live is off" answer disables the option; a loading or
  // failed platform-status query never blocks a mode the server itself offers.
  const liveDisabledByPlatform = platformStatusQuery.data ? !platformStatusQuery.data.live.enabled : false;

  const [steps, setSteps] = useState<CompletedSteps>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [startOver, setStartOver] = useState(false);

  const accountScope = accountChoice ?? options?.account_scopes[0] ?? "";
  const environment = environmentChoice ?? preferredCreateMode(options?.execution_modes ?? []);
  const jobKind =
    jobKindChoice && (options?.job_kinds ?? []).includes(jobKindChoice)
      ? jobKindChoice
      : options?.job_kinds?.[0] ?? "finite";
  const staleExitPolicy =
    policyChoice && (options?.stale_exit_policies ?? []).includes(policyChoice)
      ? policyChoice
      : options?.stale_exit_policies?.[0] ?? "none";

  const requestReadiness = useCallback(
    (snapshot: string) => {
      const requestId = readinessRequest.current + 1;
      readinessRequest.current = requestId;
      if (!snapshot.trim()) {
        setReadiness({ kind: "idle" });
        return;
      }
      setReadiness({ kind: "checking", source: snapshot });
      setTimeout(async () => {
        try {
          const result = await checkSourceReadiness(snapshot);
          // Only the newest answer, and only for the source it was asked about.
          if (readinessRequest.current !== requestId) return;
          setReadiness(classifyReadiness(snapshot, result));
        } catch (error) {
          if (readinessRequest.current !== requestId) return;
          setReadiness({ kind: "error", source: snapshot, message: hostedErrorMessage(error) });
        }
      }, READINESS_DEBOUNCE_MS);
    },
    [],
  );

  function onSourceChange(next: string) {
    if (byteLength(next) > MAX_SOURCE_BYTES) {
      setSourceNotice(
        `That source is larger than the platform's ${MAX_SOURCE_BYTES / 1024} KB limit. Nothing was changed.`,
      );
      return;
    }
    setSourceNotice(null);
    setSource(next);
    setAcceptUnknown(false);
    requestReadiness(next);
  }

  const schemaFromFields = useMemo(() => buildParametersSchema(fields), [fields]);
  const versionSchema = useMemo(() => {
    if (!advancedSchema) return { ok: true as const, value: schemaFromFields };
    try {
      const parsed = JSON.parse(schemaText);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { ok: false as const, error: "The schema must be a JSON object." };
      }
      return { ok: true as const, value: parsed as Record<string, unknown> };
    } catch {
      return { ok: false as const, error: "The schema is not valid JSON." };
    }
  }, [advancedSchema, schemaFromFields, schemaText]);

  const paramValues = useHostedParamValues(versionSchema.ok ? versionSchema.value : undefined);

  const readinessStale =
    readiness.kind !== "idle" && "source" in readiness && readiness.source !== source;
  const readinessBlocks =
    readiness.kind === "idle" ||
    readiness.kind === "checking" ||
    readiness.kind === "error" ||
    readiness.kind === "blocked" ||
    (readiness.kind === "unknown" && !acceptUnknown);

  const trades = permissions.trade;
  const desiredMode = trades ? authorization : APPROVAL_BASED;

  const summary = useMemo(() => {
    const label = name.trim() || "this strategy";
    const parts = [
      `Runs ${label} on ${environmentLabel(environment)} as ${accountScope || "an authorized account"}`,
      permissions.data ? "reads market data" : null,
      trades ? "can propose trades" : "cannot trade",
      permissions.notify ? "sends notifications" : null,
      trades
        ? desiredMode === AUTONOMOUS
          ? "and trades automatically inside the limits you set"
          : "and waits for your approval on every trade"
        : null,
    ].filter(Boolean);
    return `${parts.join(", ")}.`;
  }, [accountScope, desiredMode, environment, name, permissions, trades]);

  function validate(): string | null {
    if (!source.trim()) return "Paste or choose the Python source first.";
    // `sourceNotice` is deliberately NOT checked here: it reports an input
    // action that was refused while leaving the source untouched, so refusing
    // the launch over it would block a strategy that the readiness check has
    // already answered for.
    if (readinessStale || readiness.kind === "checking") {
      return "Waiting for the readiness check on the current source.";
    }
    if (readiness.kind === "blocked") {
      return "Fix the source problems above before creating the strategy.";
    }
    if (readiness.kind === "unknown" && !acceptUnknown) {
      return "This source cannot be certified ready. Read the notes and tick the acknowledgement to continue.";
    }
    if (readiness.kind !== "ready" && readiness.kind !== "unknown") {
      return "The readiness check has not answered for this source yet.";
    }
    if (!name.trim()) return "Give the strategy a name.";
    if (!accountScope) return "No authorized account is available on this deployment.";
    if (!options || !isModeSupported(options, environment)) {
      return `This deployment does not offer ${environmentLabel(environment)} right now.`;
    }
    if (environment === LIVE_MODE && liveDisabledByPlatform) {
      return "This deployment's platform status reports live trading is off right now.";
    }
    if (!versionSchema.ok) return versionSchema.error;
    if (trades && desiredMode === AUTONOMOUS && Object.keys(limitsPayload(limits)).length === 0) {
      return "Automatic trading needs your own limits: fill in at least the allocation.";
    }
    if (!trades && !permissions.data && !permissions.notify) {
      return "Choose at least one permission, or the strategy can do nothing.";
    }
    if (!paramValues.valid) {
      return `Check the parameters: ${Object.values(paramValues.errors)[0] ?? "a value is missing."}`;
    }
    if (runStyle === "schedule") {
      if (!knownTimezone(scheduleDraft.timezone)) {
        return `"${scheduleDraft.timezone}" is not a timezone this browser knows.`;
      }
      if (
        scheduleDraft.scheduleKind === "calendar" &&
        scheduleDraft.calendarDates.split(",").map((entry) => entry.trim()).filter(Boolean).length === 0
      ) {
        return "Add at least one calendar date for the schedule.";
      }
    }
    return null;
  }

  /**
   * The recovered identity for an existing strategy, or `null` when it is not
   * the same strategy this page is configuring.
   *
   * Adoption is only ever for the SAME strategy. Every default this page is
   * configuring is compared - name, description, account, environment, run
   * kind, timing and the stale-exit policy - because a matching name with any
   * different default is a different strategy, and adopting it would silently
   * run different settings than the page shows.
   */
  function adoptionFor(
    row: HostedStrategy,
    expectedFingerprint: string,
  ): Pick<CompletedSteps, "strategyId" | "strategyFingerprint" | "modeUnconfirmed"> | null {
    const rowFingerprint = strategyFingerprintOf({
      name: row.name,
      description: row.description ?? null,
      execution_mode: row.default_execution_mode,
      job_kind: row.default_job_kind,
      account_scope: row.default_account_scope,
      max_duration_s: row.max_duration_s,
      progress_deadline_s: row.progress_deadline_s,
      stale_exit_policy: row.stale_exit_policy,
    });
    if (rowFingerprint !== expectedFingerprint) {
      setStartOver(true);
      setFailure(
        `A strategy named "${row.name}" already exists with different name, account, environment or ` +
          `timing settings (it is on ${row.default_account_scope} in ${environmentLabel(
            row.default_execution_mode,
          )}), which is not what this page is configured for. Nothing was adopted or changed: start a ` +
          `new strategy, or put these settings back to match it.`,
      );
      return null;
    }
    setStartOver(false);
    return {
      strategyId: row.strategy_id,
      strategyFingerprint: rowFingerprint,
      modeUnconfirmed: true,
    };
  }

  function clearEverything() {
    setSteps({});
    setFailure(null);
    setNotice(null);
    setStartOver(false);
  }

  /**
   * The revision a lost response left behind, or `null`.
   *
   * Only an exact match on the source, the schema and the three permissions is
   * adopted: a different revision would run code or hold capabilities this page
   * does not show.
   */
  async function matchingRegisteredVersion(
    strategyId: string,
    payload: CreateHostedVersionPayload,
  ): Promise<HostedVersion | null> {
    try {
      const { versions } = await fetchHostedVersions(strategyId);
      const wanted = payload.capabilities ?? {};
      const wantedSchema = canonicalJson(payload.parameters_schema ?? {});
      return (
        versions.find(
          (row) =>
            row.source === payload.source &&
            canonicalJson(row.parameters_schema ?? {}) === wantedSchema &&
            Boolean(row.capabilities_snapshot?.data) === Boolean(wanted.data) &&
            Boolean(row.capabilities_snapshot?.trade) === Boolean(wanted.trade) &&
            Boolean(row.capabilities_snapshot?.notify) === Boolean(wanted.notify),
        ) ?? null
      );
    } catch {
      // The reconciliation itself failed: the original failure is the one the
      // operator needs to see.
      return null;
    }
  }

  async function handleFile(file: File | null | undefined) {
    if (!file) return;
    const looksPython = file.name.toLowerCase().endsWith(".py") || file.type === "text/x-python";
    if (!looksPython) {
      setSourceNotice("Choose a Python file (.py). The current source was kept.");
      return;
    }
    if (file.size > MAX_SOURCE_BYTES) {
      setSourceNotice(
        `That file is ${Math.ceil(file.size / 1024)} KB; the platform stores at most ${
          MAX_SOURCE_BYTES / 1024
        } KB. The current source was kept.`,
      );
      return;
    }
    try {
      onSourceChange(await file.text());
    } catch {
      setSourceNotice("That file could not be read. The current source was kept.");
    }
  }

  async function submit() {
    const invalid = validate();
    if (invalid) {
      toast.error(invalid);
      return;
    }
    setFailure(null);
    setNotice(null);
    const next: CompletedSteps = { ...steps };
    try {
      const strategyPayload = {
        name: name.trim(),
        description: description.trim() || null,
        execution_mode: environment,
        job_kind: jobKind,
        account_scope: accountScope,
        max_duration_s: Number(maxDuration) || 21600,
        progress_deadline_s: Number(progressDeadline) || 600,
        stale_exit_policy: staleExitPolicy,
      };
      const strategyKey = strategyFingerprintOf(strategyPayload);
      const versionPayload = {
        source,
        parameters_schema: versionSchema.ok ? versionSchema.value : undefined,
        capabilities: { ...permissions },
      };
      const versionKey = JSON.stringify(versionPayload);

      if (next.strategyId && next.strategyFingerprint !== strategyKey) {
        setStartOver(true);
        setFailure(
          "This strategy already exists with different name, account, environment or timing settings. Those are fixed on the created strategy — start a new strategy, or put the values back.",
        );
        return;
      }

      if (!next.strategyId) {
        setBusy("Creating the strategy…");
        try {
          const created = await createHostedStrategy(strategyPayload);
          next.strategyId = created.strategy_id;
          next.strategyFingerprint = strategyKey;
          // A create leaves the strategy on its review-first default. Recording
          // that here (before the mode write below can fail) is what makes a
          // later retry still know the server was not left autonomous.
          next.serverMode = APPROVAL_BASED;
        } catch (error) {
          // Either the name is taken, or the response was lost after the row was
          // written. Both are recovered the same way, and only for a strategy
          // whose defaults are ALL the ones this page is showing.
          // Read the REFRESHED list: the value captured in this closure is the
          // one from before the request that just failed.
          const refreshed = await strategiesQuery.refetch();
          const rows = refreshed.data?.strategies ?? strategiesQuery.data?.strategies ?? [];
          const existing = rows.find((row) => row.name === name.trim());
          const adopted = existing ? adoptionFor(existing, strategyKey) : null;
          if (adopted) {
            next.strategyId = adopted.strategyId;
            next.strategyFingerprint = adopted.strategyFingerprint;
            setNotice(
              `Continuing with the existing strategy "${existing?.name}" instead of creating a second one.`,
            );
            setSteps({ ...next });
          } else if (existing) {
            // A strategy with this name exists but is not this one: the refusal
            // above names the difference and nothing is adopted or changed.
            return;
          } else if (error instanceof ApiClientError && error.status === 409) {
            return;
          } else {
            throw error;
          }
        }
        if (!next.strategyId) return;
        setSteps({ ...next });
        void client.invalidateQueries({ queryKey: hostedKeys.strategies() });
      }

      if (next.versionId && next.versionFingerprint !== versionKey) {
        // The code, its schema or its permissions changed after the version was
        // registered: this becomes a NEW immutable version, and every request
        // key minted for the old one is dropped.
        next.versionId = undefined;
        next.versionFingerprint = undefined;
        next.grantKey = undefined;
        next.grantFingerprint = undefined;
        next.launchKey = undefined;
        next.launchFingerprint = undefined;
        setNotice("Your edits are being registered as a new version of this strategy.");
        setSteps({ ...next });
      }
      if (!next.versionId) {
        setBusy("Registering the source version…");
        try {
          const version = await createHostedVersion(next.strategyId as string, versionPayload);
          next.versionId = version.version_id;
          next.versionFingerprint = versionKey;
          setSteps({ ...next });
        } catch (error) {
          // The response can be lost after the revision was written. Only an
          // EXACT match on what this page submitted is adopted (same source,
          // same schema, same permissions); any other revision is a different
          // one and the failure is reported instead of being run silently.
          const matched = await matchingRegisteredVersion(next.strategyId as string, versionPayload);
          if (!matched) throw error;
          next.versionId = matched.version_id;
          next.versionFingerprint = versionKey;
          setNotice(
            `Version v${matched.version} registered by the previous attempt is being reused instead of registering a second copy.`,
          );
          setSteps({ ...next });
        }
      }

      // The owner's own numbers are recorded in BOTH modes: admission checks
      // them before any trade, whoever approves it. Nothing is invented when
      // they were left empty - and once a policy has been recorded here, an
      // emptied field is sent as an explicit clear rather than silently keeping
      // the old number.
      if (trades) {
        const decidedLimits = limitsPayload(limits);
        const limitsKey = JSON.stringify(decidedLimits);
        const alreadyRecorded = next.policyFingerprint !== undefined;
        if (
          (Object.keys(decidedLimits).length > 0 || alreadyRecorded) &&
          next.policyFingerprint !== limitsKey
        ) {
          setBusy("Recording your limits…");
          await saveAdmissionPolicy(next.strategyId as string, decidedLimits);
          next.policyFingerprint = limitsKey;
          setSteps({ ...next });
        }
      }

      if (trades && desiredMode === AUTONOMOUS) {
        if (next.serverMode !== AUTONOMOUS || next.modeUnconfirmed) {
          setBusy("Recording the authorization mode…");
          // The write may reach the server even when its answer never comes
          // back, so the mode stays UNCONFIRMED until the response lands.
          next.modeUnconfirmed = true;
          setSteps({ ...next });
          await setAuthorizationMode(next.strategyId as string, {
            mode: AUTONOMOUS,
            reason: "chosen while creating the strategy",
          });
          next.serverMode = AUTONOMOUS;
          next.modeUnconfirmed = undefined;
          setSteps({ ...next });
        }
        const limitsKey = JSON.stringify(limitsPayload(limits));
        if (!next.grantKey || next.grantFingerprint !== limitsKey) {
          next.grantKey = newIdempotencyKey("grant");
          next.grantFingerprint = limitsKey;
          setSteps({ ...next });
        }
        setBusy("Issuing the authorization…");
        await issueExecutionGrant(next.strategyId as string, {
          idempotency_key: next.grantKey,
          version_id: next.versionId as string,
          execution_environment: environment,
        });
      } else if (next.serverMode !== APPROVAL_BASED || next.modeUnconfirmed) {
        // Review-first is written whenever the server may not already be on it:
        //
        // * the page knows the server is autonomous (an explicit switch back);
        // * the mode is unknown - a strategy adopted after a lost create
        //   response, or one whose creation step itself failed before any mode
        //   was recorded (adoption never reads the server's mode);
        // * a mode write was sent and its answer was never seen, so the server
        //   may be armed even though the page cannot confirm it.
        //
        // A strategy this session created and never switched stays untouched:
        // its ``serverMode`` is review-first, so this is a no-op.
        setBusy("Switching back to review-first…");
        await setAuthorizationMode(next.strategyId as string, {
          mode: APPROVAL_BASED,
          reason: "review-first chosen while creating the strategy",
        });
        next.serverMode = APPROVAL_BASED;
        next.modeUnconfirmed = undefined;
        next.grantKey = undefined;
        next.grantFingerprint = undefined;
        setSteps({ ...next });
      }

      if (runStyle === "schedule") {
        setBusy("Saving the schedule…");
        await saveHostedSchedule(next.strategyId as string, {
          version_id: next.versionId as string,
          execution_mode: environment,
          job_kind: jobKind,
          params: paramValues.value,
          schedule_kind: scheduleDraft.scheduleKind,
          at_time: scheduleDraft.atTime,
          weekday: scheduleDraft.scheduleKind === "weekly" ? scheduleDraft.weekday : null,
          day_of_month: scheduleDraft.scheduleKind === "monthly" ? scheduleDraft.dayOfMonth : null,
          calendar_dates:
            scheduleDraft.scheduleKind === "calendar"
              ? scheduleDraft.calendarDates
                  .split(",")
                  .map((entry) => entry.trim())
                  .filter(Boolean)
              : null,
          timezone: scheduleDraft.timezone,
          enabled: true,
        });
        void client.invalidateQueries({ queryKey: hostedKeys.strategies() });
        void client.invalidateQueries({ queryKey: hostedKeys.schedule(next.strategyId as string) });
        toast.success("Schedule saved. The strategy starts at its next occurrence.");
        router.push(`/strategies/${next.strategyId}`);
      } else {
        const launchKey = JSON.stringify([next.versionId, environment, jobKind, paramValues.value]);
        if (!next.launchKey || next.launchFingerprint !== launchKey) {
          next.launchKey = newIdempotencyKey("launch");
          next.launchFingerprint = launchKey;
          setSteps({ ...next });
        }
        setBusy("Queueing the first attempt…");
        await runHostedStrategy(next.strategyId as string, {
          version_id: next.versionId as string,
          params: paramValues.value,
          execution_mode: environment,
          job_kind: jobKind,
          idempotency_key: next.launchKey,
        });
        void client.invalidateQueries({ queryKey: hostedKeys.strategies() });
        void client.invalidateQueries({ queryKey: hostedKeys.jobs(next.strategyId as string) });
        toast.success(
          environment === LIVE_MODE && trades && desiredMode === AUTONOMOUS
            ? "Queued. Live trades run under the authorization you just issued."
            : environment === LIVE_MODE
              ? "Queued. The live attempt still needs your approval of a plan before anything is sent."
              : "Queued. The process has not started yet.",
        );
        router.push(`/strategies/${next.strategyId}`);
      }
    } catch (error) {
      setFailure(hostedErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <SectionLabel
        eyebrow="New hosted strategy"
        title="Paste your Python, choose what it may do, then run it"
        description="Source, inputs, account, permissions and approval on one page. Nothing is sent until the end."
      />
      {optionsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load this deployment&apos;s options</AlertTitle>
          <AlertDescription>
            {hostedErrorMessage(optionsQuery.error)} Account, environment and policy choices would be
            guesses, so creating a strategy stays disabled until the server answers.
          </AlertDescription>
        </Alert>
      ) : null}
      <Panel title="Name">
        <div className="grid gap-4 md:grid-cols-2">
          <div className="grid gap-1.5">
            <Label htmlFor="composer-name">Name</Label>
            <Input
              id="composer-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="NIFTY opening range"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="composer-scope">Account</Label>
            <Select value={accountScope} onValueChange={setAccountChoice}>
              <SelectTrigger id="composer-scope" className="w-full">
                <SelectValue placeholder="Select an authorized account" />
              </SelectTrigger>
              <SelectContent>
                {(options?.account_scopes ?? []).map((scope) => (
                  <SelectItem key={scope} value={scope}>
                    {scope}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
      </Panel>
      <Panel title="Python source">
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onSourceChange(HOSTED_STARTER_SOURCE)}
            >
              <FileCode2Icon className="size-4" aria-hidden />
              Insert starter
            </Button>
            <Label
              htmlFor="composer-file"
              className="inline-flex h-9 cursor-pointer items-center gap-2 rounded-md border border-border/70 bg-background px-3 text-sm font-medium hover:bg-muted/50 focus-within:ring-2 focus-within:ring-ring"
            >
              <UploadIcon className="size-4" aria-hidden />
              Choose a .py file
            </Label>
            <input
              id="composer-file"
              type="file"
              accept=".py,text/x-python"
              className="sr-only"
              onChange={(event) => {
                void handleFile(event.target.files?.[0]);
                event.target.value = "";
              }}
            />
            <span className="text-xs text-muted-foreground">
              or drop a .py file here · up to {MAX_SOURCE_BYTES / 1024} KB
            </span>
          </div>
          <Textarea
            id="composer-source"
            aria-label="Python source"
            className="h-[18rem] max-h-[45vh] overflow-y-auto font-mono text-xs"
            spellCheck={false}
            value={source}
            placeholder={'def main(ctx):\n    ctx.progress("started")\n'}
            onChange={(event) => onSourceChange(event.target.value)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              void handleFile(event.dataTransfer.files?.[0]);
            }}
          />
          {sourceNotice ? (
            <p className="text-xs text-destructive" role="status" data-testid="source-notice">
              {sourceNotice}
            </p>
          ) : null}
          <ReadinessView state={readiness} stale={readinessStale} />
          {readiness.kind === "unknown" ? (
            <label className="flex items-start gap-2 text-sm">
              <Checkbox
                checked={acceptUnknown}
                onCheckedChange={(value) => setAcceptUnknown(value === true)}
                aria-label="Acknowledge the unverified source"
              />
              <span>
                I understand this source cannot be certified ready, and I want to run it anyway.
              </span>
            </label>
          ) : null}
          {readiness.kind === "ready" && readiness.result.profile ? (
            <p className="text-xs text-muted-foreground">
              Runner profile <code>{readiness.result.profile.id}</code> · Python{" "}
              {readiness.result.profile.python} ·{" "}
              {readiness.result.profile.runtime_pip_install
                ? "installs packages at run time"
                : "no package installs at run time"}
              .
            </p>
          ) : null}
        </div>
      </Panel>
      <Panel title="Inputs">
        <div className="flex flex-col gap-4">
          <p className="text-xs text-muted-foreground">
            Values your code reads from <code>ctx.params</code>, and what the first run uses. A strategy
            with no inputs needs nothing here.
          </p>
          {fields.length === 0 ? (
            <p className="text-sm text-muted-foreground">No parameters.</p>
          ) : (
            <ul className="flex flex-col gap-3">
              {fields.map((field) => (
                <li
                  key={field.name}
                  className="grid gap-3 rounded-md border border-border/60 p-3 md:grid-cols-[1fr_1fr_auto]"
                >
                  <div>
                    <p className="font-mono text-xs">{field.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {field.type}
                      {fieldConstraintSummary(field) ? ` · ${fieldConstraintSummary(field)}` : ""}
                    </p>
                  </div>
                  <div className="grid gap-1">
                    <Label htmlFor={`composer-value-${field.name}`}>Value for the first run</Label>
                    <ParamValueInput
                      field={field}
                      value={paramValues.drafts[field.name]}
                      onChange={(value) => paramValues.setDraft(field.name, value)}
                      idPrefix="composer-value"
                    />
                    {paramValues.errors[field.name] ? (
                      <p className="text-xs text-destructive" role="alert">
                        {paramValues.errors[field.name]}
                      </p>
                    ) : null}
                  </div>
                  <div className="flex items-start justify-end">
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        setFields((current) => current.filter((item) => item.name !== field.name))
                      }
                    >
                      Remove
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <AddParameterField
            existing={fields.map((field) => field.name)}
            onAdd={(field) => setFields((current) => [...current, field])}
          />
          <div className="flex flex-col gap-2">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={advancedSchema}
                onCheckedChange={(value) => {
                  const enabled = value === true;
                  setAdvancedSchema(enabled);
                  if (enabled) setSchemaText(JSON.stringify(schemaFromFields, null, 2));
                }}
                aria-label="Use a JSON Schema instead"
              />
              Use a JSON Schema instead
            </label>
            {advancedSchema ? (
              <div className="flex flex-col gap-2">
                <Textarea
                  aria-label="Parameters schema (JSON Schema)"
                  rows={6}
                  className="font-mono text-xs"
                  value={schemaText}
                  onChange={(event) => setSchemaText(event.target.value)}
                />
                {!versionSchema.ok ? (
                  <p className="text-xs text-destructive">{versionSchema.error}</p>
                ) : null}
                <HostedParamInputs params={paramValues} idPrefix="composer-json" />
              </div>
            ) : null}
          </div>
        </div>
      </Panel>
      <Panel title="Mode">
        <div className="grid gap-1.5 md:max-w-sm">
          <Label htmlFor="composer-environment">Environment</Label>
          <Select value={environment} onValueChange={setEnvironmentChoice}>
            <SelectTrigger id="composer-environment" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {supportedExecutionModes(options).map((mode) => (
                <SelectItem
                  key={mode}
                  value={mode}
                  disabled={mode === LIVE_MODE && liveDisabledByPlatform}
                >
                  {environmentLabel(mode)}
                  {mode === LIVE_MODE && liveDisabledByPlatform ? " (off right now)" : ""}
                </SelectItem>
              ))}
              {options && !isModeSupported(options, environment) ? (
                <SelectItem value={environment}>{environmentLabel(environment)}</SelectItem>
              ) : null}
            </SelectContent>
          </Select>
          {options && !liveModeSupported(options) ? (
            <p className="text-xs text-muted-foreground">
              Live execution is not enabled on this deployment, so it is not offered here.
            </p>
          ) : liveDisabledByPlatform ? (
            <p className="text-xs text-muted-foreground" data-testid="live-disabled-by-platform">
              This deployment&apos;s platform status reports live trading is off right now, so Live is
              disabled here until it is turned back on.
            </p>
          ) : environment === LIVE_MODE ? (
            <p className="text-xs text-muted-foreground">
              Live places real orders
              {liveLaneSummary(options) ? ` · lanes: ${liveLaneSummary(options)}` : ""}.
              {liveRequiresOwnerApproval(options)
                ? " A live attempt needs your own authority, not the platform's: either your decision on the plan it submits, or the standing authorization you issue for this version, account, limits and environment. The platform never approves one for you."
                : " This deployment reports that it does not wait for your approval of live attempts."}
            </p>
          ) : null}
        </div>
      </Panel>
      <Panel title="Run style">
        <div className="flex flex-col gap-4">
          <div className="grid gap-3 md:grid-cols-2">
            <button
              type="button"
              onClick={() => setRunStyle("now")}
              aria-pressed={runStyle === "now"}
              className={`rounded-lg border p-3 text-left text-sm transition ${
                runStyle === "now" ? "border-primary/60 bg-primary/5" : "border-border/70 hover:bg-muted/40"
              }`}
            >
              <span className="flex items-center gap-2 font-semibold">
                {runStyle === "now" ? <CheckCircle2Icon className="size-4" aria-hidden /> : null}
                Run now
              </span>
              <span className="mt-1 block text-xs text-muted-foreground">
                Queues the first attempt as soon as Start finishes.
              </span>
            </button>
            <button
              type="button"
              onClick={() => setRunStyle("schedule")}
              aria-pressed={runStyle === "schedule"}
              className={`rounded-lg border p-3 text-left text-sm transition ${
                runStyle === "schedule"
                  ? "border-primary/60 bg-primary/5"
                  : "border-border/70 hover:bg-muted/40"
              }`}
            >
              <span className="flex items-center gap-2 font-semibold">
                {runStyle === "schedule" ? <CheckCircle2Icon className="size-4" aria-hidden /> : null}
                On a schedule
              </span>
              <span className="mt-1 block text-xs text-muted-foreground">
                Starts a new attempt at a fixed local time instead of right away.
              </span>
            </button>
          </div>
          {runStyle === "schedule" ? (
            <div className="grid gap-4 md:grid-cols-2">
              <div className="grid gap-1.5">
                <Label htmlFor="composer-schedule-kind">Cadence</Label>
                <Select
                  value={scheduleDraft.scheduleKind}
                  onValueChange={(value) =>
                    setScheduleDraft((current) => ({ ...current, scheduleKind: value as ScheduleKind }))
                  }
                >
                  <SelectTrigger id="composer-schedule-kind" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="daily">Every day</SelectItem>
                    <SelectItem value="weekly">Every week</SelectItem>
                    <SelectItem value="monthly">Every month</SelectItem>
                    <SelectItem value="calendar">On chosen dates</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="composer-schedule-time">Local time</Label>
                <Input
                  id="composer-schedule-time"
                  type="time"
                  value={scheduleDraft.atTime}
                  onChange={(event) =>
                    setScheduleDraft((current) => ({ ...current, atTime: event.target.value }))
                  }
                />
              </div>
              {scheduleDraft.scheduleKind === "weekly" ? (
                <div className="grid gap-1.5">
                  <Label htmlFor="composer-schedule-weekday">Weekday</Label>
                  <Select
                    value={String(scheduleDraft.weekday)}
                    onValueChange={(value) =>
                      setScheduleDraft((current) => ({ ...current, weekday: Number(value) }))
                    }
                  >
                    <SelectTrigger id="composer-schedule-weekday" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {WEEKDAYS.map((day, index) => (
                        <SelectItem key={day} value={String(index)}>
                          {day}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              ) : null}
              {scheduleDraft.scheduleKind === "monthly" ? (
                <div className="grid gap-1.5">
                  <Label htmlFor="composer-schedule-day">Day of month</Label>
                  <Input
                    id="composer-schedule-day"
                    inputMode="numeric"
                    value={String(scheduleDraft.dayOfMonth)}
                    onChange={(event) =>
                      setScheduleDraft((current) => ({
                        ...current,
                        dayOfMonth: Number(event.target.value) || 1,
                      }))
                    }
                  />
                </div>
              ) : null}
              {scheduleDraft.scheduleKind === "calendar" ? (
                <div className="grid gap-1.5 md:col-span-2">
                  <Label htmlFor="composer-schedule-dates">Dates (comma-separated, YYYY-MM-DD)</Label>
                  <Input
                    id="composer-schedule-dates"
                    value={scheduleDraft.calendarDates}
                    onChange={(event) =>
                      setScheduleDraft((current) => ({ ...current, calendarDates: event.target.value }))
                    }
                  />
                </div>
              ) : null}
              <div className="grid gap-1.5">
                <Label htmlFor="composer-schedule-timezone">Timezone</Label>
                <Select
                  value={scheduleDraft.timezone}
                  onValueChange={(value) =>
                    setScheduleDraft((current) => ({ ...current, timezone: value }))
                  }
                >
                  <SelectTrigger id="composer-schedule-timezone" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TIMEZONES.map((zone) => (
                      <SelectItem key={zone} value={zone}>
                        {zone}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          ) : null}
        </div>
      </Panel>
      <div className="flex flex-col gap-3">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="self-start"
          onClick={() => setAdvancedOpen((value) => !value)}
        >
          {advancedOpen ? (
            <ChevronDownIcon className="size-4" aria-hidden />
          ) : (
            <ChevronRightIcon className="size-4" aria-hidden />
          )}
          Advanced
        </Button>
        {advancedOpen ? (
          <div className="flex flex-col gap-5">
            <Panel title="Description">
              <div className="grid gap-1.5">
                <Label htmlFor="composer-description">Description (optional)</Label>
                <Textarea
                  id="composer-description"
                  rows={2}
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </div>
            </Panel>
            <Panel title="Permissions">
              <div className="flex flex-col gap-3">
                {(
                  [
                    [
                      "data",
                      "Read market data",
                      "Quotes, candles, indices, indicators, option chains and owned universes.",
                    ],
                    [
                      "trade",
                      "Propose trades",
                      "Submit trade proposals for admission and approval. It never places an order by itself.",
                    ],
                    [
                      "notify",
                      "Send notifications",
                      "Publish run notifications through the configured channels.",
                    ],
                  ] as const
                ).map(([key, label, help]) => (
                  <label key={key} className="flex items-start gap-3 text-sm">
                    <Checkbox
                      checked={permissions[key]}
                      onCheckedChange={(value) =>
                        setPermissions((current) => ({ ...current, [key]: value === true }))
                      }
                      aria-label={label}
                    />
                    <span>
                      <span className="font-medium">{label}</span>
                      <span className="block text-xs text-muted-foreground">{help}</span>
                    </span>
                  </label>
                ))}
                <p className="text-xs text-muted-foreground">
                  What you choose here is the whole grant. Reading or scanning the code never grants
                  anything.
                </p>
              </div>
            </Panel>
            <Panel title="Who approves trades">
              {!trades ? (
                <p className="text-sm text-muted-foreground" data-testid="authorization-inapplicable">
                  This strategy has no &quot;Propose trades&quot; permission, so it never asks for a trade
                  decision. Add that permission above to choose between review-first and automatic trading.
                </p>
              ) : (
                <div className="flex flex-col gap-4">
                  <div className="grid gap-3 md:grid-cols-2">
                    <button
                      type="button"
                      onClick={() => setAuthorization(APPROVAL_BASED)}
                      aria-pressed={authorization === APPROVAL_BASED}
                      className={`rounded-lg border p-3 text-left text-sm transition ${
                        authorization === APPROVAL_BASED
                          ? "border-primary/60 bg-primary/5"
                          : "border-border/70 hover:bg-muted/40"
                      }`}
                    >
                      <span className="flex items-center gap-2 font-semibold">
                        {authorization === APPROVAL_BASED ? (
                          <CheckCircle2Icon className="size-4" aria-hidden />
                        ) : null}
                        {authorizationModeLabel(APPROVAL_BASED)}
                      </span>
                      <span className="mt-1 block text-xs text-muted-foreground">
                        {authorizationModeExplanation(APPROVAL_BASED)}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setAuthorization(AUTONOMOUS)}
                      aria-pressed={authorization === AUTONOMOUS}
                      className={`rounded-lg border p-3 text-left text-sm transition ${
                        authorization === AUTONOMOUS
                          ? "border-primary/60 bg-primary/5"
                          : "border-border/70 hover:bg-muted/40"
                      }`}
                    >
                      <span className="flex items-center gap-2 font-semibold">
                        {authorization === AUTONOMOUS ? (
                          <CheckCircle2Icon className="size-4" aria-hidden />
                        ) : null}
                        {authorizationModeLabel(AUTONOMOUS)}
                      </span>
                      <span className="mt-1 block text-xs text-muted-foreground">
                        {authorizationModeExplanation(AUTONOMOUS)}
                      </span>
                    </button>
                  </div>
                  <div className="flex flex-col gap-2">
                    <p className="text-sm font-medium">
                      {desiredMode === AUTONOMOUS
                        ? "Limits the authorization is bound to"
                        : "Limits a trade is admitted against"}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      These are your numbers, not defaults: admission checks them before any trade,
                      whoever approves it.
                    </p>
                    <LimitsFields limits={limits} onChange={setLimits} />
                  </div>
                </div>
              )}
            </Panel>
            <Panel title="Timing and run protection">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="grid gap-1.5">
                  <Label htmlFor="composer-kind">Run kind</Label>
                  <Select value={jobKind} onValueChange={setJobKindChoice}>
                    <SelectTrigger id="composer-kind" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(options?.job_kinds ?? []).map((kind) => (
                        <SelectItem key={kind} value={kind}>
                          {kind === "continuous"
                            ? "Continuous (runs until stopped)"
                            : "Finite (finishes on its own)"}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="composer-stale">If the strategy stops reporting</Label>
                  <Select value={staleExitPolicy} onValueChange={setPolicyChoice}>
                    <SelectTrigger id="composer-stale" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(options?.stale_exit_policies ?? []).map((policy) => (
                        <SelectItem key={policy} value={policy}>
                          {policy === "none" ? "Keep positions" : "Exit positions (platform risk reduction)"}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="composer-max">Max duration (seconds)</Label>
                  <Input
                    id="composer-max"
                    inputMode="numeric"
                    value={maxDuration}
                    onChange={(event) => setMaxDuration(event.target.value)}
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="composer-deadline">Progress deadline (seconds)</Label>
                  <Input
                    id="composer-deadline"
                    inputMode="numeric"
                    value={progressDeadline}
                    onChange={(event) => setProgressDeadline(event.target.value)}
                  />
                </div>
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                Stop always ends the process. It does not flatten positions and does not cancel orders a
                broker already holds.
              </p>
            </Panel>
          </div>
        ) : null}
      </div>
      <Panel title="What this will do">
        <div className="flex flex-col gap-3">
          <p className="text-sm" data-testid="composer-summary">
            {summary}
          </p>
          <p className="text-xs text-muted-foreground">
            {!trades
              ? "This strategy only reads and reports; there is no trade for anyone to approve."
              : desiredMode === AUTONOMOUS
                ? "Automatic trading starts once the authorization is issued, and stops when you revoke it or change the version, account or limits."
                : "Each trade this strategy proposes waits for your decision before anything is sent."}
          </p>
          {steps.versionId ? (
            <p className="text-xs text-muted-foreground">
              A version is already registered. Editing the code, its schema or its permissions registers a
              new version.
            </p>
          ) : null}
          {notice ? (
            <Alert data-testid="composer-notice">
              <AlertTitle>Continuing</AlertTitle>
              <AlertDescription>{notice}</AlertDescription>
            </Alert>
          ) : null}
          {failure ? (
            <Alert variant="destructive" data-testid="composer-error">
              <AlertTitle>That step did not complete</AlertTitle>
              <AlertDescription>
                {failure}
                {startOver ? (
                  <span className="mt-3 block">
                    <Button type="button" size="sm" variant="outline" onClick={clearEverything}>
                      Start a new strategy
                    </Button>
                  </span>
                ) : steps.strategyId ? (
                  <span className="mt-2 block">
                    The strategy and any version already created were kept, so retrying continues from the
                    step that failed instead of creating a duplicate.
                  </span>
                ) : null}
              </AlertDescription>
            </Alert>
          ) : null}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={submit}
              disabled={Boolean(busy) || optionsQuery.isLoading || readinessBlocks || readinessStale}
            >
              {busy ? (
                <Loader2Icon className="size-4 animate-spin" aria-hidden />
              ) : (
                <PlayIcon className="size-4" aria-hidden />
              )}
              {busy ?? "Start"}
            </Button>
            <Link href="/strategies" className="text-sm underline">
              Back to strategies
            </Link>
          </div>
        </div>
      </Panel>
    </div>
  );
}
