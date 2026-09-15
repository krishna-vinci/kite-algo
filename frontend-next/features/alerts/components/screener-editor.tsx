"use client";

import { AlertCircleIcon, CheckIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import Link from "next/link";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { SectionLabel } from "@/components/operator/section-label";
import { ConditionEditor } from "@/features/alerts/components/condition-editor";
import { InstrumentPicker } from "@/features/alerts/components/instrument-picker";
import { OperatorIssueList } from "@/features/alerts/components/operator-issue-list";
import { UniverseTargetingEditor } from "@/features/alerts/components/universe-targeting-editor";
import { createAlertsWorkflow, patchAlertsWorkflow } from "@/features/alerts/api";
import {
  useAlertsCapabilities,
  useAlertsChannels,
} from "@/features/alerts/hooks/use-alerts-queries";
import {
  DURATION_CHOICES,
  FRESHNESS_CHOICES,
  SCREENER_TRIGGERS,
  attachmentSupportsRankBands,
  buildScreenerDocument,
  defaultRankBand,
  emptyAttachment,
  emptyScreenerDraft,
  screenerDraftIssues,
  type ScreenerAttachmentDraft,
  type ScreenerDraft,
  type ScreenerTrigger,
} from "@/features/alerts/lib/screener-authoring";
import { ApiClientError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

const STEPS = ["Coverage", "Clock", "Who qualifies", "Schedule", "Notifications", "Save"] as const;

type ScreenerEditorProps = Readonly<{
  scope: string | null;
  initialDraft?: ScreenerDraft;
  /**
   * The loaded document on the edit path. The form merges the fields it models
   * onto a clone of this, so unmodeled keys survive a save.
   */
  baseDocument?: Record<string, unknown> | null;
  edit?: { workflowId: string; expectedRevision: number };
}>;

export function ScreenerEditor({ scope, initialDraft, baseDocument, edit }: ScreenerEditorProps) {
  const router = useRouter();
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const channelsQuery = useAlertsChannels(scope);

  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<ScreenerDraft>(() => initialDraft ?? emptyScreenerDraft());
  const [conflict, setConflict] = useState(false);

  const capabilities = capabilitiesQuery.data?.capabilities;
  const channels = channelsQuery.data?.channels ?? [];
  const isEditing = Boolean(edit);

  const document = useMemo(
    () => buildScreenerDocument(draft, baseDocument),
    [draft, baseDocument],
  );
  const issues = useMemo(() => screenerDraftIssues(draft), [draft]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (edit) {
        await patchAlertsWorkflow(
          edit.workflowId,
          { document, expected_revision: edit.expectedRevision },
          scope,
        );
        return edit.workflowId;
      }
      const response = await createAlertsWorkflow(
        { name: draft.name, document, idempotency_key: crypto.randomUUID() },
        scope,
      );
      return response.workflow_id;
    },
    onMutate: () => setConflict(false),
    onSuccess: (workflowId) => router.push(`/alerts/screeners/${workflowId}`),
    onError: (error) => {
      if (error instanceof ApiClientError && error.status === 409) setConflict(true);
    },
  });

  if (capabilitiesQuery.isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  if (!capabilities) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Capabilities unavailable</AlertTitle>
        <AlertDescription>
          The screener editor is driven entirely by the backend&apos;s declared capabilities, so it
          cannot render without them.
        </AlertDescription>
      </Alert>
    );
  }

  const acceptedExchanges = capabilities.session_exchanges[draft.session] ?? [];
  const storedFields = capabilities.screener.stored_data_fields;
  const schedulerCalendars = capabilities.screener.schedule_calendars;
  const calendarBacked = schedulerCalendars.includes(draft.schedule.calendar);

  const updateAttachment = (index: number, next: ScreenerAttachmentDraft) =>
    setDraft({ ...draft, attachments: draft.attachments.map((a, i) => (i === index ? next : a)) });

  return (
    <div className="flex flex-col gap-6 pb-8">
      <SectionLabel
        eyebrow="Screeners"
        title={isEditing ? "Edit screener" : "New screener"}
        description="A scheduled ranked scan. Results persist as runs; attachments notify on the transitions you choose."
      />

      <div className="flex flex-wrap gap-2">
        {STEPS.map((label, index) => (
          <button
            key={label}
            type="button"
            aria-current={index === step ? "step" : undefined}
            onClick={() => setStep(index)}
            className={cn(
              "rounded-full border px-3 py-1 text-xs",
              index === step
                ? "border-primary/60 bg-primary/10 text-primary"
                : "border-border/60 text-muted-foreground hover:text-foreground",
            )}
          >
            {index + 1}. {label}
          </button>
        ))}
      </div>

      <div className="rounded-xl border border-border/70 bg-card/60 p-5">
        {step === 0 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">What should be scanned?</h3>
            <div role="radiogroup" aria-label="Coverage" className="flex flex-wrap gap-2">
              {([
                ["universe", "A universe"],
                ["instruments", "A fixed instrument list"],
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

            {draft.targeting === "universe" ? (
              <UniverseTargetingEditor
                scope={scope}
                value={draft.universe}
                onChange={(universe) => setDraft({ ...draft, universe })}
              />
            ) : (
              <>
                <InstrumentPicker
                  selected={draft.instruments}
                  onChange={(instruments) => setDraft({ ...draft, instruments })}
                  acceptedExchanges={acceptedExchanges}
                />
                <p className="text-xs text-muted-foreground">
                  A screener takes precedence in the document: a scan-and-rank workflow normally
                  uses a universe so membership follows the market.
                </p>
              </>
            )}
          </div>
        ) : null}

        {step === 1 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">Session, clock and timeframe</h3>
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor="screener-session">Session</Label>
                <Select
                  value={draft.session || undefined}
                  onValueChange={(session) => setDraft({ ...draft, session })}
                >
                  <SelectTrigger id="screener-session">
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
                <Label htmlFor="screener-clock">Clock</Label>
                <Select value={draft.clock} onValueChange={(clock) => setDraft({ ...draft, clock })}>
                  <SelectTrigger id="screener-clock">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {/* A screener scans stored history, so an ltp stage is
                        rejected by the compiler — offer it disabled rather
                        than let it fail on save. */}
                    {capabilities.clocks &&
                      Object.keys(capabilities.clocks)
                        .filter((clock) => clock !== "ltp")
                        .map((clock) => (
                          <SelectItem key={clock} value={clock}>
                            {clock}
                          </SelectItem>
                        ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="screener-timeframe">Timeframe</Label>
                <Select
                  value={draft.timeframe}
                  onValueChange={(timeframe) => setDraft({ ...draft, timeframe })}
                >
                  <SelectTrigger id="screener-timeframe">
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
            <p className="text-xs text-muted-foreground">
              Scan fields available in screener conditions: {storedFields.join(", ")}.
            </p>
          </div>
        ) : null}

        {step === 2 ? (
          <div className="flex flex-col gap-5">
            <div>
              <h3 className="text-sm font-semibold">Who qualifies</h3>
              <p className="mt-1 text-xs text-muted-foreground">
                Every condition must hold for an instrument to enter the ranking.
              </p>
              <div className="mt-3">
                <ConditionEditor
                  conditions={draft.conditions}
                  onChange={(conditions) => setDraft({ ...draft, conditions })}
                  capabilities={capabilities}
                />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor="rank-kind">Rank by</Label>
                <Select
                  value={draft.rank.by.kind}
                  onValueChange={(kind) =>
                    setDraft({
                      ...draft,
                      rank: { ...draft.rank, by: { kind: kind as "field" | "indicator", name: "" } },
                    })
                  }
                >
                  <SelectTrigger id="rank-kind">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="field">a field</SelectItem>
                    <SelectItem value="indicator">an indicator</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="rank-name">
                  {draft.rank.by.kind === "indicator" ? "Indicator" : "Field"}
                </Label>
                {draft.rank.by.kind === "field" ? (
                  <Select
                    value={draft.rank.by.name}
                    onValueChange={(name) =>
                      setDraft({ ...draft, rank: { ...draft.rank, by: { kind: "field", name } } })
                    }
                  >
                    <SelectTrigger id="rank-name">
                      <SelectValue placeholder="Select field" />
                    </SelectTrigger>
                    <SelectContent>
                      {[...new Set([...storedFields, ...capabilities.fields])].map((field) => (
                        <SelectItem key={field} value={field}>
                          {field}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <Input
                    id="rank-name"
                    value={draft.rank.by.name}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        rank: { ...draft.rank, by: { kind: "indicator", name: event.target.value } },
                      })
                    }
                  />
                )}
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="rank-direction">Direction</Label>
                <Select
                  value={draft.rank.direction}
                  onValueChange={(direction) =>
                    setDraft({ ...draft, rank: { ...draft.rank, direction: direction as "desc" | "asc" } })
                  }
                >
                  <SelectTrigger id="rank-direction">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="desc">highest first</SelectItem>
                    <SelectItem value="asc">lowest first</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor="top-n">Ranked results kept (top_n)</Label>
                <Input
                  id="top-n"
                  type="number"
                  value={draft.top_n}
                  onChange={(event) => setDraft({ ...draft, top_n: Number(event.target.value) })}
                />
              </div>
              <p className="self-end text-xs text-muted-foreground">
                Tie-break is fixed by the server: {capabilities.screener.tie_break}. Equal scores
                therefore still have one stable order.
              </p>
            </div>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">Schedule</h3>
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor="schedule-every">Run every</Label>
                <Select
                  value={draft.schedule.every}
                  onValueChange={(every) =>
                    setDraft({ ...draft, schedule: { ...draft.schedule, every } })
                  }
                >
                  <SelectTrigger id="schedule-every">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {DURATION_CHOICES.map((choice) => (
                      <SelectItem key={choice.value} value={choice.value}>
                        {choice.label}
                      </SelectItem>
                    ))}
                    {/* The backend accepts any 5m..31d, which can include a
                        duration the fixed list does not name (e.g. "2h"). Show
                        the loaded value rather than an empty box. */}
                    {DURATION_CHOICES.every((choice) => choice.value !== draft.schedule.every) ? (
                      <SelectItem value={draft.schedule.every}>
                        {draft.schedule.every} (current)
                      </SelectItem>
                    ) : null}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="schedule-calendar">Calendar</Label>
                <Select
                  value={draft.schedule.calendar}
                  onValueChange={(calendar) =>
                    setDraft({ ...draft, schedule: { ...draft.schedule, calendar } })
                  }
                >
                  <SelectTrigger id="schedule-calendar">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {schedulerCalendars.map((calendar) => (
                      <SelectItem key={calendar} value={calendar}>
                        {calendar}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="schedule-at">At</Label>
                <Select
                  value={draft.schedule.at || "session_close"}
                  onValueChange={(at) => setDraft({ ...draft, schedule: { ...draft.schedule, at } })}
                >
                  <SelectTrigger id="schedule-at">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="session_close">session close</SelectItem>
                    <SelectItem value="09:15">09:15 IST</SelectItem>
                    <SelectItem value="12:00">12:00 IST</SelectItem>
                    <SelectItem value="15:15">15:15 IST</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* The backend's own reason, shown rather than discovered at save
                time. MCX/currency have no session calendar to bucket by. */}
            {!calendarBacked ? (
              <Alert variant="destructive">
                <AlertCircleIcon />
                <AlertTitle>This calendar cannot be scheduled</AlertTitle>
                <AlertDescription>
                  Only {schedulerCalendars.join(", ")} is calendar-backed. {capabilities.screener.schedule_note}
                </AlertDescription>
              </Alert>
            ) : (
              <p className="text-xs text-muted-foreground">{capabilities.screener.schedule_note}</p>
            )}

            <div className="flex flex-col gap-1 sm:max-w-sm">
              <Label htmlFor="freshness">Downstream freshness limit</Label>
              <Select
                value={String(draft.freshness_limit_s)}
                onValueChange={(value) =>
                  setDraft({ ...draft, freshness_limit_s: Number(value) })
                }
              >
                <SelectTrigger id="freshness">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {FRESHNESS_CHOICES.map((choice) => (
                    <SelectItem key={choice.seconds} value={String(choice.seconds)}>
                      {choice.label}
                    </SelectItem>
                  ))}
                  {FRESHNESS_CHOICES.every((choice) => choice.seconds !== draft.freshness_limit_s) ? (
                    <SelectItem value={String(draft.freshness_limit_s)}>
                      {Math.round(draft.freshness_limit_s / 3600)}h (current)
                    </SelectItem>
                  ) : null}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                How long a screener-backed universe stays usable after its last complete run. Past
                this, dependent alerts are unknown rather than quietly stale.
              </p>
            </div>
          </div>
        ) : null}

        {step === 4 ? (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold">Notification attachments</h3>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  setDraft({ ...draft, attachments: [...draft.attachments, emptyAttachment(draft.attachments.length)] })
                }
              >
                <PlusIcon className="size-4" aria-hidden />
                Add attachment
              </Button>
            </div>

            {draft.attachments.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No attachments. The scan still runs and its results are inspectable — it just does
                not notify.
              </p>
            ) : null}

            {draft.attachments.map((attachment, index) => (
              <div key={index} className="flex flex-col gap-3 rounded-lg border border-border/60 p-4">
                <div className="flex flex-wrap items-end gap-3">
                  <div className="flex flex-col gap-1">
                    <Label htmlFor={`att-id-${index}`}>Id</Label>
                    <Input
                      id={`att-id-${index}`}
                      value={attachment.id}
                      onChange={(event) => updateAttachment(index, { ...attachment, id: event.target.value })}
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <Label htmlFor={`att-trigger-${index}`}>Notify when</Label>
                    <Select
                      value={attachment.trigger}
                      onValueChange={(trigger) => {
                        const next = trigger as ScreenerTrigger;
                        // Switching to top_n seeds the parser's own default
                        // band, so the form never shows an empty box that
                        // implies "no hysteresis".
                        const band =
                          next === "top_n"
                            ? (() => {
                                const seed = attachment.top_n ?? draft.top_n;
                                const { entry_rank, exit_rank } = defaultRankBand(seed);
                                return { top_n: attachment.top_n ?? seed, entry_rank, exit_rank };
                              })()
                            : {};
                        updateAttachment(index, {
                          ...attachment,
                          trigger: next,
                          ...(next === "rank_delta" && attachment.rank_delta === null
                            ? { rank_delta: 5 }
                            : {}),
                          ...band,
                        });
                      }}
                    >
                      <SelectTrigger id={`att-trigger-${index}`} className="w-[14rem]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {SCREENER_TRIGGERS.map((trigger) => (
                          <SelectItem
                            key={trigger}
                            value={trigger}
                            disabled={!capabilities.screener.attachment_triggers.includes(trigger)}
                          >
                            {trigger}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove attachment ${attachment.id}`}
                    onClick={() =>
                      setDraft({ ...draft, attachments: draft.attachments.filter((_, i) => i !== index) })
                    }
                  >
                    <Trash2Icon className="size-4" aria-hidden />
                  </Button>
                </div>

                <div className="flex flex-wrap gap-3">
                  {channels.length === 0 ? (
                    <p className="text-xs text-rose-300">
                      No notification channels are configured.{" "}
                      <Link className="underline" href="/alerts/operations">
                        Add one under Alerts → Operations
                      </Link>{" "}
                      — an attachment with no channel can never notify.
                    </p>
                  ) : (
                    channels.map((channel) => {
                      const checked = attachment.channels.includes(channel.name);
                      return (
                        <label key={channel.channel_id} className="flex items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() =>
                              updateAttachment(index, {
                                ...attachment,
                                channels: checked
                                  ? attachment.channels.filter((name) => name !== channel.name)
                                  : [...attachment.channels, channel.name],
                              })
                            }
                          />
                          {channel.name}
                          <Badge variant="outline">{channel.provider}</Badge>
                        </label>
                      );
                    })
                  )}
                </div>

                <div className="grid gap-3 sm:grid-cols-4">
                  {attachment.trigger === "top_n" ? (
                    <div className="flex flex-col gap-1">
                      <Label htmlFor={`att-topn-${index}`}>Top n</Label>
                      <Input
                        id={`att-topn-${index}`}
                        type="number"
                        value={attachment.top_n ?? ""}
                        onChange={(event) => {
                          const topN = Number(event.target.value);
                          const { entry_rank, exit_rank } = defaultRankBand(topN);
                          updateAttachment(index, { ...attachment, top_n: topN, entry_rank, exit_rank });
                        }}
                      />
                    </div>
                  ) : null}
                  {attachment.trigger === "rank_delta" ? (
                    <div className="flex flex-col gap-1">
                      <Label htmlFor={`att-delta-${index}`}>Rank move</Label>
                      <Input
                        id={`att-delta-${index}`}
                        type="number"
                        value={attachment.rank_delta ?? ""}
                        onChange={(event) =>
                          updateAttachment(index, { ...attachment, rank_delta: Number(event.target.value) })
                        }
                      />
                    </div>
                  ) : null}
                  {attachmentSupportsRankBands(attachment.trigger) ? (
                    <>
                      <div className="flex flex-col gap-1">
                        <Label htmlFor={`att-entry-${index}`}>Enter rank ≤</Label>
                        <Input
                          id={`att-entry-${index}`}
                          type="number"
                          value={attachment.entry_rank ?? ""}
                          onChange={(event) =>
                            updateAttachment(index, {
                              ...attachment,
                              entry_rank: Number(event.target.value),
                            })
                          }
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <Label htmlFor={`att-exit-${index}`}>Exit rank &gt;</Label>
                        <Input
                          id={`att-exit-${index}`}
                          type="number"
                          value={attachment.exit_rank ?? ""}
                          onChange={(event) =>
                            updateAttachment(index, {
                              ...attachment,
                              exit_rank: Number(event.target.value),
                            })
                          }
                        />
                      </div>
                    </>
                  ) : null}
                  <div className="flex flex-col gap-1">
                    <Label htmlFor={`att-exit-after-${index}`}>Exit after (absent runs)</Label>
                    <Input
                      id={`att-exit-after-${index}`}
                      type="number"
                      value={attachment.exit_after ?? ""}
                      placeholder="1"
                      onChange={(event) =>
                        updateAttachment(index, {
                          ...attachment,
                          exit_after: event.target.value === "" ? null : Number(event.target.value),
                        })
                      }
                    />
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <Switch
                    checked={attachment.initial_match}
                    onCheckedChange={(initial_match) =>
                      updateAttachment(index, { ...attachment, initial_match })
                    }
                  />
                  <span className="text-sm">
                    Notify on the first complete run too
                    <span className="block text-xs text-muted-foreground">
                      Off (recommended): the first complete run sets a silent baseline, so you are
                      told about changes rather than a flood of everything already matching.
                    </span>
                  </span>
                </div>
              </div>
            ))}
          </div>
        ) : null}

        {step === 5 ? (
          <div className="flex flex-col gap-4">
            <h3 className="text-sm font-semibold">Name and save</h3>
            <div className="flex flex-col gap-1 sm:max-w-sm">
              <Label htmlFor="screener-name">Name</Label>
              <Input
                id="screener-name"
                value={draft.name}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                placeholder="nifty-momentum-scan"
              />
            </div>

            {issues.length > 0 ? (
              <OperatorIssueList
                issues={issues.map((message) => ({
                  where: "form",
                  code: "bad_value",
                  message,
                  severity: "error",
                }))}
                bare
              />
            ) : (
              <p className="flex items-center gap-2 text-sm text-emerald-300">
                <CheckIcon className="size-4" /> No client-side problems found. The server still
                validates on save.
              </p>
            )}

            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                disabled={!draft.name || issues.length > 0 || saveMutation.isPending}
                onClick={() => saveMutation.mutate()}
              >
                {isEditing ? "Save as new draft revision" : "Save as draft"}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Saving creates a draft. A screener never activates an alert by itself; its attachments
              notify from the runs that follow.
            </p>

            {conflict ? (
              <Alert variant="destructive">
                <AlertCircleIcon />
                <AlertTitle>Someone changed this screener first</AlertTitle>
                <AlertDescription>
                  Your edit was not applied and nothing was overwritten. Reload the latest revision
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
        <Button type="button" variant="ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>
          Back
        </Button>
        {step < STEPS.length - 1 ? (
          <Button type="button" onClick={() => setStep(step + 1)}>
            Next
          </Button>
        ) : null}
      </div>

      <details className="rounded-xl border border-border/70 bg-card/60 p-5">
        <summary className="cursor-pointer text-sm text-muted-foreground">
          Document preview (canonical shape)
        </summary>
        <pre className="mt-3 overflow-auto text-xs">{JSON.stringify(document, null, 2)}</pre>
      </details>
    </div>
  );
}
