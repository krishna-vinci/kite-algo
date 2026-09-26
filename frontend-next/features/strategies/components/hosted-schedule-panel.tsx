"use client";

/**
 * Schedules: when the strategy runs, what happened last, and when it runs next.
 *
 * Every timing fact here comes from the server: the next occurrence from the
 * scheduler's own forward rule, the last/missed occurrence from its durable
 * occurrence rows, and the misfire grace and overlap policy from the runtime's
 * configured values. Exchange sessions are requested per exchange and segment,
 * so an MCX or currency schedule is never shown NSE/CM hours.
 */

import { useMemo, useState } from "react";
import { CalendarClockIcon, Loader2Icon, PlayIcon, PowerIcon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
  useHostedSchedule,
  useHostedScheduleOccurrences,
  useOperatorCalendar,
  useSaveHostedSchedule,
  useSetHostedScheduleEnabled,
} from "@/features/strategies/hooks/use-hosted-strategies-queries";
import {
  hostedErrorMessage,
  occurrenceStatusLabel,
  scheduleCadenceLabel,
  scheduleKindLabel,
  schedulePolicyCopy,
} from "@/features/strategies/lib/format";
import { HostedParamInputs, useHostedParamValues } from "@/features/strategies/components/hosted-params-editor";
import { environmentLabel, supportedExecutionModes } from "@/features/strategies/lib/modes";
import type {
  HostedSchedule,
  HostedStrategy,
  HostedStrategyOptions,
  HostedVersion,
} from "@/lib/hosted-strategies/types";

/** Reused by the composer's inline "On a schedule" run style. */
export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
export const TIMEZONES = [
  "Asia/Kolkata",
  "Asia/Dubai",
  "Asia/Singapore",
  "UTC",
  "Europe/London",
  "America/New_York",
];

type ScheduleForm = {
  versionId: string;
  environment: string;
  jobKind: string;
  scheduleKind: "daily" | "weekly" | "monthly" | "calendar";
  atTime: string;
  weekday: number;
  dayOfMonth: number;
  calendarDates: string;
  timezone: string;
  enabled: boolean;
};

function formFromSchedule(
  schedule: HostedSchedule | null,
  fallbackEnvironment: string,
): ScheduleForm {
  return {
    versionId: schedule?.version_id ?? "",
    environment: schedule?.execution_mode ?? fallbackEnvironment,
    jobKind: schedule?.job_kind ?? "finite",
    scheduleKind: (schedule?.schedule_kind as ScheduleForm["scheduleKind"]) ?? "daily",
    atTime: schedule?.at_time ?? "09:30",
    weekday: schedule?.weekday ?? 1,
    dayOfMonth: schedule?.day_of_month ?? 1,
    calendarDates: (schedule?.calendar_dates ?? []).join(", "),
    timezone: schedule?.timezone ?? "Asia/Kolkata",
    enabled: schedule?.enabled ?? true,
  };
}

/**
 * The backend silently falls back to UTC for a zone it cannot resolve, so an
 * unknown name is refused here. `DateTimeFormat` is the authoritative check:
 * unlike `supportedValuesOf` it accepts the aliases operators actually type
 * (Asia/Kolkata as well as Asia/Calcutta).
 */
export function knownTimezone(name: string): boolean {
  const raw = name.trim();
  if (!raw) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: raw });
    return true;
  } catch {
    return false;
  }
}

export function HostedSchedulePanel({
  strategy,
  versions,
  options,
}: Readonly<{
  strategy: HostedStrategy;
  versions: HostedVersion[];
  options: HostedStrategyOptions | undefined;
}>) {
  const strategyId = strategy.strategy_id;
  const scheduleQuery = useHostedSchedule(strategyId);
  const occurrencesQuery = useHostedScheduleOccurrences(strategyId, Boolean(scheduleQuery.data));
  const saveSchedule = useSaveHostedSchedule(strategyId);
  const setEnabled = useSetHostedScheduleEnabled(strategyId);

  const schedule = scheduleQuery.data ?? null;
  const [editing, setEditing] = useState(false);
  // The editable draft is `null` until the operator touches a control, so the
  // displayed form derives from the server's stored schedule without copying it
  // into state inside an effect.
  const [draft, setDraft] = useState<ScheduleForm | null>(null);
  const [exchange, setExchange] = useState("NSE");
  const [segment, setSegment] = useState("CM");

  const baseForm = formFromSchedule(schedule, strategy.default_execution_mode);
  const form: ScheduleForm = draft ?? {
    ...baseForm,
    versionId: baseForm.versionId || (versions.length > 0 ? versions[versions.length - 1].version_id : ""),
  };
  const setForm = (update: ScheduleForm | ((current: ScheduleForm) => ScheduleForm)) =>
    setDraft((current) => (typeof update === "function" ? update(current ?? form) : update));
  const selectedVersion =
    versions.find((version) => version.version_id === form.versionId) ??
    (versions.length > 0 ? versions[versions.length - 1] : undefined);
  // The pinned version's schema drives the parameters, exactly like a manual
  // launch. Nothing is stamped into them.
  const paramValues = useHostedParamValues(selectedVersion?.parameters_schema);

  const calendarParams = useMemo(() => {
    const start = new Date();
    const end = new Date(start.getTime() + 14 * 24 * 3600 * 1000);
    return {
      exchange,
      segment,
      from: start.toISOString().slice(0, 10),
      to: end.toISOString().slice(0, 10),
    };
  }, [exchange, segment]);
  const calendarQuery = useOperatorCalendar(calendarParams);

  async function save() {
    if (!selectedVersion) {
      toast.error("Register a version before scheduling.");
      return;
    }
    if (!knownTimezone(form.timezone)) {
      toast.error(`"${form.timezone}" is not a timezone this browser knows.`);
      return;
    }
    if (!paramValues.valid) {
      toast.error(Object.values(paramValues.errors)[0] ?? "Check the parameters.");
      return;
    }
    try {
      await saveSchedule.mutateAsync({
        version_id: selectedVersion.version_id,
        execution_mode: form.environment,
        job_kind: form.jobKind,
        params: paramValues.value,
        schedule_kind: form.scheduleKind,
        at_time: form.atTime,
        weekday: form.scheduleKind === "weekly" ? form.weekday : null,
        day_of_month: form.scheduleKind === "monthly" ? form.dayOfMonth : null,
        calendar_dates:
          form.scheduleKind === "calendar"
            ? form.calendarDates
                .split(",")
                .map((entry) => entry.trim())
                .filter(Boolean)
            : null,
        timezone: form.timezone,
        enabled: form.enabled,
      });
      toast.success("Schedule saved.");
      setEditing(false);
      setDraft(null);
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  async function toggleEnabled(next: boolean) {
    try {
      await setEnabled.mutateAsync(next);
      toast.success(
        next
          ? "Schedule enabled. It starts the strategy at the next occurrence."
          : "Schedule disabled. Nothing new will start from it.",
      );
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    }
  }

  if (scheduleQuery.isLoading) {
    return <Skeleton className="h-32 w-full rounded-md" />;
  }
  if (scheduleQuery.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the schedule</AlertTitle>
        <AlertDescription>{hostedErrorMessage(scheduleQuery.error)}</AlertDescription>
      </Alert>
    );
  }

  const showForm = !schedule || editing;

  return (
    <div className="flex flex-col gap-5">
      {schedule ? (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="flex items-center gap-2 text-sm font-semibold">
              <CalendarClockIcon className="size-4" aria-hidden />
              {scheduleCadenceLabel(schedule)}
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => setEditing((value) => !value)}>
                {editing ? "Cancel edit" : "Edit schedule"}
              </Button>
              <Button
                size="sm"
                variant={schedule.enabled ? "outline" : "default"}
                onClick={() => toggleEnabled(!schedule.enabled)}
                disabled={setEnabled.isPending}
              >
                {setEnabled.isPending ? (
                  <Loader2Icon className="size-4 animate-spin" aria-hidden />
                ) : (
                  <PowerIcon className="size-4" aria-hidden />
                )}
                {schedule.enabled ? "Disable" : "Enable"}
              </Button>
            </div>
          </div>
          <dl className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
            <div>
              <dt className="font-medium text-foreground">State</dt>
              <dd data-testid="schedule-state">
                {schedule.enabled ? (schedule.manually_paused ? "Paused" : "Enabled") : "Disabled"}
              </dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Next run</dt>
              <dd data-testid="schedule-next">
                {schedule.next_occurrence_at ?? "No future occurrence"}
              </dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Last occurrence</dt>
              <dd data-testid="schedule-last">
                {schedule.last_occurrence
                  ? `${occurrenceStatusLabel(schedule.last_occurrence.status)} · ${
                      schedule.last_occurrence.due_at ?? ""
                    }${schedule.last_occurrence.skip_reason ? ` · ${schedule.last_occurrence.skip_reason}` : ""}`
                  : "Nothing has run yet"}
              </dd>
            </div>
            <div>
              <dt className="font-medium text-foreground">Version, environment and kind</dt>
              <dd>
                v{schedule.version_number ?? "—"} · {environmentLabel(schedule.execution_mode)} ·{" "}
                {schedule.job_kind}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="font-medium text-foreground">Missed runs and overlap</dt>
              <dd data-testid="schedule-policy">{schedulePolicyCopy(schedule)}</dd>
            </div>
          </dl>
          {!schedule.enabled ? (
            <p className="text-xs text-muted-foreground">
              A disabled schedule never starts an attempt. Enabling it again starts the strategy at the
              next occurrence — it does not resume a stopped attempt.
            </p>
          ) : null}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <p className="text-sm font-semibold">No schedule yet</p>
          <p className="text-xs text-muted-foreground">
            A schedule starts a new attempt of a pinned version at a fixed local time. It never resumes a
            stopped attempt and it never restarts a disabled strategy.
          </p>
        </div>
      )}

      {occurrencesQuery.data && occurrencesQuery.data.length > 0 ? (
        <div className="flex flex-col gap-2">
          <p className="text-sm font-medium">Recent occurrences</p>
          <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
            {occurrencesQuery.data.slice(0, 5).map((row) => (
              <li key={row.occurrence_key} className="flex flex-wrap items-center justify-between gap-2">
                <span>
                  {row.due_at} · {occurrenceStatusLabel(row.status)}
                  {row.skip_reason ? ` · ${row.skip_reason}` : ""}
                </span>
                <span>{row.fired_at ? `started ${row.fired_at}` : ""}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {showForm ? (
        <div className="flex flex-col gap-4 rounded-lg border border-border/70 p-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="grid gap-1.5">
              <Label htmlFor="schedule-version">Version</Label>
              <Select
                value={selectedVersion?.version_id ?? ""}
                onValueChange={(value) => setForm((current) => ({ ...current, versionId: value }))}
              >
                <SelectTrigger id="schedule-version" className="w-full">
                  <SelectValue placeholder="Select a version" />
                </SelectTrigger>
                <SelectContent>
                  {versions.map((version) => (
                    <SelectItem key={version.version_id} value={version.version_id}>
                      v{version.version}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="schedule-environment">Environment</Label>
              <Select
                value={form.environment}
                onValueChange={(value) => setForm((current) => ({ ...current, environment: value }))}
              >
                <SelectTrigger id="schedule-environment" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {supportedExecutionModes(options).map((mode) => (
                    <SelectItem key={mode} value={mode}>
                      {environmentLabel(mode)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="schedule-kind">Repeats</Label>
              <Select
                value={form.scheduleKind}
                onValueChange={(value) =>
                  setForm((current) => ({
                    ...current,
                    scheduleKind: value as ScheduleForm["scheduleKind"],
                  }))
                }
              >
                <SelectTrigger id="schedule-kind" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(["daily", "weekly", "monthly", "calendar"] as const).map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {scheduleKindLabel(kind)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="schedule-time">At (local time, HH:MM)</Label>
              <Input
                id="schedule-time"
                value={form.atTime}
                placeholder="09:30"
                onChange={(event) => setForm((current) => ({ ...current, atTime: event.target.value }))}
              />
            </div>
            {form.scheduleKind === "weekly" ? (
              <div className="grid gap-1.5">
                <Label htmlFor="schedule-weekday">Day of week</Label>
                <Select
                  value={String(form.weekday)}
                  onValueChange={(value) =>
                    setForm((current) => ({ ...current, weekday: Number(value) }))
                  }
                >
                  <SelectTrigger id="schedule-weekday" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {WEEKDAYS.map((label, index) => (
                      <SelectItem key={label} value={String(index)}>
                        {label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            ) : null}
            {form.scheduleKind === "monthly" ? (
              <div className="grid gap-1.5">
                <Label htmlFor="schedule-day">Day of month</Label>
                <Input
                  id="schedule-day"
                  inputMode="numeric"
                  value={String(form.dayOfMonth)}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, dayOfMonth: Number(event.target.value) }))
                  }
                />
                <p className="text-xs text-muted-foreground">
                  A short month uses its last day (31 becomes 30 or 28).
                </p>
              </div>
            ) : null}
            {form.scheduleKind === "calendar" ? (
              <div className="grid gap-1.5 md:col-span-2">
                <Label htmlFor="schedule-dates">Dates (YYYY-MM-DD, comma separated)</Label>
                <Input
                  id="schedule-dates"
                  value={form.calendarDates}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, calendarDates: event.target.value }))
                  }
                />
              </div>
            ) : null}
            <div className="grid gap-1.5">
              <Label htmlFor="schedule-timezone">Timezone</Label>
              <Select
                value={TIMEZONES.includes(form.timezone) ? form.timezone : "other"}
                onValueChange={(value) =>
                  setForm((current) => ({
                    ...current,
                    timezone: value === "other" ? current.timezone : value,
                  }))
                }
              >
                <SelectTrigger id="schedule-timezone" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TIMEZONES.map((zone) => (
                    <SelectItem key={zone} value={zone}>
                      {zone}
                    </SelectItem>
                  ))}
                  <SelectItem value="other">Other…</SelectItem>
                </SelectContent>
              </Select>
              {!TIMEZONES.includes(form.timezone) ? (
                <Input
                  aria-label="Timezone name"
                  value={form.timezone}
                  placeholder="Asia/Kolkata"
                  onChange={(event) =>
                    setForm((current) => ({ ...current, timezone: event.target.value }))
                  }
                />
              ) : null}
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="schedule-jobkind">Run kind</Label>
              <Select
                value={form.jobKind}
                onValueChange={(value) => setForm((current) => ({ ...current, jobKind: value }))}
              >
                <SelectTrigger id="schedule-jobkind" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(options?.job_kinds ?? ["finite", "continuous"]).map((kind) => (
                    <SelectItem key={kind} value={kind}>
                      {kind}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">Parameters for scheduled runs</p>
            <HostedParamInputs params={paramValues} idPrefix="schedule-param" />
          </div>

          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">Session check</p>
            <div className="flex flex-wrap items-end gap-3">
              <div className="grid gap-1.5">
                <Label htmlFor="schedule-exchange">Exchange</Label>
                <Input
                  id="schedule-exchange"
                  className="w-28"
                  value={exchange}
                  onChange={(event) => setExchange(event.target.value.toUpperCase())}
                />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="schedule-segment">Segment</Label>
                <Input
                  id="schedule-segment"
                  className="w-28"
                  value={segment}
                  onChange={(event) => setSegment(event.target.value.toUpperCase())}
                />
              </div>
            </div>
            {calendarQuery.isLoading ? (
              <p className="text-xs text-muted-foreground">Loading sessions…</p>
            ) : calendarQuery.isError ? (
              <p className="text-xs text-muted-foreground" data-testid="calendar-unavailable">
                {hostedErrorMessage(calendarQuery.error)}
              </p>
            ) : calendarQuery.data ? (
              <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
                {calendarQuery.data.sessions.slice(0, 4).map((session) => (
                  <li key={session.session_date}>
                    {session.session_date} · {session.session_type} ·{" "}
                    {session.opens_at && session.closes_at
                      ? `${session.opens_at.slice(11, 16)}–${session.closes_at.slice(11, 16)}`
                      : "no clock times"}
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="text-xs text-muted-foreground">
              Sessions come from this deployment&apos;s own exchange calendar for the exchange and segment
              you name. An uncovered range is reported, never guessed.
            </p>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={form.enabled}
              onCheckedChange={(value) =>
                setForm((current) => ({ ...current, enabled: value === true }))
              }
              aria-label="Schedule enabled"
            />
            Schedule enabled
          </label>
          <div className="flex items-center gap-3">
            <Button onClick={save} disabled={saveSchedule.isPending}>
              {saveSchedule.isPending ? (
                <Loader2Icon className="size-4 animate-spin" aria-hidden />
              ) : (
                <PlayIcon className="size-4" aria-hidden />
              )}
              {schedule ? "Save schedule" : "Create schedule"}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
