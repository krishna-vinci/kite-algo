"use client";

/**
 * Row/detail actions for an alert that already exists.
 *
 * Two things an operator asks for after creation and could not do before:
 *
 * * **Repeat** — change how often the alert notifies. "Once when it happens" is
 *   the calm default for a new alert, but an alert that has fired once went quiet
 *   with no way to make it repeat short of rebuilding it.
 * * **Delete** — remove a workflow that was a mistake or a test. Archive stays
 *   the reversible option; this one really removes the definition and stops it
 *   evaluating, and says exactly what it removed.
 *
 * Both are honest about what happened: a frequency change that had to be treated
 * as "already in that state" says so, and a delete reports the notification
 * history it kept instead of implying it erased something it did not.
 */

import { useState } from "react";
import { AlertTriangleIcon, RepeatIcon, TrashIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";
import { FREQUENCY_OPTIONS, type FrequencyChoice } from "@/features/alerts/lib/plain-language";

export type FrequencyChangeResult = {
  changed: boolean;
  trigger: string;
  activated: boolean;
  revision: number;
  note?: string;
};

export function describeFrequencyResult(result: FrequencyChangeResult): string {
  if (!result.changed) {
    return result.note ?? "The alert already notifies this way.";
  }
  const base = result.activated
    ? `Saved as revision ${result.revision} and put in force.`
    : `Saved as revision ${result.revision}. Activate it to take effect.`;
  return base;
}

/** The three plain choices, shared by the creation page and this dialog. */
export function FrequencyChoices({
  value,
  onChange,
  reminderSeconds,
  onReminderSeconds,
}: {
  value: FrequencyChoice;
  onChange: (next: FrequencyChoice) => void;
  reminderSeconds: number;
  onReminderSeconds: (next: number) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div role="radiogroup" aria-label="How often should it notify" className="flex flex-col gap-2">
        {FREQUENCY_OPTIONS.map((option) => (
          <label
            key={option.value}
            className="flex cursor-pointer items-start gap-3 rounded-lg border border-border/60 px-3 py-2"
          >
            <input
              type="radio"
              name="change-frequency"
              className="mt-1"
              checked={value === option.value}
              onChange={() => onChange(option.value)}
            />
            <span className="flex flex-col">
              <span className="text-sm">{option.label}</span>
              <span className="text-xs text-muted-foreground">{option.hint}</span>
            </span>
          </label>
        ))}
      </div>
      {value === "reminder" ? (
        <div className="flex flex-col gap-1">
          <Label htmlFor="change-frequency-interval">Remind me every (minutes)</Label>
          <Input
            id="change-frequency-interval"
            type="number"
            min={1}
            className="max-w-[10rem]"
            value={Math.max(1, Math.round(reminderSeconds / 60))}
            onChange={(event) => onReminderSeconds(Number(event.target.value) * 60)}
          />
        </div>
      ) : null}
    </div>
  );
}

export function ChangeFrequencyDialog({
  name,
  current,
  currentReminderSeconds,
  pending,
  result,
  error,
  onSubmit,
  onClose,
}: {
  name: string;
  current: FrequencyChoice;
  currentReminderSeconds: number;
  pending: boolean;
  result: FrequencyChangeResult | null;
  error: unknown;
  onSubmit: (frequency: FrequencyChoice, reminderSeconds: number) => void;
  onClose: () => void;
}) {
  const [choice, setChoice] = useState<FrequencyChoice>(current);
  const [reminderSeconds, setReminderSeconds] = useState(currentReminderSeconds || 900);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Change how often ${name} notifies`}
    >
      <div className="w-full max-w-lg rounded-xl border border-border/70 bg-card p-5 shadow-lg">
        <h2 className="text-sm font-semibold">How often should this alert notify?</h2>
        <p className="mt-1 text-xs text-muted-foreground">{name}</p>

        <div className="mt-4">
          <FrequencyChoices
            value={choice}
            onChange={setChoice}
            reminderSeconds={reminderSeconds}
            onReminderSeconds={setReminderSeconds}
          />
        </div>

        {result ? (
          <Alert className="mt-4" role="status">
            <AlertTitle>Saved</AlertTitle>
            <AlertDescription>{describeFrequencyResult(result)}</AlertDescription>
          </Alert>
        ) : null}
        {error ? (
          <Alert variant="destructive" className="mt-4" role="alert">
            <AlertTitle>Could not change the frequency</AlertTitle>
            <AlertDescription>
              {alertsErrorMessage(error, "the server refused the change")}
            </AlertDescription>
          </Alert>
        ) : null}

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <Button
            type="button"
            disabled={pending}
            onClick={() => onSubmit(choice, reminderSeconds)}
          >
            <RepeatIcon className="size-4" aria-hidden />
            {pending ? "Saving…" : "Save"}
          </Button>
          <Button type="button" variant="secondary" onClick={onClose}>
            {result ? "Close" : "Cancel"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function DeleteWorkflowDialog({
  name,
  active,
  pending,
  result,
  error,
  onSubmit,
  onClose,
}: {
  name: string;
  active: boolean;
  pending: boolean;
  result: { deleted: Record<string, number>; history: { events_preserved: number }; note: string } | null;
  error: unknown;
  onSubmit: () => void;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Delete ${name}`}
    >
      <div className="w-full max-w-lg rounded-xl border border-border/70 bg-card p-5 shadow-lg">
        <div className="flex items-start gap-2">
          <AlertTriangleIcon className="mt-0.5 size-4 text-rose-400" aria-hidden />
          <div>
            <h2 className="text-sm font-semibold">Delete “{name}”?</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              This removes the alert and its revisions, and stops it being evaluated
              {active ? " (it is switched on right now)" : ""}. It cannot be undone — use
              Archive instead if you may want it back.
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              The record of what was already sent is kept, so the notification history stays
              readable.
            </p>
          </div>
        </div>

        {result ? (
          <Alert className="mt-4" role="status">
            <AlertTitle>Deleted</AlertTitle>
            <AlertDescription>
              {result.note} {result.history.events_preserved > 0
                ? `${result.history.events_preserved} notification record(s) kept.`
                : ""}
            </AlertDescription>
          </Alert>
        ) : null}
        {error ? (
          <Alert variant="destructive" className="mt-4" role="alert">
            <AlertTitle>Could not delete</AlertTitle>
            <AlertDescription>
              {alertsErrorMessage(error, "the server refused the delete")}
            </AlertDescription>
          </Alert>
        ) : null}

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <Button type="button" variant="destructive" disabled={pending} onClick={onSubmit}>
            <TrashIcon className="size-4" aria-hidden />
            {pending ? "Deleting…" : "Delete permanently"}
          </Button>
          <Button type="button" variant="secondary" onClick={onClose}>
            {result ? "Close" : "Keep it"}
          </Button>
        </div>
      </div>
    </div>
  );
}
