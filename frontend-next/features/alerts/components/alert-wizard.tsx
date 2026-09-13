"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { AlertCircleIcon, CheckIcon, InfoIcon, TriangleAlertIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { SectionLabel } from "@/components/operator/section-label";
import { ConditionEditor } from "@/features/alerts/components/condition-editor";
import { InstrumentPicker } from "@/features/alerts/components/instrument-picker";
import { OperatorIssueList } from "@/features/alerts/components/operator-issue-list";
import { UniverseTargetingEditor } from "@/features/alerts/components/universe-targeting-editor";
import { createAlertsWorkflow, fetchAlertsChannels, patchAlertsWorkflow, previewAlertsWorkflow, validateAlertsWorkflow } from "@/features/alerts/api";
import { useAlertsCapabilities } from "@/features/alerts/hooks/use-alerts-queries";
import {
  buildDocument,
  emptyDraft,
  incompatibleInstruments,
  levelOnlyWarning,
  universeDraftIssues,
  type AlertDraft,
} from "@/features/alerts/lib/authoring";
import type { AlertsPreviewResponse, AlertsValidateResponse } from "@/features/alerts/types";
import { ApiClientError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

const STEPS = [
  "Instruments",
  "Session & clock",
  "Conditions",
  "Trigger & limits",
  "Notifications",
  "Validate & preview",
  "Save",
] as const;

function StepperNav({
  step,
  onSelect,
}: Readonly<{ step: number; onSelect: (step: number) => void }>) {
  return (
    <ol className="flex flex-wrap gap-2" aria-label="Creation steps">
      {STEPS.map((label, index) => (
        <li key={label}>
          <button
            type="button"
            onClick={() => onSelect(index)}
            aria-current={index === step ? "step" : undefined}
            className={cn(
              "rounded-full border px-3 py-1 text-xs transition-colors",
              index === step
                ? "border-primary/60 bg-primary/10 text-primary"
                : "border-border/60 text-muted-foreground hover:text-foreground",
            )}
          >
            {index + 1}. {label}
          </button>
        </li>
      ))}
    </ol>
  );
}

export type AlertWizardProps = Readonly<{
  scope: string | null;
  /** Pre-populated draft for the edit path. */
  initialDraft?: AlertDraft;
  /**
   * The loaded document on the edit path. The form merges the fields it models
   * onto a clone of this, so keys the editor does not model (`expires_at`,
   * `message`, `session_cap_reset`, indicator `source`/`offset`, ...) survive a
   * save untouched and a no-op save cannot move the canonical hash.
   */
  baseDocument?: Record<string, unknown> | null;
  /**
   * Present when editing an existing workflow: saving PATCHes a new draft
   * revision under `expected_revision` instead of creating a workflow, so the
   * optimistic-concurrency check is always engaged.
   */
  edit?: { workflowId: string; expectedRevision: number };
}>;

export function AlertWizard({ scope, initialDraft, baseDocument, edit }: AlertWizardProps) {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<AlertDraft>(() => initialDraft ?? emptyDraft());
  const [observationsText, setObservationsText] = useState("");
  const [observationsError, setObservationsError] = useState<string | null>(null);
  const [validation, setValidation] = useState<AlertsValidateResponse | null>(null);
  const [preview, setPreview] = useState<AlertsPreviewResponse | null>(null);

  const capabilitiesQuery = useAlertsCapabilities(scope);
  const channelsQuery = useQuery({
    queryKey: ["alerts", "channels", scope],
    queryFn: () => fetchAlertsChannels(scope),
    enabled: Boolean(scope),
  });

  const capabilities = capabilitiesQuery.data?.capabilities;
  const channels = channelsQuery.data?.channels ?? [];

  const document = useMemo(
    () => (capabilities ? buildDocument(draft, baseDocument) : null),
    [draft, baseDocument, capabilities],
  );

  const acceptedExchanges = useMemo(() => {
    if (!capabilities || !draft.session) return null;
    return capabilities.session_exchanges[draft.session] ?? [];
  }, [capabilities, draft.session]);

  const incompatible = useMemo(() => {
    if (!capabilities || !draft.session) return [];
    return incompatibleInstruments(draft.session, draft.instruments, capabilities.session_exchanges);
  }, [capabilities, draft.instruments, draft.session]);

  const inlineWarning = useMemo(() => {
    if (!capabilities) return null;
    return levelOnlyWarning(draft.conditions, draft.alert.trigger, capabilities.operators);
  }, [capabilities, draft.conditions, draft.alert.trigger]);

  const validateMutation = useMutation({
    mutationFn: () => validateAlertsWorkflow({ document: document ?? {} }, scope),
    onSuccess: setValidation,
  });

  const previewMutation = useMutation({
    mutationFn: (observations: unknown[]) =>
      previewAlertsWorkflow({ document: document ?? {}, observations }, scope),
    onSuccess: setPreview,
  });

  const isEditing = Boolean(edit);
  const [conflict, setConflict] = useState(false);

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (edit) {
        const response = await patchAlertsWorkflow(
          edit.workflowId,
          { document: document ?? {}, expected_revision: edit.expectedRevision },
          scope,
        );
        return { workflowId: edit.workflowId, response };
      }
      const response = await createAlertsWorkflow(
        { name: draft.name, document: document ?? {}, idempotency_key: crypto.randomUUID() },
        scope,
      );
      return { workflowId: response.workflow_id, response };
    },
    onMutate: () => setConflict(false),
    onSuccess: ({ workflowId }) => {
      router.push(`/alerts/${workflowId}`);
    },
    onError: (error) => {
      // 409 is a recoverable state, not a failure: the definition changed under
      // us, so the operator reloads and re-applies rather than losing the edit.
      if (error instanceof ApiClientError && error.status === 409) {
        setConflict(true);
      }
    },
  });

  if (capabilitiesQuery.isLoading) {
    return <Skeleton className="h-96 w-full rounded-xl" />;
  }

  if (capabilitiesQuery.error || !capabilities) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Could not load capabilities</AlertTitle>
        <AlertDescription>
          The authoring form is driven entirely by the backend&apos;s declared capabilities, so it
          cannot render without them.{" "}
          {capabilitiesQuery.error instanceof Error ? capabilitiesQuery.error.message : ""}
        </AlertDescription>
      </Alert>
    );
  }

  const canLeaveInstruments =
    draft.targeting === "universe"
      ? universeDraftIssues(draft.universe).length === 0
      : draft.instruments.length > 0 && incompatible.length === 0;

  const runPreview = () => {
    let observations: unknown[] = [];
    if (observationsText.trim()) {
      try {
        const parsed = JSON.parse(observationsText);
        if (!Array.isArray(parsed)) throw new Error("observations must be a JSON array");
        observations = parsed;
      } catch (error) {
        setObservationsError(error instanceof Error ? error.message : "invalid JSON");
        return;
      }
    }
    setObservationsError(null);
    previewMutation.mutate(observations);
  };

  return (
    <div className="flex flex-col gap-6 pb-8">
      <SectionLabel
        eyebrow="Alerts"
        title={isEditing ? "Edit alert" : "New alert"}
        description="Every field below is driven by the backend's declared capabilities. Alerts never place orders."
      />

      <StepperNav step={step} onSelect={setStep} />

      <div className="rounded-xl border border-border/70 bg-card/60 p-5">
        {step === 0 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">1. What should this watch?</h3>

            {/* Two ways to say what is covered, never both at once: the
                document carries EITHER an instrument list or a universe
                expression, and offering both would make which one wins
                ambiguous. */}
            <div role="radiogroup" aria-label="Coverage" className="flex flex-wrap gap-2">
              {([
                ["instruments", "Specific instruments"],
                ["universe", "A universe"],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={draft.targeting === value}
                  onClick={() => setDraft({ ...draft, targeting: value })}
                  className={cn(
                    "rounded-full border px-3 py-1 text-xs",
                    draft.targeting === value
                      ? "border-primary/60 bg-primary/10 text-primary"
                      : "border-border/60 text-muted-foreground hover:text-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>

            {draft.targeting === "instruments" ? (
              <>
                <InstrumentPicker
                  selected={draft.instruments}
                  onChange={(instruments) => setDraft({ ...draft, instruments })}
                  acceptedExchanges={acceptedExchanges}
                />
                {draft.session === "" ? (
                  <p className="text-xs text-muted-foreground">
                    Choose a session in the next step to see which instruments it accepts.
                  </p>
                ) : null}
              </>
            ) : (
              <>
                <UniverseTargetingEditor
                  scope={scope}
                  value={draft.universe}
                  onChange={(universe) => setDraft({ ...draft, universe })}
                />
                <p className="text-xs text-muted-foreground">
                  A universe is resolved when the alert is activated (and again when it is
                  re-resolved), so membership can change without editing the alert. The
                  instrument-by-instrument session rule is checked at resolution time for each
                  member.
                </p>
              </>
            )}

            {draft.targeting === "instruments" && incompatible.length > 0 ? (
              <Alert variant="destructive">
                <AlertCircleIcon />
                <AlertTitle>Session does not support these instruments</AlertTitle>
                <AlertDescription>
                  Session “{draft.session}” accepts {acceptedExchanges?.join(", ") || "no exchanges"}.
                  Remove or replace:{" "}
                  {incompatible.map((item) => `${item.instrumentKey} (${item.exchange})`).join(", ")}
                </AlertDescription>
              </Alert>
            ) : null}
          </div>
        ) : null}

        {step === 1 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">2. Session, clock and timeframe</h3>
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor="session">Session</Label>
                <Select value={draft.session || undefined} onValueChange={(session) => setDraft({ ...draft, session })}>
                  <SelectTrigger id="session">
                    <SelectValue placeholder="Select session" />
                  </SelectTrigger>
                  <SelectContent>
                    {capabilities.sessions.map((session) => (
                      <SelectItem key={session} value={session}>
                        {session}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="flex flex-col gap-1">
                <Label htmlFor="clock">Evaluation clock</Label>
                <Select value={draft.clock} onValueChange={(clock) => setDraft({ ...draft, clock })}>
                  <SelectTrigger id="clock">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {Object.entries(capabilities.clocks).map(([name, spec]) => (
                      <SelectItem key={name} value={name}>
                        {name} <span className="text-muted-foreground">· {spec.latency}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {capabilities.clocks[draft.clock]?.source}
                </p>
              </div>

              <div className="flex flex-col gap-1">
                <Label htmlFor="timeframe">Timeframe</Label>
                <Select value={draft.timeframe} onValueChange={(timeframe) => setDraft({ ...draft, timeframe })}>
                  <SelectTrigger id="timeframe">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {capabilities.timeframes.map((timeframe) => (
                      <SelectItem key={timeframe} value={timeframe}>
                        {timeframe}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {Object.entries(capabilities.clock_aliases).length > 0 ? (
              <p className="text-xs text-muted-foreground">
                Aliases:{" "}
                {Object.entries(capabilities.clock_aliases)
                  .map(([alias, target]) => `${alias} = ${target}`)
                  .join(", ")}
              </p>
            ) : null}
          </div>
        ) : null}

        {step === 2 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">3. Conditions</h3>
            <ConditionEditor
              title="All of these (AND)"
              conditions={draft.conditions}
              onChange={(conditions) => setDraft({ ...draft, conditions })}
              capabilities={capabilities}
            />

            <details className="rounded-lg border border-border/60 p-3">
              <summary className="cursor-pointer text-sm">
                Any of / none of (optional)
                {draft.anyConditions.length + draft.notConditions.length > 0 ? (
                  <span className="ml-2 text-xs text-muted-foreground">
                    {draft.anyConditions.length} any, {draft.notConditions.length} none
                  </span>
                ) : null}
              </summary>
              <div className="mt-3 flex flex-col gap-4">
                <p className="text-xs text-muted-foreground">
                  These groups use three-valued logic: an unavailable value makes the condition
                  UNKNOWN, not false. A rule can therefore not trigger without being false.
                </p>
                <ConditionEditor
                  title="Any of these (OR)"
                  allowEmpty
                  addLabel="Add any condition"
                  conditions={draft.anyConditions}
                  onChange={(anyConditions) => setDraft({ ...draft, anyConditions })}
                  capabilities={capabilities}
                />
                <ConditionEditor
                  title="None of these (NOT)"
                  allowEmpty
                  addLabel="Add excluded condition"
                  conditions={draft.notConditions}
                  onChange={(notConditions) => setDraft({ ...draft, notConditions })}
                  capabilities={capabilities}
                />
              </div>
            </details>

            <div className="flex flex-col gap-1 sm:max-w-xs">
              <Label htmlFor="consecutive-bars">Require consecutive completed bars (optional)</Label>
              <Input
                id="consecutive-bars"
                type="number"
                min={1}
                max={capabilities.limits.max_consecutive_bars}
                value={draft.consecutiveBars ?? ""}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    consecutiveBars:
                      event.target.value === "" ? null : Number(event.target.value),
                  })
                }
              />
              <p className="text-xs text-muted-foreground">
                The whole “all of these” group must hold for N consecutive completed bars. Valid
                only on the candle-close clock, and not with a sequence or breadth condition.
                Server-validated.
              </p>
            </div>

            <details className="rounded-lg border border-border/60 p-3">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                Not available in this editor
              </summary>
              <ul className="mt-2 flex flex-col gap-1 text-xs text-muted-foreground">
                <li>
                  Simultaneous breadth —{" "}
                  {capabilities.breadth_modes.simultaneous?.implemented === false
                    ? "reported by the server as not implemented"
                    : "available"}
                  . {capabilities.breadth_semantics}
                </li>
                <li>Dynamic / indicator hysteresis — {capabilities.hysteresis.threshold}</li>
              </ul>
            </details>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">4. Trigger, rearm and limits</h3>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor="trigger">Trigger</Label>
                <Select
                  value={draft.alert.trigger}
                  onValueChange={(trigger) => setDraft({ ...draft, alert: { ...draft.alert, trigger } })}
                >
                  <SelectTrigger id="trigger">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {capabilities.triggers.map((trigger) => (
                      <SelectItem key={trigger} value={trigger}>
                        {trigger}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="flex flex-col gap-1">
                <Label htmlFor="cooldown">Cooldown (seconds)</Label>
                <Input
                  id="cooldown"
                  type="number"
                  min={0}
                  value={draft.alert.cooldown_s ?? ""}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      alert: {
                        ...draft.alert,
                        cooldown_s: event.target.value === "" ? null : Number(event.target.value),
                      },
                    })
                  }
                />
              </div>

              {draft.alert.trigger === "reminder" ? (
                <div className="flex flex-col gap-1">
                  <Label htmlFor="reminder">Reminder interval (seconds)</Label>
                  <Input
                    id="reminder"
                    type="number"
                    min={1}
                    value={draft.alert.reminder_interval_s ?? ""}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        alert: {
                          ...draft.alert,
                          reminder_interval_s: event.target.value === "" ? null : Number(event.target.value),
                        },
                      })
                    }
                  />
                </div>
              ) : null}

              <div className="flex flex-col gap-1">
                <Label htmlFor="max-per-session">Max notifications per session</Label>
                <Input
                  id="max-per-session"
                  type="number"
                  min={1}
                  max={capabilities.limits.max_per_session}
                  value={draft.alert.max_per_session ?? ""}
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
                <p className="text-xs text-muted-foreground">{capabilities.session_cap_note}</p>
              </div>

              <div className="flex flex-col gap-1">
                <Label htmlFor="rearm-level">Rearm level (optional)</Label>
                <Input
                  id="rearm-level"
                  type="number"
                  step="any"
                  value={draft.alert.rearm_level ?? ""}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      alert: {
                        ...draft.alert,
                        rearm_level: event.target.value === "" ? null : Number(event.target.value),
                      },
                    })
                  }
                />
                <p className="text-xs text-muted-foreground">
                  After firing, the rule re-arms only once the value moves back past this level.
                </p>
              </div>

              <div className="flex flex-col gap-1">
                <Label htmlFor="rearm-direction">Rearm direction (optional)</Label>
                <Select
                  value={draft.alert.rearm_direction ?? "unset"}
                  onValueChange={(direction) =>
                    setDraft({
                      ...draft,
                      alert: {
                        ...draft.alert,
                        rearm_direction: direction === "unset" ? null : direction,
                      },
                    })
                  }
                >
                  <SelectTrigger id="rearm-direction">
                    <SelectValue placeholder="Not set" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="unset">Not set</SelectItem>
                    <SelectItem value="above">above</SelectItem>
                    <SelectItem value="below">below</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  The server requires the level and direction together; a value on its own is
                  rejected on validate.
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <Switch
                id="notify-if-true"
                checked={draft.alert.notify_if_already_true}
                onCheckedChange={(checked) =>
                  setDraft({ ...draft, alert: { ...draft.alert, notify_if_already_true: checked } })
                }
              />
              <Label htmlFor="notify-if-true">Notify if already true on activation</Label>
            </div>
            <p className="text-xs text-muted-foreground">
              By default a fresh activation is silent: an alert whose condition is already true
              initializes without notifying.
            </p>

            {inlineWarning ? (
              <Alert>
                <TriangleAlertIcon />
                <AlertTitle>This rule may never notify</AlertTitle>
                <AlertDescription>{inlineWarning}</AlertDescription>
              </Alert>
            ) : null}
          </div>
        ) : null}

        {step === 4 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">5. Notification destinations</h3>
            {channels.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No channels are configured for this scope. Add one under Alerts → Operations
                before this alert can notify.
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {channels.map((channel) => {
                  const checked = draft.alert.channels.includes(channel.name);
                  return (
                    <li key={channel.channel_id} className="flex items-center gap-2">
                      <Checkbox
                        id={`channel-${channel.channel_id}`}
                        checked={checked}
                        onCheckedChange={(next) =>
                          setDraft({
                            ...draft,
                            alert: {
                              ...draft.alert,
                              channels:
                                next === true
                                  ? [...draft.alert.channels, channel.name]
                                  : draft.alert.channels.filter((name) => name !== channel.name),
                            },
                          })
                        }
                      />
                      <label htmlFor={`channel-${channel.channel_id}`} className="text-sm">
                        {channel.name}{" "}
                        <span className="text-muted-foreground">· {channel.provider}</span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        ) : null}

        {step === 5 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">6. Validate and preview</h3>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => validateMutation.mutate()}
                disabled={!document || validateMutation.isPending}
              >
                {validateMutation.isPending ? "Validating…" : "Validate"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={runPreview}
                disabled={!document || previewMutation.isPending}
              >
                {previewMutation.isPending ? "Running…" : "Run preview"}
              </Button>
            </div>

            <div className="flex flex-col gap-1">
              <Label htmlFor="observations">Preview samples (JSON array, optional)</Label>
              <Textarea
                id="observations"
                rows={4}
                value={observationsText}
                onChange={(event) => setObservationsText(event.target.value)}
                placeholder={'[{"instrument_key":"NSE:RELIANCE","ts":"2026-09-11T09:30:00Z","close":2900.0}]'}
                className="font-mono text-xs"
              />
              <p className="text-xs text-muted-foreground">
                Preview does not fetch market data — you supply the samples. With none, preview
                reports that nothing was evaluated rather than guessing.
              </p>
              {observationsError ? (
                <p className="text-xs text-rose-300">{observationsError}</p>
              ) : null}
            </div>

            {validation ? (
              <div className="flex flex-col gap-2 rounded-lg border border-border/60 p-3">
                <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
                  Validation
                </p>
                {validation.ok && validation.issues.length === 0 ? (
                  <p className="flex items-center gap-2 text-sm text-emerald-300">
                    <CheckIcon className="size-4" /> Valid, no issues.
                  </p>
                ) : null}
                <OperatorIssueList issues={validation.issues} bare />
              </div>
            ) : null}

            {preview ? (
              <div className="flex flex-col gap-2 rounded-lg border border-border/60 p-3">
                <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">
                  Preview
                </p>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
                  <div>
                    <dt className="text-xs text-muted-foreground">evaluation</dt>
                    <dd>{preview.evaluation ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">warmup bars</dt>
                    <dd>{preview.warmup_bars ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">observations</dt>
                    <dd>{preview.evaluated_observations ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">would fire</dt>
                    <dd>{preview.would_fire?.length ?? 0}</dd>
                  </div>
                </dl>
                {preview.unknown_reasons && preview.unknown_reasons.length > 0 ? (
                  <p className="text-sm text-amber-300">
                    Unknown reasons: {preview.unknown_reasons.join(", ")}
                  </p>
                ) : null}
                {preview.note ? (
                  <p className="flex items-start gap-2 text-xs text-muted-foreground">
                    <InfoIcon className="mt-0.5 size-3 shrink-0" aria-hidden />
                    {preview.note}
                  </p>
                ) : null}
              </div>
            ) : null}

            <details className="rounded-lg border border-border/60 p-3">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                Canonical document
              </summary>
              <pre className="mt-2 overflow-auto text-xs">
                {JSON.stringify(document, null, 2)}
              </pre>
            </details>
          </div>
        ) : null}

        {step === 6 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">7. Save</h3>
            <div className="flex flex-col gap-1">
              <Label htmlFor="alert-name">Name</Label>
              <Input
                id="alert-name"
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                placeholder="reliance-breakout"
              />
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={!draft.name || saveMutation.isPending}
                onClick={() => saveMutation.mutate()}
              >
                {isEditing ? "Save as new draft revision" : "Save as draft"}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {isEditing
                ? "Saving adds a draft revision under an optimistic-concurrency check (expected_revision). It does not activate it."
                : "Saving creates the workflow as a draft. Activation is a separate step that re-validates the stored revision and materializes subscriptions — and is silent by design."}
            </p>

            {conflict ? (
              <Alert variant="destructive">
                <AlertCircleIcon />
                <AlertTitle>Someone changed this alert first</AlertTitle>
                <AlertDescription>
                  The definition moved on since you opened it, so your edit was not applied —
                  nothing was overwritten.{" "}
                  <button
                    type="button"
                    className="underline"
                    onClick={() => router.refresh()}
                  >
                    Reload the latest revision
                  </button>{" "}
                  and re-apply your change.
                </AlertDescription>
              </Alert>
            ) : null}

            {saveMutation.error && !conflict ? (
              <Alert variant="destructive">
                <AlertCircleIcon />
                <AlertTitle>Could not save</AlertTitle>
                <AlertDescription>
                  {saveMutation.error instanceof Error ? saveMutation.error.message : "Unknown error"}
                </AlertDescription>
              </Alert>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="flex items-center justify-between gap-3">
        <Button
          type="button"
          variant="ghost"
          disabled={step === 0}
          onClick={() => setStep((current) => Math.max(0, current - 1))}
        >
          Back
        </Button>
        <Button
          type="button"
          disabled={step === STEPS.length - 1 || (step === 0 && !canLeaveInstruments)}
          onClick={() => setStep((current) => Math.min(STEPS.length - 1, current + 1))}
        >
          Next
        </Button>
      </div>
    </div>
  );
}
