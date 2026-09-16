"use client";

/**
 * The one alert authoring experience: create and edit on a single page.
 *
 * The common case reads top to bottom — pick an instrument, see its live price,
 * state the rule, hear what the target means, choose how often to be told, pick
 * a destination, save — and everything uncommon is disclosed in place rather
 * than moved to a second product:
 *
 * * the full condition editor (AND/OR/NOT groups, extra conditions) sits under
 *   the primary rule row;
 * * timing and noise controls (cooldown, daily cap, rearm, hysteresis, expiry)
 *   live in a collapsed section that stays on the page;
 * * instruments/universe targeting is a disclosure, not another screen;
 * * Code view (lossless YAML/JSON) is a tab on the same page, and it is where an
 *   unmodeled construct goes instead of being silently dropped.
 *
 * Nothing here re-implements server authority: capabilities come from the
 * server, validation is the server's, and the save path is the same documents
 * API the wizard used.
 */

import Link from "next/link";
import {
  AlertCircleIcon,
  ArrowRightIcon,
  CheckIcon,
  CodeIcon,
  InfoIcon,
  ZapIcon,
} from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { AdvancedDefinitionEditor } from "@/features/alerts/components/advanced-definition-editor";
import { AlertsPageHeader } from "@/features/alerts/components/alerts-page-header";
import { ConditionEditor } from "@/features/alerts/components/condition-editor";
import { InstrumentPicker } from "@/features/alerts/components/instrument-picker";
import { LiveSidePanel } from "@/features/alerts/components/live-side-panel";
import { UniverseTargetingEditor } from "@/features/alerts/components/universe-targeting-editor";
import {
  activateAlertsWorkflow,
  createAlertsWorkflow,
  fetchAlertsUniverse,
  fetchAlertsWorkflow,
  patchAlertsWorkflow,
  validateAlertsWorkflow,
} from "@/features/alerts/api";
import { useAlertsCapabilities, useAlertsChannels } from "@/features/alerts/hooks/use-alerts-queries";
import { useDefinitionValidation } from "@/features/alerts/hooks/use-definition-validation";
import { useMarketQuote, useQuotePresentation } from "@/features/alerts/hooks/use-market-stream";
import { describeCoverage } from "@/features/alerts/lib/alert-state";
import {
  OPERATOR_LABELS,
  type AlertDraft,
  buildDocument,
  exchangeOf,
  sessionLabel,
} from "@/features/alerts/lib/authoring";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";
import {
  EVALUATION_OPTIONS,
  FREQUENCY_OPTIONS,
  TARGET_SHORTCUTS,
  applyFrequency,
  describeFrequency,
  describeRule,
  describeTarget,
  evaluationLabel,
  formatAge,
  formatPrice,
  frequencyOf,
  inferSession,
  needsTimeframe,
  offsetPrice,
  suggestName,
  timeframeOptions,
} from "@/features/alerts/lib/plain-language";
import {
  VALIDATION_LABEL,
  VALIDATION_TONE,
} from "@/features/alerts/lib/status";
import { useDirtyGuard } from "@/features/alerts/lib/use-dirty-guard";
import { newIdempotencyKey } from "@/lib/ids";

const LEVEL_OPERATORS = [
  "crosses_above",
  "crosses_below",
  "gt",
  "lt",
  "gte",
  "lte",
  "rises_pct",
  "falls_pct",
];

type Outcome =
  | { kind: "draft-saved"; workflowId: string }
  | { kind: "activation-failed"; workflowId: string; reason: string }
  | { kind: "conflict"; reason?: string };

export type UnifiedEditorMode =
  | { kind: "create" }
  | {
      kind: "edit";
      workflowId: string;
      expectedRevision: number;
      baseDocument: Record<string, unknown> | null;
      workflowName: string;
      yaml?: string | null;
    };

export function UnifiedAlertEditor({
  scope,
  mode,
  initialDraft,
  conversionError,
}: {
  scope: string | null;
  mode: UnifiedEditorMode;
  initialDraft: AlertDraft;
  /** Set when the stored document cannot be represented structurally. */
  conversionError?: string;
}) {
  const router = useRouter();
  const isEdit = mode.kind === "edit";
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const channelsQuery = useAlertsChannels(scope);

  const [draft, setDraft] = useState<AlertDraft>(initialDraft);
  const [view, setView] = useState<"form" | "code">(conversionError ? "code" : "form");
  const [nameTouched, setNameTouched] = useState(Boolean(initialDraft.name));
  // Channels follow the same rule as the name: the derived default is what the
  // operator sees, so it must be what gets saved — until they change it, after
  // which their choice (including "none") is the truth.
  const [channelsTouched, setChannelsTouched] = useState(initialDraft.alert.channels.length > 0);
  const [expectedRevision, setExpectedRevision] = useState(
    isEdit ? mode.expectedRevision : 1,
  );
  // The merge target for a structured save: a conflict reload replaces it with
  // the newer revision so the next save is based on what is actually stored.
  const [baseDocument, setBaseDocument] = useState<Record<string, unknown> | null>(
    mode.kind === "edit" ? mode.baseDocument : null,
  );
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [idempotencyKey] = useState(() => newIdempotencyKey("alert"));
  const [moreOpen, setMoreOpen] = useState(false);

  const capabilities = capabilitiesQuery.data?.capabilities;
  const enabledChannels = (channelsQuery.data?.channels ?? []).filter((channel) => channel.enabled);
  const channels = draft.alert.channels;
  const selectedChannels = channelsTouched
    ? channels
    : enabledChannels.length === 1
      ? [enabledChannels[0].name]
      : [];

  const targetingUniverse = draft.targeting === "universe";
  const primaryInstrument = targetingUniverse ? "" : (draft.instruments[0] ?? "");

  // Live price for the instrument being configured. Multi-instrument and
  // universe alerts never pretend one price speaks for the whole definition.
  const { quote, status } = useMarketQuote(primaryInstrument || null);
  const presentation = useQuotePresentation(quote, status);

  const universeProbe = useQuery({
    queryKey: ["alerts", "editor-universe-probe", scope, targetingUniverse ? draft.universe.union[0]?.name ?? "" : ""],
    queryFn: () => fetchAlertsUniverse(draft.universe.union[0]?.name ?? "", scope),
    enabled: Boolean(scope && targetingUniverse && draft.universe.union[0]?.name),
    staleTime: 5 * 60_000,
  });

  const inference = useMemo(() => {
    if (!capabilities) return { session: "", exchange: "", error: null };
    if (targetingUniverse) {
      const members = universeProbe.data?.latest_members ?? [];
      const exchanges = new Set(members.map((key) => exchangeOf(String(key))));
      if (exchanges.size === 1) {
        const single = inferSession(`X:${[...exchanges][0]}`, capabilities.session_exchanges);
        return { ...single, exchange: [...exchanges][0] };
      }
      if (members.length > 1) {
        return {
          session: "",
          exchange: "",
          error:
            "This universe spans more than one exchange, so it cannot be evaluated in one session. Use a universe from a single exchange.",
        };
      }
      return { session: "", exchange: "", error: null };
    }
    return inferSession(primaryInstrument, capabilities.session_exchanges);
  }, [capabilities, targetingUniverse, universeProbe.data, primaryInstrument]);


  const operator = draft.conditions[0]?.op ?? "crosses_above";
  const rightOperand = draft.conditions[0]?.right;
  const storedTarget =
    rightOperand && rightOperand.kind === "constant" && typeof rightOperand.value === "number"
      ? rightOperand.value
      : null;
  // The target is held as text so "not entered yet" is distinguishable from a
  // real value: a brand-new alert must not look ready with a 0 target.
  const [targetText, setTargetText] = useState<string>(() => {
    if (storedTarget === null) return "";
    // A brand-new alert starts with a placeholder level of 0 that the operator
    // has not entered anything into; showing it as a real target would let a
    // meaningless rule look ready to save.
    if (mode.kind === "create" && storedTarget === 0) return "";
    return String(storedTarget);
  });
  const parsedTarget = targetText.trim() === "" ? null : Number(targetText);
  const targetValue = parsedTarget !== null && Number.isFinite(parsedTarget) ? parsedTarget : null;

  const effectiveName =
    nameTouched && draft.name.trim()
      ? draft.name
      : suggestName(
          primaryInstrument ? primaryInstrument.split(":").slice(1).join(":") : draft.universe.union[0]?.name ?? "",
          OPERATOR_LABELS[operator] ?? operator,
          targetValue,
        );

  const target = describeTarget(targetValue, presentation.price, operator);

  // What gets validated and saved: the inferred session (never a control the
  // operator has to understand, and an ambiguous exchange is reported instead of
  // guessed) and the generated name (a saved document must not carry an empty
  // name just because the operator never typed one).
  const effectiveDraft: AlertDraft = {
    ...draft,
    session: inference.session || draft.session,
    name: effectiveName,
    // Destinations are part of the definition, so the selection the operator can
    // see (including a single preselected channel) is what gets validated and
    // saved — not only the value they happened to click.
    alert: { ...draft.alert, channels: selectedChannels },
  };

  // The unsaved-work guard compares the draft as first loaded with what the
  // operator sees now; the target text and touched flags are part of the draft's
  // story even though they live outside `AlertDraft`. useState captures the
  // first render's value without touching refs during render.
  const dirtySnapshot = JSON.stringify({ draft: effectiveDraft, targetText, nameTouched, channelsTouched });
  const [initialSnapshot] = useState(dirtySnapshot);
  const isDirty = dirtySnapshot !== initialSnapshot;
  const { attemptExit, dialog: dirtyDialog } = useDirtyGuard(isDirty);

  // The rail's reading of exactly what will be saved, derived from the same
  // effective draft the save path uses — never a parallel interpretation.
  const railSummary = {
    title: isEdit ? "You are editing" : "You are creating",
    name: effectiveName,
    rule: targetingUniverse
      ? describeCoverage(draft.instruments, true, [])
      : describeRule(
          primaryInstrument.split(":").slice(1).join(":"),
          OPERATOR_LABELS[operator] ?? operator,
          targetValue,
        ),
    frequency: describeFrequency(effectiveDraft.alert),
    channels: selectedChannels,
  };

  const updateCondition = (patch: Partial<AlertDraft["conditions"][number]>) => {
    setDraft((current) => {
      const conditions = [...current.conditions];
      conditions[0] = { ...conditions[0], ...patch };
      return { ...current, conditions };
    });
  };

  // -- save ------------------------------------------------------------

  const save = useMutation({
    mutationFn: async (intent: "draft" | "activate") => {
      const document = buildDocument(effectiveDraft, baseDocument);
      if (mode.kind === "create") {
        const created = await createAlertsWorkflow(
          { name: effectiveName, document, idempotency_key: idempotencyKey },
          scope,
        );
        if (intent === "draft") {
          return { kind: "draft-saved" as const, workflowId: created.workflow_id };
        }
        try {
          await activateAlertsWorkflow(created.workflow_id, { scope });
        } catch (error) {
          // The draft IS saved; saying otherwise would be a lie and retrying the
          // create would duplicate it.
          return {
            kind: "activation-failed" as const,
            workflowId: created.workflow_id,
            reason: alertsErrorMessage(error, "activation was refused"),
          };
        }
        router.push(`/alerts/${created.workflow_id}`);
        return { kind: "saved" as const, workflowId: created.workflow_id };
      }

      const patched = await patchAlertsWorkflow(
        mode.workflowId,
        { document, expected_revision: expectedRevision },
        scope,
      );
      const revision = patched.revision ?? expectedRevision + 1;
      if (intent === "activate") {
        await activateAlertsWorkflow(mode.workflowId, { revision, scope });
      }
      router.push(`/alerts/${mode.workflowId}`);
      return { kind: "saved" as const, workflowId: mode.workflowId };
    },
    onSuccess: (result) => {
      if (result.kind === "draft-saved" || result.kind === "activation-failed") {
        setOutcome(result);
      }
    },
    onError: (error) => {
      const status = (error as { status?: number } | null)?.status;
      if (status === 409) {
        // Keep every entered value: the operator decides what to do with the
        // newer revision rather than losing work to it.
        setOutcome({ kind: "conflict" });
      }
    },
  });

  const reloadLatest = useMutation({
    mutationFn: async () => {
      if (mode.kind !== "edit") return null;
      return fetchAlertsWorkflow(mode.workflowId, { scope });
    },
    onSuccess: (payload) => {
      if (!payload) return;
      setExpectedRevision(
        payload.latest_revision?.revision ?? payload.active_revision?.revision ?? expectedRevision + 1,
      );
      setBaseDocument(payload.document ?? null);
      setOutcome(null);
    },
    onError: (error) => setOutcome({ kind: "conflict", reason: alertsErrorMessage(error, "reload failed") } as never),
  });

  // -- completeness ----------------------------------------------------

  const issues: string[] = [];
  if (!targetingUniverse && draft.instruments.length === 0) issues.push("Choose an instrument.");
  if (targetingUniverse && draft.universe.union.every((ref) => !ref.name.trim())) {
    issues.push("Choose a universe.");
  }
  if (targetValue === null) issues.push("Enter the value this alert compares against.");
  if (selectedChannels.length === 0) issues.push("Choose at least one destination.");
  if (inference.error) issues.push(inference.error);

  const ready = issues.length === 0;
  const pending = save.isPending;

  // Background validation: only a draft that is complete enough to mean anything
  // is sent, and the live price is the preview sample.
  const validationDocument = ready
    ? buildDocument(effectiveDraft, mode.kind === "edit" ? baseDocument : null)
    : null;
  const validation = useDefinitionValidation({
    scope,
    document: validationDocument,
    enabled: ready,
    quote,
    instrumentKey: primaryInstrument,
    clock: draft.clock,
    crossed: Boolean(target?.alreadyBeyond),
  });

  if (capabilitiesQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading alert options…</p>;
  }
  if (capabilitiesQuery.error || !capabilities) {
    return (
      <Alert variant="destructive" role="alert">
        <AlertCircleIcon className="size-4" />
        <AlertTitle>Could not load alert options</AlertTitle>
        <AlertDescription>
          {alertsErrorMessage(capabilitiesQuery.error, "the server did not return alert options")}
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="flex flex-col gap-5 pb-32">
      <AlertsPageHeader
        backHref={isEdit ? `/alerts/${mode.workflowId}` : "/alerts"}
        onBack={attemptExit}
        trail={
          isEdit
            ? [
                { label: "Alerts", href: "/alerts" },
                { label: mode.workflowName, href: `/alerts/${mode.workflowId}` },
                { label: "Edit" },
              ]
            : [
                { label: "Alerts", href: "/alerts" },
                { label: "New alert" },
              ]
        }
        right={
          <div role="tablist" aria-label="Editor view" className="flex gap-2">
            {(
              [
                // Code view is never gated: it is the route that preserves an
                // unmodeled definition, so the FORM is what becomes unavailable.
                [
                  "form",
                  "Form",
                  conversionError ? "this definition uses features the form cannot show" : null,
                ],
                ["code", "Code view", null],
              ] as const
            ).map(([value, label, disabledReason]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={view === value}
                disabled={disabledReason !== null}
                title={disabledReason ?? undefined}
                onClick={() => setView(value)}
                className={
                  view === value
                    ? "rounded-full border border-primary/60 bg-primary/10 px-3 py-1 text-xs text-primary"
                    : "rounded-full border border-border/60 px-3 py-1 text-xs text-muted-foreground disabled:opacity-50"
                }
              >
                {label}
              </button>
            ))}
          </div>
        }
      />
      <p className="text-sm text-muted-foreground">
        {isEdit
          ? "Saving creates a new draft revision; activation stays a separate, explicit step."
          : "Pick an instrument, set the level, choose where it notifies."}
      </p>

      {conversionError ? (
        <Alert role="status">
          <InfoIcon className="size-4" />
          <AlertTitle>Editing as text</AlertTitle>
          <AlertDescription>
            {conversionError} The definition is shown as-is below, so nothing is dropped.
          </AlertDescription>
        </Alert>
      ) : null}

      {outcome?.kind === "draft-saved" ? (
        <Alert role="status">
          <CheckIcon className="size-4" />
          <AlertTitle>Draft saved</AlertTitle>
          <AlertDescription>
            Nothing is evaluating yet.{" "}
            <Link className="underline" href={`/alerts/${outcome.workflowId}`}>
              Open it and switch it on
            </Link>{" "}
            when you are ready.
          </AlertDescription>
        </Alert>
      ) : null}
      {outcome?.kind === "activation-failed" ? (
        <Alert variant="destructive" role="alert">
          <AlertCircleIcon className="size-4" />
          <AlertTitle>Saved as a draft — activation failed</AlertTitle>
          <AlertDescription className="flex flex-col gap-2">
            <span>{outcome.reason}</span>
            <Link className="underline" href={`/alerts/${outcome.workflowId}`}>
              Open the draft to retry activation
            </Link>
          </AlertDescription>
        </Alert>
      ) : null}
      {outcome?.kind === "conflict" ? (
        <Alert variant="destructive" role="alert">
          <AlertCircleIcon className="size-4" />
          <AlertTitle>This alert changed while you were editing</AlertTitle>
          <AlertDescription className="flex flex-col gap-2">
            <span>Your changes are still here. Load the newer revision to continue against it.</span>
            <Button
              type="button"
              size="sm"
              variant="secondary"
              disabled={reloadLatest.isPending}
              onClick={() => reloadLatest.mutate()}
            >
              {reloadLatest.isPending ? "Loading…" : "Load the newer revision"}
            </Button>
            {outcome.reason ? <span className="text-xs">{outcome.reason}</span> : null}
          </AlertDescription>
        </Alert>
      ) : null}
      {save.error && (save.error as { status?: number } | null)?.status !== 409 ? (
        <Alert variant="destructive" role="alert">
          <AlertCircleIcon className="size-4" />
          <AlertTitle>Could not save</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(save.error, "the server rejected the request")}
          </AlertDescription>
        </Alert>
      ) : null}

      {view === "code" ? (
        isEdit && mode.kind === "edit" ? (
          <AdvancedDefinitionEditor
            workflowId={mode.workflowId}
            scope={scope}
            name={mode.workflowName}
            expectedRevision={expectedRevision}
            initialYaml={mode.yaml ?? null}
            initialDocument={mode.baseDocument}
            reason={conversionError}
          />
        ) : (
          <CodeViewCreate
            draft={draft}
            scope={scope}
            name={effectiveName}
            idempotencyKey={idempotencyKey}
          />
        )
      ) : (
        <div className="mx-auto grid w-full max-w-6xl gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <div className="flex min-w-0 flex-col gap-5">
            {/* ---------------------------------------------------------- instrument */}
            <Panel id="section-instrument" className="flex flex-col gap-4 p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Label htmlFor="alert-instrument">Instrument</Label>
                <button
                  type="button"
                  className="text-xs underline text-muted-foreground"
                  onClick={() =>
                    setDraft((current) => ({
                      ...current,
                      targeting: current.targeting === "universe" ? "instruments" : "universe",
                    }))
                  }
                >
                  {targetingUniverse ? "Use specific instruments instead" : "Scan a universe instead"}
                </button>
              </div>
              {targetingUniverse ? (
                <UniverseTargetingEditor
                  scope={scope}
                  value={draft.universe}
                  onChange={(universe) => setDraft({ ...draft, universe })}
                />
              ) : (
                <InstrumentPicker
                  selected={draft.instruments}
                  onChange={(instruments) => setDraft({ ...draft, instruments })}
                  acceptedExchanges={
                    inference.session ? capabilities.session_exchanges[inference.session] ?? [] : null
                  }
                />
              )}

              {primaryInstrument && quote ? (
                <div className="flex flex-wrap items-baseline gap-3 rounded-lg border border-border/60 bg-card/40 px-3 py-2 lg:hidden">
                  <span className="text-lg font-semibold tabular-nums">
                    {formatPrice(presentation.price)}
                  </span>
                  <StatusBadge tone={presentation.tone}>{presentation.label}</StatusBadge>
                  <span className="text-xs text-muted-foreground">{formatAge(quote.age_ms)}</span>
                  <span className="text-xs text-muted-foreground">
                    {quote.exchange_timestamp
                      ? `exchange ${new Date(quote.exchange_timestamp).toLocaleTimeString()}`
                      : "no exchange timestamp"}
                    {quote.received_at
                      ? ` · received ${new Date(quote.received_at).toLocaleTimeString()}`
                      : ""}
                  </span>
                  {quote.change_percent !== null && quote.change_percent !== undefined ? (
                    <span className="text-xs text-muted-foreground">
                      {quote.change_percent >= 0 ? "+" : ""}
                      {quote.change_percent.toFixed(2)}% today
                    </span>
                  ) : null}
                </div>
              ) : primaryInstrument ? (
                <p className="text-xs text-muted-foreground lg:hidden">
                  Waiting for the first price… {presentation.label === "NO DATA" ? "no data for this instrument yet" : presentation.label}
                </p>
              ) : null}

              <SectionIssues issues={validation.bySection.instrument} />
              {inference.error ? (
                <p className="text-xs text-rose-300" role="alert">
                  {inference.error}
                </p>
              ) : inference.session ? (
                <p className="text-xs text-muted-foreground">
                  {`Evaluated in the ${sessionLabel(inference.session)} session, taken from the instrument's exchange.`}
                </p>
              ) : null}
            </Panel>

            {/* ---------------------------------------------------------- rule */}
            <Panel id="section-rule" className="flex flex-col gap-4 p-5">
              <Label htmlFor="alert-operator">Alert me when</Label>
              <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <Select
                  value={operator}
                  onValueChange={(op) => {
                    const value = op === "rises_pct" || op === "falls_pct" ? 1 : targetValue ?? 0;
                    updateCondition({
                      op,
                      right: { kind: "constant", value },
                      left: { kind: "field", name: op === "rises_pct" || op === "falls_pct" ? "close" : "ltp" },
                    });
                    if (op === "rises_pct" || op === "falls_pct") {
                      setDraft((current) => ({ ...current, clock: "candle_close" }));
                    }
                  }}
                >
                  <SelectTrigger id="alert-operator" aria-label="Condition">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {LEVEL_OPERATORS.filter((op) => op in OPERATOR_LABELS).map((op) => (
                      <SelectItem key={op} value={op}>
                        {OPERATOR_LABELS[op]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div>
                  <Input
                    id="alert-value"
                    aria-label="Target value"
                    type="number"
                    inputMode="decimal"
                    step="any"
                    value={targetText}
                    onChange={(event) => {
                      setTargetText(event.target.value);
                      const next = event.target.value.trim() === "" ? null : Number(event.target.value);
                      updateCondition({
                        right: { kind: "constant", value: next !== null && Number.isFinite(next) ? next : 0 },
                        left: {
                          kind: "field",
                          name: operator === "rises_pct" || operator === "falls_pct" ? "close" : "ltp",
                        },
                      });
                    }}
                  />
                </div>
              </div>

              {target ? (
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>{target.sentence}</span>
                  {presentation.price !== null ? (
                    <span className="flex flex-wrap gap-1">
                      {TARGET_SHORTCUTS.map((shortcut) => (
                        <Button
                          key={shortcut.label}
                          type="button"
                          size="xs"
                          variant="outline"
                          onClick={() => {
                            const next =
                              shortcut.percent === null || shortcut.percent === 0
                                ? Math.round((presentation.price ?? 0) * 100) / 100
                                : offsetPrice(presentation.price ?? 0, shortcut.percent);
                            setTargetText(String(next));
                            updateCondition({ right: { kind: "constant", value: next } });
                          }}
                        >
                          {shortcut.label}
                        </Button>
                      ))}
                    </span>
                  ) : null}
                </div>
              ) : null}

              <div id="section-evaluation" className="flex flex-col gap-2">
                <Label htmlFor="alert-evaluation">Evaluation</Label>
                <Select
                  value={draft.clock}
                  onValueChange={(clock) => setDraft({ ...draft, clock })}
                >
                  <SelectTrigger id="alert-evaluation" className="sm:w-72">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {EVALUATION_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {EVALUATION_OPTIONS.find((option) => option.value === draft.clock)?.hint}
                </p>
                <SectionIssues issues={validation.bySection.evaluation} />
              </div>

              {needsTimeframe(draft) ? (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="alert-timeframe">Measured over</Label>
                  <Select
                    value={draft.timeframe}
                    onValueChange={(timeframe) => setDraft({ ...draft, timeframe })}
                  >
                    <SelectTrigger id="alert-timeframe" className="sm:w-72">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {timeframeOptions(capabilities.timeframes ?? []).map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              ) : null}

              <SectionIssues issues={validation.bySection.rule} />
              <details className="rounded-lg border border-border/60 px-3 py-2">
                <summary className="cursor-pointer text-xs text-muted-foreground">
                  All conditions and groups
                </summary>
                <div className="mt-3">
                  <ConditionEditor
                    conditions={draft.conditions}
                    capabilities={capabilities}
                    onChange={(conditions) => setDraft({ ...draft, conditions })}
                  />
                </div>
              </details>
            </Panel>

            {/* ---------------------------------------------------------- frequency */}
            <Panel id="section-frequency" className="flex flex-col gap-4 p-5">
              <Label>When should we notify you?</Label>
              <div role="radiogroup" aria-label="Notification frequency" className="flex w-fit flex-wrap overflow-hidden rounded-lg border border-border/60">
                {FREQUENCY_OPTIONS.map((option) => {
                  const active = frequencyOf(draft.alert) === option.value;
                  return (
                    <label
                      key={option.value}
                      className={cn(
                        "cursor-pointer border-r border-border/60 px-4 py-2 text-sm last:border-r-0",
                        active ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
                      )}
                    >
                      <input
                        type="radio"
                        name="alert-frequency"
                        className="sr-only"
                        checked={active}
                        onChange={() => setDraft({ ...draft, alert: applyFrequency(draft.alert, option.value) })}
                      />
                      {option.label}
                    </label>
                  );
                })}
              </div>
              <p className="text-xs text-muted-foreground">
                {FREQUENCY_OPTIONS.find((option) => frequencyOf(draft.alert) === option.value)?.hint}
              </p>
              <p className="text-xs text-muted-foreground">{describeFrequency(draft.alert)}</p>
              <SectionIssues issues={validation.bySection.frequency} />

              <details
                className="rounded-lg border border-border/60 px-3 py-2"
                open={moreOpen}
                onToggle={(event) => setMoreOpen((event.target as HTMLDetailsElement).open)}
              >
                <summary className="cursor-pointer text-xs text-muted-foreground">
                  More timing and noise controls
                </summary>
                <div className="mt-3 grid gap-4 sm:grid-cols-2">
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="alert-cooldown">Wait before notifying again (minutes)</Label>
                    <Input
                      id="alert-cooldown"
                      type="number"
                      min={0}
                      value={draft.alert.cooldown_s ? Math.round(draft.alert.cooldown_s / 60) : ""}
                      placeholder="no wait"
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          alert: {
                            ...draft.alert,
                            cooldown_s: event.target.value === "" ? null : Number(event.target.value) * 60,
                          },
                        })
                      }
                    />
                    <p className="text-xs text-muted-foreground">
                      Stops a price hovering at the level from notifying repeatedly.
                    </p>
                  </div>
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="alert-max">Maximum notifications today</Label>
                    <Input
                      id="alert-max"
                      type="number"
                      min={1}
                      value={draft.alert.max_per_session ?? ""}
                      placeholder="no limit"
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          alert: {
                            ...draft.alert,
                            max_per_session: event.target.value === "" ? null : Number(event.target.value),
                          },
                        })
                      }
                    />
                    <p className="text-xs text-muted-foreground">A daily cap for a noisy instrument.</p>
                  </div>
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="alert-rearm">Becomes ready again above/below</Label>
                    <Input
                      id="alert-rearm"
                      type="number"
                      step="any"
                      value={draft.alert.rearm_level ?? ""}
                      placeholder="no reset level"
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          alert: {
                            ...draft.alert,
                            rearm_level: event.target.value === "" ? null : Number(event.target.value),
                            rearm_direction:
                              event.target.value === ""
                                ? null
                                : draft.alert.rearm_direction ??
                                  ((targetValue ?? 0) >= (presentation.price ?? 0) ? "below" : "above"),
                          },
                        })
                      }
                    />
                    <p className="text-xs text-muted-foreground">
                      After notifying once, the alert waits until the price reaches this level before it can notify again.
                    </p>
                  </div>
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="alert-consecutive">Consecutive completed candles</Label>
                    <Input
                      id="alert-consecutive"
                      type="number"
                      min={1}
                      value={draft.consecutiveBars ?? ""}
                      placeholder="not required"
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          consecutiveBars: event.target.value === "" ? null : Number(event.target.value),
                        })
                      }
                    />
                    <p className="text-xs text-muted-foreground">
                      Requires the condition to hold for several candles before notifying.
                    </p>
                  </div>
                  <label className="flex items-center gap-2 text-sm" htmlFor="alert-already-true">
                    <input
                      id="alert-already-true"
                      type="checkbox"
                      checked={draft.alert.notify_if_already_true}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          alert: { ...draft.alert, notify_if_already_true: event.target.checked },
                        })
                      }
                    />
                    Tell me if it is already true when I switch it on
                  </label>
                </div>
              </details>
            </Panel>

            {/* ---------------------------------------------------------- destinations */}
            <Panel id="section-destinations" className="flex flex-col gap-3 p-5">
              <Label>Notify via</Label>
              {channelsQuery.isLoading ? (
                <p className="text-xs text-muted-foreground">Loading destinations…</p>
              ) : enabledChannels.length === 0 ? (
                <Alert role="status">
                  <InfoIcon className="size-4" />
                  <AlertTitle>No notification destination yet</AlertTitle>
                  <AlertDescription>
                    Add one under{" "}
                    <Link className="underline" href="/alerts/operations">
                      Operations → Destinations
                    </Link>
                    . Everything you have entered here is kept.
                  </AlertDescription>
                </Alert>
            ) : (
              <div className="flex flex-wrap gap-2">
                {enabledChannels.map((channel) => {
                  const checked = selectedChannels.includes(channel.name);
                  return (
                    <label
                      key={channel.channel_id}
                      htmlFor={`destination-${channel.channel_id}`}
                      className={cn(
                        "flex cursor-pointer items-center gap-2 rounded-full border px-3 py-1.5 text-sm",
                        checked
                          ? "border-primary/60 bg-primary/10 text-primary"
                          : "border-border/60 text-muted-foreground",
                      )}
                    >
                      <Checkbox
                        id={`destination-${channel.channel_id}`}
                        checked={checked}
                        onCheckedChange={() => {
                          setChannelsTouched(true);
                          setDraft({
                            ...draft,
                            alert: {
                              ...draft.alert,
                              channels: checked
                                ? selectedChannels.filter((name) => name !== channel.name)
                                : [...selectedChannels, channel.name],
                            },
                          });
                        }}
                      />
                      {channel.name} · {channel.provider}
                    </label>
                  );
                })}
              </div>
            )}
              <p className="text-xs text-muted-foreground">
                Delivery is accepted by the provider, which is not proof anyone has read it.
              </p>
              <SectionIssues issues={validation.bySection.destinations} />
            </Panel>

            {/* ---------------------------------------------------------- name */}
            <Panel id="section-name" className="flex flex-col gap-2 p-5">
              <Label htmlFor="alert-name">Name</Label>
              <Input
                id="alert-name"
                value={nameTouched ? draft.name : effectiveName}
                onChange={(event) => {
                  setNameTouched(true);
                  setDraft({ ...draft, name: event.target.value });
                }}
                placeholder="Generated from the definition"
              />
              <SectionIssues issues={validation.bySection.other} />
            </Panel>
          </div>
          <aside className="lg:sticky lg:top-4 lg:self-start">
            <LiveSidePanel
              quote={quote ?? null}
              presentation={presentation}
              coverage={targetingUniverse ? describeCoverage(draft.instruments, true, []) : null}
              targetValue={targetValue}
              distance={target}
              validation={validation}
              summary={railSummary}
            />
          </aside>
        </div>
      )}

      {/* ---------------------------------------------------------- save bar */}
      <div className="fixed inset-x-0 bottom-0 z-20 border-t border-border/60 bg-background/95 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="secondary"
            disabled={!ready || pending}
            onClick={() => save.mutate("draft")}
          >
            {isEdit ? "Save changes" : "Save draft"}
          </Button>
          <Button
            type="button"
            disabled={!ready || pending}
            onClick={() => save.mutate("activate")}
          >
            <ZapIcon className="size-4" />
            {pending
              ? "Working…"
              : isEdit
                ? "Save and activate latest"
                : "Create and activate"}
          </Button>
          <div className="flex flex-1 flex-col gap-1 text-xs">
            <span className="flex items-center gap-2">
              <StatusBadge tone={VALIDATION_TONE[validation.state] ?? "neutral"}>
                {VALIDATION_LABEL[validation.state] ?? validation.state}
              </StatusBadge>
              <span className="text-muted-foreground">
                {evaluationLabel(draft.clock)}
                {validation.previewSentence ? ` · ${validation.previewSentence}` : ""}
              </span>
            </span>
            {issues.length ? (
              <ul className="flex flex-col text-muted-foreground" role="status">
                {issues.map((issue) => (
                  <li key={issue} className="flex items-center gap-1">
                    <ArrowRightIcon className="size-3" aria-hidden />
                    {issue}
                  </li>
                ))}
              </ul>
            ) : null}
            {validation.error ? (
              <span className="text-amber-300" role="status">
                {validation.error} Your draft is untouched.
              </span>
            ) : null}
          </div>
        </div>
      </div>
      {dirtyDialog}
    </div>
  );
}

/** Server-reported problems for one section, shown where they can be fixed. */
function SectionIssues({ issues }: { issues: string[] | undefined }) {
  if (!issues || issues.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1 text-xs text-rose-300" role="alert">
      {issues.map((issue) => (
        <li key={issue} className="flex items-start gap-1">
          <AlertCircleIcon className="mt-0.5 size-3 shrink-0" aria-hidden />
          {issue}
        </li>
      ))}
    </ul>
  );
}

/**
 * Code view for a NEW alert: the canonical document as JSON, or YAML if the
 * operator pastes it. Both are lossless — the text is what gets stored, and the
 * server validates it either way.
 */
function CodeViewCreate({
  draft,
  scope,
  name,
  idempotencyKey,
}: {
  draft: AlertDraft;
  scope: string | null;
  name: string;
  idempotencyKey: string;
}) {
  const router = useRouter();
  const [text, setText] = useState(() => JSON.stringify(buildDocument(draft), null, 2));
  const [issues, setIssues] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const parse = (): { document?: Record<string, unknown>; yaml_text?: string } => {
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>;
      if (parsed && typeof parsed === "object") return { document: parsed };
    } catch {
      // not JSON: the server owns YAML parsing, and it reports what is wrong
    }
    return { yaml_text: text };
  };

  const validate = useMutation({
    mutationFn: async () => validateAlertsWorkflow(parse(), scope),
    onSuccess: (response) => {
      setError(null);
      setIssues((response.issues ?? []).map((issue) => issue.message || issue.code || "issue"));
    },
    onError: (issue) => {
      setIssues(null);
      setError(alertsErrorMessage(issue, "the definition could not be validated"));
    },
  });

  const create = useMutation({
    mutationFn: async (activate: boolean) => {
      const created = await createAlertsWorkflow(
        { name, ...parse(), idempotency_key: idempotencyKey },
        scope,
      );
      if (activate) {
        await activateAlertsWorkflow(created.workflow_id, { scope });
      }
      return created.workflow_id;
    },
    onSuccess: (workflowId) => router.push(`/alerts/${workflowId}`),
    onError: (issue) => setError(alertsErrorMessage(issue, "creation failed")),
  });

  return (
    <Panel className="flex flex-col gap-3 p-5">
      <div className="flex items-center gap-2">
        <CodeIcon className="size-4" aria-hidden />
        <span className="text-sm font-medium">Definition (JSON or YAML)</span>
      </div>
      <p className="text-xs text-muted-foreground">
        This is the stored document. JSON is the canonical form; paste YAML if you prefer it and the
        server validates it the same way.
      </p>
      <textarea
        aria-label="Alert definition"
        className="min-h-[24rem] w-full rounded-lg border border-border/60 bg-background/60 p-3 font-mono text-xs"
        value={text}
        onChange={(event) => setText(event.target.value)}
        spellCheck={false}
      />
      {issues ? (
        <ul className="flex flex-col gap-1 text-xs text-amber-300">
          {issues.length === 0 ? <li>No issues found.</li> : issues.map((issue) => <li key={issue}>{issue}</li>)}
        </ul>
      ) : null}
      {error ? (
        <p className="text-xs text-rose-300" role="alert">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => validate.mutate()}>
          Validate
        </Button>
        <Button type="button" size="sm" variant="secondary" onClick={() => create.mutate(false)}>
          Save draft
        </Button>
        <Button type="button" size="sm" onClick={() => create.mutate(true)}>
          Create and activate
        </Button>
      </div>
    </Panel>
  );
}
