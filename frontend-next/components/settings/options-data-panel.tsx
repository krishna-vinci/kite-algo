"use client";

/**
 * Settings → Options data. Controls the always-on option-chain sessions
 * (`platform_options_settings`): which underlyings run continuously, how
 * often the chain refreshes, whether it also updates on ticks, and how long
 * an on-demand chain (started by a strategy, the Options page, or
 * protection) is kept running once idle.
 */

import { useState } from "react";
import { toast } from "sonner";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { Switch } from "@/components/ui/switch";
import { ApiClientError } from "@/lib/api/client";
import {
  usePlatformOptionsSettings,
  useUpdatePlatformOptionsSettings,
} from "@/features/platform/hooks/use-platform-queries";
import type { OptionsSettings } from "@/lib/platform/types";

const MAX_ALWAYS_ON = 3;
const CADENCE_MIN = 1;
const CADENCE_MAX = 10;
const MIN_INTERVAL_MIN = 0.25;
const MIN_INTERVAL_MAX = 10;
const IDLE_STOP_MAX = 390;

type OptionsDraft = {
  alwaysOn: string[];
  cadenceSec: number;
  tickDriven: boolean;
  minIntervalSec: number;
  idleStopMinutes: number;
};

function draftFromSettings(data: OptionsSettings): OptionsDraft {
  return {
    alwaysOn: [...data.always_on],
    cadenceSec: data.cadence_sec,
    tickDriven: data.tick_driven,
    minIntervalSec: data.min_interval_sec,
    idleStopMinutes: data.idle_stop_minutes,
  };
}

function validateDraft(draft: OptionsDraft): string | null {
  if (draft.alwaysOn.length > MAX_ALWAYS_ON) {
    return `Choose at most ${MAX_ALWAYS_ON} always-on underlyings.`;
  }
  if (!Number.isInteger(draft.cadenceSec) || draft.cadenceSec < CADENCE_MIN || draft.cadenceSec > CADENCE_MAX) {
    return `Chain refresh must be between ${CADENCE_MIN} and ${CADENCE_MAX} seconds.`;
  }
  if (
    !Number.isFinite(draft.minIntervalSec) ||
    draft.minIntervalSec < MIN_INTERVAL_MIN ||
    draft.minIntervalSec > MIN_INTERVAL_MAX
  ) {
    return `Min interval must be between ${MIN_INTERVAL_MIN} and ${MIN_INTERVAL_MAX} seconds.`;
  }
  if (draft.minIntervalSec > draft.cadenceSec) {
    return "Min interval cannot be greater than the chain refresh interval.";
  }
  if (!Number.isInteger(draft.idleStopMinutes) || draft.idleStopMinutes < 0 || draft.idleStopMinutes > IDLE_STOP_MAX) {
    return `Idle stop must be between 0 and ${IDLE_STOP_MAX} minutes (0 keeps chains running until market close).`;
  }
  return null;
}

function isDirty(draft: OptionsDraft, data: OptionsSettings): boolean {
  return (
    draft.alwaysOn.join(",") !== data.always_on.join(",") ||
    draft.cadenceSec !== data.cadence_sec ||
    draft.tickDriven !== data.tick_driven ||
    draft.minIntervalSec !== data.min_interval_sec ||
    draft.idleStopMinutes !== data.idle_stop_minutes
  );
}

function formatDate(value: string | null): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s ago`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes.toFixed(1)}m ago`;
  return `${(minutes / 60).toFixed(1)}h ago`;
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError) {
    const detail = (error.body as { detail?: unknown } | null | undefined)?.detail;
    if (typeof detail === "string" && detail.trim() !== "") return detail;
    return error.message || fallback;
  }
  if (error instanceof Error) return error.message || fallback;
  return fallback;
}

export function OptionsDataPanel() {
  const settingsQuery = usePlatformOptionsSettings();
  const updateMutation = useUpdatePlatformOptionsSettings();
  const [draft, setDraft] = useState<OptionsDraft | null>(null);
  // Tracks which server snapshot `draft` was last seeded from, so a fresh
  // fetch (after save, or on first load) re-seeds the draft without an
  // effect: the "adjust state during render" pattern React recommends in
  // place of syncing props into state inside a useEffect.
  const [seededFrom, setSeededFrom] = useState<OptionsSettings | null>(null);
  const [reason, setReason] = useState("");

  const data = settingsQuery.data;

  if (data && data !== seededFrom) {
    setDraft(draftFromSettings(data));
    setSeededFrom(data);
  }

  const validationError = draft ? validateDraft(draft) : null;
  const dirty = Boolean(data && draft && isDirty(draft, data));

  function toggleUnderlying(underlying: string) {
    if (!draft || !data) return;
    const isSelected = draft.alwaysOn.includes(underlying);
    if (isSelected) {
      setDraft({ ...draft, alwaysOn: draft.alwaysOn.filter((u) => u !== underlying) });
      return;
    }
    if (draft.alwaysOn.length >= MAX_ALWAYS_ON) {
      toast.error(`You can keep at most ${MAX_ALWAYS_ON} underlyings always-on.`);
      return;
    }
    const nextSet = new Set(draft.alwaysOn);
    nextSet.add(underlying);
    const next = data.available_underlyings.filter((u) => nextSet.has(u));
    setDraft({ ...draft, alwaysOn: next });
  }

  async function save() {
    if (!draft) return;
    const error = validateDraft(draft);
    if (error) {
      toast.error(error);
      return;
    }
    try {
      await updateMutation.mutateAsync({
        always_on: draft.alwaysOn,
        cadence_sec: draft.cadenceSec,
        tick_driven: draft.tickDriven,
        min_interval_sec: draft.minIntervalSec,
        idle_stop_minutes: draft.idleStopMinutes,
        reason: reason.trim(),
      });
      toast.success("Options data settings saved.");
      setReason("");
    } catch (err) {
      toast.error(errorMessage(err, "Could not save options data settings."));
    }
  }

  if (settingsQuery.isLoading) {
    return (
      <Panel eyebrow="options data" title="Options data">
        <p className="text-sm text-foreground/55">Loading options settings…</p>
      </Panel>
    );
  }

  if (settingsQuery.isError || !data || !draft) {
    return (
      <Panel eyebrow="options data" title="Options data">
        <p className="text-sm text-rose-300">Could not load options settings.</p>
      </Panel>
    );
  }

  return (
    <Panel
      id="options-data"
      eyebrow="options data"
      title="Options data"
      action={
        <StatusBadge tone={data.source === "db" ? "positive" : "neutral"}>{`source: ${data.source}`}</StatusBadge>
      }
    >
      <div className="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <div className="space-y-4 rounded-xl border border-border/70 bg-background/45 p-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Last updated</p>
            <p className="mt-1 text-sm text-foreground/70">
              {formatDate(data.updated_at)}
              {data.updated_by ? ` by ${data.updated_by}` : ""}
            </p>
          </div>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">
              What &ldquo;always-on&rdquo; means
            </p>
            <p className="mt-1 text-sm leading-6 text-foreground/60">
              Always-on underlyings keep their option chain running continuously. Others start on demand
              when a strategy, the Options page, or protection needs them, and stop again after the idle
              timeout below.
            </p>
          </div>
        </div>

        <div className="space-y-4 rounded-xl border border-border/70 bg-background/45 p-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">
              Always-on underlyings (max {MAX_ALWAYS_ON})
            </p>
            <p className="mt-1 text-xs text-foreground/55">
              {draft.alwaysOn.length === 0
                ? "On-demand only: no chain runs until a strategy, the Options page or protection needs one."
                : "Leave all unchecked to run every chain on demand only."}
            </p>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              {data.available_underlyings.map((underlying) => (
                <label
                  key={underlying}
                  className="flex items-center gap-2 rounded-lg border border-border/70 bg-background/35 px-3 py-2 text-sm text-foreground/80"
                >
                  <input
                    type="checkbox"
                    checked={draft.alwaysOn.includes(underlying)}
                    onChange={() => toggleUnderlying(underlying)}
                    className="accent-primary"
                    aria-label={`Always run ${underlying}`}
                  />
                  <span className="font-medium">{underlying}</span>
                </label>
              ))}
            </div>
            <p className="mt-1 text-[11px] text-foreground/45">
              Others start on demand when a strategy, the Options page, or protection needs them.
            </p>
          </div>

          <label className="grid gap-1.5 text-xs text-foreground/55" htmlFor="options-cadence">
            Chain refresh (seconds)
            <input
              id="options-cadence"
              type="number"
              min={CADENCE_MIN}
              max={CADENCE_MAX}
              step={1}
              value={draft.cadenceSec}
              onChange={(event) => setDraft({ ...draft, cadenceSec: Number(event.target.value) })}
              className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 text-sm text-foreground outline-none transition-colors focus:border-primary/45"
            />
          </label>
          <p className="text-[11px] text-foreground/45">
            Default 5s. Index and premium stop levels do not depend on this.
          </p>

          <label className="flex items-center justify-between gap-2 rounded-lg border border-border/70 bg-background/35 px-3 py-2 text-sm text-foreground/80">
            <span className="font-medium">Update on ticks</span>
            <Switch
              checked={draft.tickDriven}
              onCheckedChange={(checked) => setDraft({ ...draft, tickDriven: checked })}
              aria-label="Toggle update on ticks"
            />
          </label>

          {draft.tickDriven && (
            <label className="grid gap-1.5 text-xs text-foreground/55" htmlFor="options-min-interval">
              Min interval between tick-driven updates (seconds)
              <input
                id="options-min-interval"
                type="number"
                min={MIN_INTERVAL_MIN}
                max={MIN_INTERVAL_MAX}
                step={0.25}
                value={draft.minIntervalSec}
                onChange={(event) => setDraft({ ...draft, minIntervalSec: Number(event.target.value) })}
                className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 text-sm text-foreground outline-none transition-colors focus:border-primary/45"
              />
            </label>
          )}

          <label className="grid gap-1.5 text-xs text-foreground/55" htmlFor="options-idle-stop">
            Stop idle on-demand chains after (minutes)
            <input
              id="options-idle-stop"
              type="number"
              min={0}
              max={IDLE_STOP_MAX}
              step={1}
              value={draft.idleStopMinutes}
              onChange={(event) => setDraft({ ...draft, idleStopMinutes: Number(event.target.value) })}
              className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 text-sm text-foreground outline-none transition-colors focus:border-primary/45"
            />
          </label>
          <p className="text-[11px] text-foreground/45">0 keeps on-demand chains running until market close.</p>

          <label className="grid gap-1.5 text-xs text-foreground/55" htmlFor="options-data-reason">
            Reason for this change (optional)
            <textarea
              id="options-data-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="e.g. widening BANKNIFTY refresh for the new spread strategy"
              className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 text-sm text-foreground outline-none transition-colors focus:border-primary/45"
            />
          </label>

          {validationError && <p className="text-xs text-rose-300">{validationError}</p>}

          <button
            type="button"
            onClick={() => void save()}
            disabled={!dirty || Boolean(validationError) || updateMutation.isPending}
            className="inline-flex items-center justify-center rounded-xl border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {updateMutation.isPending ? "Saving…" : "Save options data settings"}
          </button>
        </div>
      </div>

      <div className="mt-4">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Sessions</p>
        <div className="mt-2 overflow-auto rounded-xl border border-border/60">
          <table className="w-full text-left text-[11px]">
            <thead className="bg-background/60 text-[9px] uppercase tracking-[0.2em] text-foreground/40">
              <tr>
                <th className="px-3 py-2 font-medium">Underlying</th>
                <th className="px-3 py-2 font-medium">Running</th>
                <th className="px-3 py-2 font-medium">Always-on</th>
                <th className="px-3 py-2 font-medium">Reasons</th>
                <th className="px-3 py-2 font-medium">Last used</th>
                <th className="px-3 py-2 font-medium">Last update</th>
                <th className="px-3 py-2 font-medium text-right">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {data.sessions.map((session) => (
                <tr key={session.underlying} className="border-t border-border/40">
                  <td className="px-3 py-2 font-medium text-foreground/90">{session.underlying}</td>
                  <td className="px-3 py-2">
                    <StatusBadge tone={session.running ? "positive" : "neutral"}>
                      {session.running ? "running" : "stopped"}
                    </StatusBadge>
                  </td>
                  <td className="px-3 py-2 text-foreground/70">{session.always_on ? "Yes" : "No"}</td>
                  <td className="px-3 py-2 text-foreground/70">{session.reasons?.join(", ") || "—"}</td>
                  <td className="px-3 py-2 font-mono text-foreground/60">{formatAge(session.last_used_age_s)}</td>
                  <td className="px-3 py-2 font-mono text-foreground/60">{formatAge(session.updated_age_s)}</td>
                  <td className="px-3 py-2 text-right font-mono text-foreground/60">{session.desired_tokens}</td>
                </tr>
              ))}
              {data.sessions.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-4 text-center text-foreground/40">
                    No sessions running.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </Panel>
  );
}
