"use client";

/**
 * The common alert: "alert me when [instrument] [condition] [value], via [channel]".
 *
 * One screen, one primary action, nothing to infer by hand:
 *
 * - the session is derived from the instrument's exchange (the server's own
 *   session/exchange map), so nobody has to know the word `mcx_commodity`;
 * - the clock is `ltp` because a price alert is a tick rule; the timeframe
 *   appears only once a candle-based clock is chosen under "Timing";
 * - the name is generated from the definition and stays editable;
 * - notification channels are visible, not a later step;
 * - "Create and activate" is the primary action and "Save draft" the secondary
 *   one, and a create-then-activate partial failure is reported honestly while
 *   keeping the saved draft recoverable.
 *
 * Everything the backend can express beyond this (boolean groups, sequences,
 * breadth, producers, arithmetic operands, raw preview observations, multi-stage
 * documents) stays available through the advanced editor, which is one click
 * away and never silently drops a field.
 */

import Link from "next/link";
import { AlertCircleIcon, CheckIcon, InfoIcon, ZapIcon } from "lucide-react";
import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

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
import { InstrumentPicker } from "@/features/alerts/components/instrument-picker";
import { activateAlertsWorkflow, createAlertsWorkflow } from "@/features/alerts/api";
import { useAlertsCapabilities, useAlertsChannels } from "@/features/alerts/hooks/use-alerts-queries";
import {
  OPERATOR_LABELS,
  type AlertDraft,
  buildDocument,
  emptyDraft,
  exchangeOf,
  sessionForExchange,
  sessionLabel,
} from "@/features/alerts/lib/authoring";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";
import { newIdempotencyKey } from "@/lib/ids";

/** The operators that read naturally in "price [operator] level". */
const QUICK_OPERATORS = [
  "crosses_above",
  "crosses_below",
  "gt",
  "lt",
  "rises_pct",
  "falls_pct",
] as const;

type Outcome =
  | { kind: "draft-saved"; workflowId: string }
  | { kind: "activated"; workflowId: string }
  | { kind: "activation-failed"; workflowId: string; reason: string };

function generatedName(instrument: string | undefined, operator: string, value: number | null): string {
  const symbol = instrument ? instrument.split(":").slice(1).join(":") : "";
  const verb: Record<string, string> = {
    crosses_above: "crosses above",
    crosses_below: "crosses below",
    gt: "above",
    lt: "below",
    rises_pct: "rises",
    falls_pct: "falls",
  };
  const suffix = operator === "rises_pct" || operator === "falls_pct" ? "%" : "";
  if (!symbol) return "";
  return `${symbol} ${verb[operator] ?? operator} ${value ?? ""}${suffix}`.trim();
}

function isCandleOperator(operator: string): boolean {
  return operator === "rises_pct" || operator === "falls_pct";
}

export function QuickAlertComposer({ scope }: { scope: string | null }) {
  const router = useRouter();
  const capabilitiesQuery = useAlertsCapabilities(scope);
  const channelsQuery = useAlertsChannels(scope);

  const [instrument, setInstrument] = useState<string>("");
  const [operator, setOperator] = useState<string>("crosses_above");
  const [value, setValue] = useState<string>("");
  const [channels, setChannels] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [timeframe, setTimeframe] = useState("15minute");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [idempotencyKey] = useState(() => newIdempotencyKey("alert"));

  const capabilities = capabilitiesQuery.data?.capabilities;
  const allChannels = channelsQuery.data?.channels;
  const enabledChannels = useMemo(
    () => (allChannels ?? []).filter((channel) => channel.enabled),
    [allChannels],
  );

  // Notifications are part of the common path, so preselect when there is no
  // choice to make rather than showing an empty required field.
  const selectedChannels = useMemo(
    () =>
      channels.length
        ? channels
        : enabledChannels.length === 1
          ? [enabledChannels[0].name]
          : [],
    [channels, enabledChannels],
  );

  const session = useMemo(() => {
    if (!capabilities || !instrument) return "";
    return sessionForExchange(exchangeOf(instrument), capabilities.session_exchanges);
  }, [capabilities, instrument]);

  const numericValue = value.trim() === "" ? null : Number(value);
  const valueValid = numericValue !== null && Number.isFinite(numericValue);
  const effectiveName = nameTouched && name.trim() ? name.trim() : generatedName(instrument, operator, numericValue);
  const ready = Boolean(instrument) && valueValid && selectedChannels.length > 0;

  const draft: AlertDraft = useMemo(() => {
    const base = emptyDraft();
    return {
      ...base,
      name: effectiveName,
      session,
      clock: isCandleOperator(operator) ? "candle_close" : "ltp",
      timeframe,
      instruments: instrument ? [instrument] : [],
      conditions: [
        {
          left: { kind: "field", name: "ltp" },
          op: operator,
          right: { kind: "constant", value: numericValue ?? 0 },
        },
      ],
      alert: { ...base.alert, trigger: "once", channels: selectedChannels },
    };
  }, [effectiveName, session, operator, timeframe, instrument, numericValue, selectedChannels]);

  const create = useMutation({
    mutationFn: async (mode: "draft" | "activate") => {
      const document = buildDocument(draft);
      const created = await createAlertsWorkflow(
        { name: draft.name, document, idempotency_key: idempotencyKey },
        scope,
      );
      if (mode === "draft") {
        return { kind: "draft-saved" as const, workflowId: created.workflow_id };
      }
      try {
        await activateAlertsWorkflow(created.workflow_id, { scope });
        return { kind: "activated" as const, workflowId: created.workflow_id };
      } catch (error) {
        // The draft IS saved. Saying "created" would be a lie, and retrying the
        // whole create would duplicate it, so report the half that failed and
        // hand back a link to the recoverable draft.
        return {
          kind: "activation-failed" as const,
          workflowId: created.workflow_id,
          reason: alertsErrorMessage(error, "activation was refused"),
        };
      }
    },
    onSuccess: (result) => {
      setOutcome(result);
      if (result.kind === "activated") {
        router.push(`/alerts/${result.workflowId}`);
      }
    },
  });

  if (capabilitiesQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading alert options…</p>;
  }
  if (capabilitiesQuery.error || !capabilities) {
    return (
      <Alert variant="destructive" role="alert">
        <AlertCircleIcon className="size-4" />
        <AlertTitle>Could not load alert options</AlertTitle>
        <AlertDescription>{alertsErrorMessage(capabilitiesQuery.error, "the server did not return alert options")}</AlertDescription>
      </Alert>
    );
  }

  const pending = create.isPending;
  const sessions = new Set(Object.keys(capabilities.session_exchanges));

  return (
    <div className="flex flex-col gap-5 pb-8">
      <SectionLabel
        eyebrow="Alerts"
        title="New alert"
        description="Pick an instrument, set the level, choose where it notifies."
      />

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
            <span>
              The definition is saved and nothing was lost.{" "}
              <Link className="underline" href={`/alerts/${outcome.workflowId}`}>
                Open the draft
              </Link>{" "}
              to retry activation.
            </span>
          </AlertDescription>
        </Alert>
      ) : null}
      {create.error ? (
        <Alert variant="destructive" role="alert">
          <AlertCircleIcon className="size-4" />
          <AlertTitle>Could not save</AlertTitle>
          <AlertDescription>{alertsErrorMessage(create.error, "the server rejected the request")}</AlertDescription>
        </Alert>
      ) : null}

      <Panel className="flex flex-col gap-5 p-5">
        <div className="flex flex-col gap-2">
          <Label htmlFor="quick-instrument">Instrument</Label>
          <InstrumentPicker
            selected={instrument ? [instrument] : []}
            onChange={(next) => setInstrument(next[next.length - 1] ?? "")}
            acceptedExchanges={
              session ? capabilities.session_exchanges[session] ?? [] : null
            }
          />
          {instrument && session ? (
            <p className="text-xs text-muted-foreground">
              {sessionLabel(session)} session · this alert is checked on every price tick
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              The session follows the exchange, so you do not have to pick one.
            </p>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-operator">Condition</Label>
            <Select value={operator} onValueChange={setOperator}>
              <SelectTrigger id="quick-operator">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {QUICK_OPERATORS.filter((op) => op in OPERATOR_LABELS).map((op) => (
                  <SelectItem key={op} value={op}>
                    {OPERATOR_LABELS[op]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-value">
              {isCandleOperator(operator) ? "Percent" : "Level"}
            </Label>
            <Input
              id="quick-value"
              type="number"
              inputMode="decimal"
              step="any"
              value={value}
              aria-invalid={value.trim() !== "" && !valueValid}
              onChange={(event) => setValue(event.target.value)}
            />
          </div>
        </div>

        {isCandleOperator(operator) ? (
          <div className="flex flex-col gap-2">
            <Label htmlFor="quick-timeframe">Measured over</Label>
            <Select value={timeframe} onValueChange={setTimeframe}>
              <SelectTrigger id="quick-timeframe">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(capabilities.timeframes ?? []).map((frame) => (
                  <SelectItem key={frame} value={frame}>
                    {frame}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Percentage moves are measured between completed candles, so this one needs a timeframe.
            </p>
          </div>
        ) : (
          <p className="text-xs text-muted-foreground" id="quick-crossing-help">
            A crossing fires the first time the price moves through the level. A fresh activation
            starts silent: if the price is already past the level right now, nothing is sent until
            it crosses again.
          </p>
        )}

        <div className="flex flex-col gap-2">
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
                </Link>{" "}
                before activating this alert.
              </AlertDescription>
            </Alert>
          ) : (
            <div className="flex flex-wrap gap-3">
              {enabledChannels.map((channel) => {
                const checked = selectedChannels.includes(channel.name);
                return (
                  <label
                    key={channel.channel_id}
                    htmlFor={`quick-channel-${channel.channel_id}`}
                    className="flex items-center gap-2 text-sm"
                  >
                    <Checkbox
                      id={`quick-channel-${channel.channel_id}`}
                      checked={checked}
                      onCheckedChange={(next) =>
                        setChannels(
                          next
                            ? [...selectedChannels, channel.name]
                            : selectedChannels.filter((item) => item !== channel.name),
                        )
                      }
                    />
                    {channel.name} · {channel.provider}
                  </label>
                );
              })}
            </div>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="quick-name">Name</Label>
          <Input
            id="quick-name"
            value={nameTouched ? name : effectiveName}
            onChange={(event) => {
              setNameTouched(true);
              setName(event.target.value);
            }}
            placeholder="Generated from the definition"
          />
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            disabled={!ready || pending}
            onClick={() => create.mutate("activate")}
          >
            <ZapIcon className="size-4" />
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
            href="/alerts/new?mode=advanced"
          >
            Need groups, sequences or a custom message? Use the advanced editor
          </Link>
        </div>
        {!ready ? (
          <p className="text-xs text-muted-foreground">
            Choose an instrument, a level and a destination to continue. Sessions, clocks and
            revisions are handled for you.
          </p>
        ) : null}
      </Panel>

      {sessions.size ? null : (
        <Alert role="status">
          <InfoIcon className="size-4" />
          <AlertDescription>
            This deployment exposes no session policies, so nothing can be activated yet.
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
