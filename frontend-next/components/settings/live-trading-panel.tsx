"use client";

/**
 * Settings → Live trading. The master switch is env-controlled and shown
 * read-only; the four lane toggles are DB-backed and audited (see
 * `platform_live_settings`). Turning a lane ON always asks for confirmation
 * first — reductions never need to, since exits are never blocked.
 */

import { useState } from "react";
import { toast } from "sonner";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import {
  usePlatformLiveSettings,
  useUpdatePlatformLiveSettings,
} from "@/features/platform/hooks/use-platform-queries";
import type { PlatformLaneKey, PlatformLiveLanes } from "@/lib/platform/types";

const LANES: { key: PlatformLaneKey; label: string }[] = [
  { key: "cnc", label: "CNC" },
  { key: "mis", label: "MIS" },
  { key: "futures", label: "Futures" },
  { key: "options", label: "Options" },
];

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

export function LiveTradingPanel() {
  const settingsQuery = usePlatformLiveSettings();
  const updateMutation = useUpdatePlatformLiveSettings();
  const [lanes, setLanes] = useState<PlatformLiveLanes | null>(null);
  // Tracks which server snapshot `lanes` was last seeded from, so a fresh
  // fetch (after save, or on first load) re-seeds the draft without an
  // effect: this is the "adjust state during render" pattern React recommends
  // in place of syncing props into state inside a useEffect.
  const [seededFrom, setSeededFrom] = useState<PlatformLiveLanes | null>(null);
  const [reason, setReason] = useState("");
  const [confirmLane, setConfirmLane] = useState<PlatformLaneKey | null>(null);

  const data = settingsQuery.data;

  if (data && data.lanes !== seededFrom) {
    setLanes(data.lanes);
    setSeededFrom(data.lanes);
  }
  const dirty = Boolean(data && lanes && LANES.some((lane) => lanes[lane.key] !== data.lanes[lane.key]));

  function requestToggle(lane: PlatformLaneKey, nextValue: boolean) {
    if (!lanes) return;
    if (nextValue) {
      setConfirmLane(lane);
      return;
    }
    // Reductions (turning a lane off) are never gated.
    setLanes({ ...lanes, [lane]: false });
  }

  function confirmToggleOn() {
    if (!confirmLane || !lanes) return;
    setLanes({ ...lanes, [confirmLane]: true });
    setConfirmLane(null);
  }

  async function save() {
    if (!lanes) return;
    if (!reason.trim()) {
      toast.error("A reason is required to change live lanes.");
      return;
    }
    try {
      await updateMutation.mutateAsync({ lanes, reason: reason.trim() });
      toast.success("Live lane settings saved.");
      setReason("");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not save live settings.");
    }
  }

  if (settingsQuery.isLoading) {
    return (
      <Panel eyebrow="live trading" title="Live trading">
        <p className="text-sm text-foreground/55">Loading live settings…</p>
      </Panel>
    );
  }

  if (settingsQuery.isError || !data || !lanes) {
    return (
      <Panel eyebrow="live trading" title="Live trading">
        <p className="text-sm text-rose-300">Could not load live settings.</p>
      </Panel>
    );
  }

  const confirmLaneLabel = confirmLane ? (LANES.find((lane) => lane.key === confirmLane)?.label ?? confirmLane) : "";

  return (
    <Panel
      id="live-trading"
      eyebrow="live trading"
      title="Live trading"
      action={
        <StatusBadge tone={data.live_enabled ? "danger" : "neutral"}>
          {data.live_enabled ? "master: on" : "master: off"}
        </StatusBadge>
      }
    >
      <div className="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <div className="space-y-4 rounded-xl border border-border/70 bg-background/45 p-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Master switch</p>
            <p className="mt-1 text-sm text-foreground/80">
              {data.live_enabled ? "Live trading is enabled" : "Live trading is disabled"}
            </p>
            <p className="mt-1 text-[11px] text-foreground/45">Set by the server</p>
          </div>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Account scope</p>
            <p className="mt-1 font-mono text-sm text-foreground/80">{data.account.scope}</p>
            <p className="mt-1 text-[11px] text-foreground/45">
              {data.account.allowed ? "Authorized for live trading" : "Not authorized for live trading"}
            </p>
          </div>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Last updated</p>
            <p className="mt-1 text-sm text-foreground/70">
              {formatDate(data.updated_at)}
              {data.updated_by ? ` by ${data.updated_by}` : ""}
            </p>
            <p className="mt-1 text-[11px] text-foreground/45">Lane source: {data.lanes_source}</p>
          </div>
        </div>

        <div className="space-y-3 rounded-xl border border-border/70 bg-background/45 p-4">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-foreground/55">Lanes</p>
          <div className="grid gap-2 sm:grid-cols-2">
            {LANES.map((lane) => (
              <label
                key={lane.key}
                className="flex items-center justify-between gap-2 rounded-lg border border-border/70 bg-background/35 px-3 py-2 text-sm text-foreground/80"
              >
                <span className="font-medium uppercase tracking-[0.1em]">{lane.label}</span>
                <Switch
                  checked={lanes[lane.key]}
                  onCheckedChange={(checked) => requestToggle(lane.key, checked)}
                  aria-label={`Toggle ${lane.label} lane`}
                />
              </label>
            ))}
          </div>

          <label className="grid gap-1.5 text-xs text-foreground/55" htmlFor="live-trading-reason">
            Reason for this change
            <textarea
              id="live-trading-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="e.g. enabling options lane for the new hedge strategy"
              className="rounded-xl border border-border/70 bg-background/70 px-3 py-2 text-sm text-foreground outline-none transition-colors focus:border-primary/45"
            />
          </label>

          <button
            type="button"
            onClick={() => void save()}
            disabled={!dirty || updateMutation.isPending}
            className="inline-flex items-center justify-center rounded-xl border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {updateMutation.isPending ? "Saving…" : "Save lane changes"}
          </button>
        </div>
      </div>

      <Dialog open={confirmLane !== null} onOpenChange={(open) => !open && setConfirmLane(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Allow new live exposure in {confirmLaneLabel}?</DialogTitle>
            <DialogDescription>
              New live exposure will be allowed in {confirmLaneLabel}. Exits are never blocked.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <button
              type="button"
              onClick={() => setConfirmLane(null)}
              className="rounded-xl border border-border/70 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-foreground/70"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={confirmToggleOn}
              className="rounded-xl border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-primary"
            >
              Confirm
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Panel>
  );
}
