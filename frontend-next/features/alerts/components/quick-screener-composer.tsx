"use client";

/**
 * The common screener: "scan [universe] for [qualification], rank by [field],
 * run [schedule]".
 *
 * One screen and two actions, with the same honest partial-failure behaviour as
 * the alert composer: if the create succeeds and the activation does not, the
 * draft is reported as saved rather than pretending it is running.
 *
 * The qualification row is the ordinary case (one field comparison). Adding
 * conditions, boolean groups, attachments beyond a single entry notification, a
 * candle clock other than daily, or anything the screener block does not model
 * is one click away in the step editor, which also routes unmodeled documents to
 * the lossless advanced editor.
 */

import Link from "next/link";
import { AlertCircleIcon, CheckIcon, InfoIcon, PlayIcon } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
import { ConditionEditor } from "@/features/alerts/components/condition-editor";
import { UniverseTargetingEditor } from "@/features/alerts/components/universe-targeting-editor";
import {
  activateAlertsWorkflow,
  createAlertsWorkflow,
  fetchAlertsUniverse,
} from "@/features/alerts/api";
import { useAlertsCapabilities, useAlertsChannels } from "@/features/alerts/hooks/use-alerts-queries";
import { exchangeOf, sessionForExchange, sessionLabel } from "@/features/alerts/lib/authoring";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";
import {
  DURATION_CHOICES,
  buildScreenerDocument,
  emptyScreenerDraft,
  screenerDraftIssues,
  type ScreenerDraft,
} from "@/features/alerts/lib/screener-authoring";
import { newIdempotencyKey } from "@/lib/ids";

type Outcome =
  | { kind: "draft-saved"; workflowId: string }
  | { kind: "activation-failed"; workflowId: string; reason: string };

function generatedName(universeNames: string[], condition: string, topN: number): string {
  const scope = universeNames.filter(Boolean).join(", ");
  if (!scope) return "";
  return `${scope} — ${condition} (top ${topN})`.slice(0, 120);
}

export function QuickScreenerComposer({ scope }: { scope: string | null }) {
  const router = useRouter();
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const channelsQuery = useAlertsChannels(scope);

  const [draft, setDraft] = useState<ScreenerDraft>(() => emptyScreenerDraft());
  const [notifyEntry, setNotifyEntry] = useState(false);
  const [sessionTouched, setSessionTouched] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [idempotencyKey] = useState(() => newIdempotencyKey("screener"));

  const capabilities = capabilitiesQuery.data?.capabilities;
  // The session must match the exchanges a universe resolves to, which the
  // operator should not have to work out. When the universe resolves to a single
  // exchange, take its session; otherwise leave the choice visible.
  const universeNamesEarly = draft.universe.union
    .map((ref) => ref.name.trim())
    .filter(Boolean);
  const universeProbe = useQuery({
    queryKey: ["alerts", "universe-session-probe", scope, universeNamesEarly[0] ?? ""],
    queryFn: () => fetchAlertsUniverse(universeNamesEarly[0], scope),
    enabled: Boolean(scope && universeNamesEarly.length === 1 && capabilities),
    staleTime: 5 * 60_000,
  });
  const enabledChannels = (channelsQuery.data?.channels ?? []).filter((c) => c.enabled);

  const inferredSession = (() => {
    const members = universeProbe.data?.latest_members;
    if (!members || !capabilities) return "";
    const exchanges = new Set(members.map((key) => exchangeOf(String(key))));
    if (exchanges.size !== 1) return "";
    return sessionForExchange([...exchanges][0], capabilities.session_exchanges);
  })();

  const conditionText = `${
    draft.conditions[0]?.left.kind === "field" ? draft.conditions[0].left.name : "value"
  } ${
    { gt: "above", gte: "at or above", lt: "below", lte: "at or below" }[
      draft.conditions[0]?.op ?? ""
    ] ?? draft.conditions[0]?.op
  } ${draft.conditions[0]?.right.kind === "constant" ? draft.conditions[0].right.value : ""}`;

  const effectiveSession = sessionTouched ? draft.session : inferredSession || draft.session;
  const effectiveDraft: ScreenerDraft = {
    ...draft,
    session: effectiveSession,
    name: draft.name.trim() || generatedName(universeNamesEarly, conditionText, draft.top_n),
    attachments:
      notifyEntry && enabledChannels.length
        ? [
            {
              id: "entry",
              trigger: "entry",
              channels: [enabledChannels[0].name],
              top_n: null,
              rank_delta: null,
              entry_rank: null,
              exit_rank: null,
              exit_after: null,
              initial_match: false,
              message: null,
            },
          ]
        : [],
  };

  const issues = screenerDraftIssues(effectiveDraft);
  const ready = issues.length === 0;

  const create = useMutation({
    mutationFn: async (mode: "draft" | "activate") => {
      const document = buildScreenerDocument(effectiveDraft);
      const created = await createAlertsWorkflow(
        { name: effectiveDraft.name, document, idempotency_key: idempotencyKey },
        scope,
      );
      if (mode === "draft") {
        return { kind: "draft-saved" as const, workflowId: created.workflow_id };
      }
      try {
        await activateAlertsWorkflow(created.workflow_id, { scope });
      } catch (error) {
        return {
          kind: "activation-failed" as const,
          workflowId: created.workflow_id,
          reason: alertsErrorMessage(error, "activation was refused"),
        };
      }
      router.push(`/alerts/screeners/${created.workflow_id}`);
      return { kind: "saved" as const, workflowId: created.workflow_id };
    },
    onSuccess: (result) => {
      if (result.kind !== "saved") setOutcome(result);
    },
  });

  if (capabilitiesQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading screener options…</p>;
  }
  if (capabilitiesQuery.error || !capabilities) {
    return (
      <Alert variant="destructive" role="alert">
        <AlertCircleIcon className="size-4" />
        <AlertTitle>Could not load screener options</AlertTitle>
        <AlertDescription>
          {alertsErrorMessage(capabilitiesQuery.error, "the server did not return screener options")}
        </AlertDescription>
      </Alert>
    );
  }

  const storedFields = capabilities.screener.stored_data_fields;
  const pending = create.isPending;

  return (
    <div className="flex flex-col gap-5 pb-8">
      <SectionLabel
        eyebrow="Screeners"
        title="New screener"
        description="Scan a universe, rank what qualifies, and run it on a schedule."
      />

      {outcome?.kind === "draft-saved" ? (
        <Alert role="status">
          <CheckIcon className="size-4" />
          <AlertTitle>Draft saved</AlertTitle>
          <AlertDescription>
            It will not run until you switch it on.{" "}
            <Link className="underline" href={`/alerts/screeners/${outcome.workflowId}`}>
              Open the screener
            </Link>
          </AlertDescription>
        </Alert>
      ) : null}
      {outcome?.kind === "activation-failed" ? (
        <Alert variant="destructive" role="alert">
          <AlertCircleIcon className="size-4" />
          <AlertTitle>Saved as a draft — activation failed</AlertTitle>
          <AlertDescription className="flex flex-col gap-2">
            <span>{outcome.reason}</span>
            <Link className="underline" href={`/alerts/screeners/${outcome.workflowId}`}>
              Open the draft to retry activation
            </Link>
          </AlertDescription>
        </Alert>
      ) : null}
      {create.error ? (
        <Alert variant="destructive" role="alert">
          <AlertCircleIcon className="size-4" />
          <AlertTitle>Could not save</AlertTitle>
          <AlertDescription>
            {alertsErrorMessage(create.error, "the server rejected the request")}
          </AlertDescription>
        </Alert>
      ) : null}

      <Panel className="flex flex-col gap-5 p-5">
        <div className="flex flex-col gap-2">
          <Label>Scan</Label>
          <UniverseTargetingEditor
            scope={scope}
            value={draft.universe}
            onChange={(universe) => setDraft({ ...draft, universe })}
          />
        </div>

        <div className="flex flex-col gap-2">
          <Label>Qualification</Label>
          <ConditionEditor
            conditions={draft.conditions}
            capabilities={capabilities}
            onChange={(conditions) => setDraft({ ...draft, conditions })}
          />
          <p className="text-xs text-muted-foreground">
            Daily candles are used, so a symbol without stored history is reported as unavailable
            rather than silently skipped.
          </p>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-rank-field">Rank by</Label>
            <Select
              value={draft.rank.by.name}
              onValueChange={(name) =>
                setDraft({ ...draft, rank: { ...draft.rank, by: { kind: "field", name } } })
              }
            >
              <SelectTrigger id="quick-rank-field">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(storedFields.length ? storedFields : capabilities.fields).map((field) => (
                  <SelectItem key={field} value={field}>
                    {field}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-rank-direction">Order</Label>
            <Select
              value={draft.rank.direction}
              onValueChange={(direction) =>
                setDraft({
                  ...draft,
                  rank: { ...draft.rank, direction: direction as "asc" | "desc" },
                })
              }
            >
              <SelectTrigger id="quick-rank-direction">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="desc">Highest first</SelectItem>
                <SelectItem value="asc">Lowest first</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-top-n">Keep</Label>
            <Input
              id="quick-top-n"
              type="number"
              min={1}
              max={1000}
              value={draft.top_n}
              onChange={(event) => setDraft({ ...draft, top_n: Number(event.target.value) })}
            />
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-schedule-every">Run every</Label>
            <Select
              value={draft.schedule.every}
              onValueChange={(every) =>
                setDraft({ ...draft, schedule: { ...draft.schedule, every } })
              }
            >
              <SelectTrigger id="quick-schedule-every">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DURATION_CHOICES.map((choice) => (
                  <SelectItem key={choice.value} value={choice.value}>
                    {choice.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-schedule-at">At</Label>
            <Select
              value={draft.schedule.at || "session_close"}
              onValueChange={(at) => setDraft({ ...draft, schedule: { ...draft.schedule, at } })}
            >
              <SelectTrigger id="quick-schedule-at">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="session_close">Market close</SelectItem>
                <SelectItem value="09:30">09:30</SelectItem>
                <SelectItem value="12:00">12:00</SelectItem>
                <SelectItem value="15:00">15:00</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">{capabilities.screener.schedule_note}</p>

        <div className="flex flex-col gap-2">
          <label className="flex items-center gap-2 text-sm" htmlFor="quick-notify-entry">
            <input
              id="quick-notify-entry"
              type="checkbox"
              className="size-4 accent-current"
              checked={notifyEntry}
              onChange={(event) => setNotifyEntry(event.target.checked)}
              disabled={enabledChannels.length === 0}
            />
            Notify me when a symbol enters this list (optional)
          </label>
          {notifyEntry && enabledChannels.length ? (
            <p className="text-xs text-muted-foreground">
              Sends to {enabledChannels[0].name} · {enabledChannels[0].provider}. The first
              complete run only establishes the baseline, so it is silent.
            </p>
          ) : null}
          {enabledChannels.length === 0 ? (
            <Alert role="status">
              <InfoIcon className="size-4" />
              <AlertDescription>
                No notification destination is configured; the scan still runs and its results are
                kept.
              </AlertDescription>
            </Alert>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="quick-screener-session">Session</Label>
          <Select
            value={effectiveSession}
            onValueChange={(session) => {
              setSessionTouched(true);
              setDraft({ ...draft, session });
            }}
          >
            <SelectTrigger id="quick-screener-session" className="sm:w-72">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(capabilities.sessions ?? []).map((session) => (
                <SelectItem key={session} value={session}>
                  {sessionLabel(session)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">
            {inferredSession
              ? `Set from the universe's instruments (${sessionLabel(inferredSession)}).`
              : "Must match the instruments the universe resolves to."}
          </p>
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="quick-screener-name">Name</Label>
          <Input
            id="quick-screener-name"
            value={draft.name || generatedName(universeNamesEarly, conditionText, draft.top_n)}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            placeholder="Generated from the definition"
          />
        </div>

        {issues.length ? (
          <ul className="flex flex-col gap-1 text-xs text-muted-foreground" role="status">
            {issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            disabled={!ready || pending}
            onClick={() => create.mutate("activate")}
          >
            <PlayIcon className="size-4" />
            {pending ? "Working…" : "Create and activate"}
          </Button>
          <Button
            type="button"
            variant="secondary"
            disabled={!ready || pending}
            onClick={() => create.mutate("draft")}
          >
            Save draft
          </Button>
          <Link
            className="text-xs underline text-muted-foreground"
            href="/alerts/screeners/new?mode=advanced"
          >
            Need more conditions, attachments or a different clock? Use the advanced editor
          </Link>
        </div>
      </Panel>
    </div>
  );
}
