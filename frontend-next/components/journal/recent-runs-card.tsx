"use client";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { JournalRun } from "@/lib/journal/types";

type RecentRunsCardProps = {
  runs: JournalRun[];
  loading: boolean;
  error: string | null;
  onSelect: (runId: string) => void;
};

function formatPnl(value: number | null): string {
  if (value == null) return "—";
  const prefix = value >= 0 ? "+" : "";
  return `${prefix}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function statusTone(status: string): "positive" | "warning" | "neutral" | "danger" {
  if (status === "closed") return "positive";
  if (status === "open") return "warning";
  if (status === "cancelled") return "danger";
  return "neutral";
}

function formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

export function RecentRunsCard({ runs, loading, error, onSelect }: RecentRunsCardProps) {
  return (
    <Panel eyebrow="recent" title="Recent runs">
      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-14 animate-pulse rounded-xl bg-background/40" />
          ))}
        </div>
      )}

      {error && <p className="text-sm text-rose-300">Failed to load recent runs.</p>}

      {!loading && !error && runs.length === 0 && (
        <p className="text-sm text-foreground/50">No runs recorded yet.</p>
      )}

      {!loading && !error && runs.length > 0 && (
        <div className="space-y-2">
          {runs.slice(0, 6).map((run) => (
            <button
              key={run.id}
              type="button"
              onClick={() => onSelect(run.id)}
              className="flex w-full items-center justify-between gap-3 rounded-xl border border-border/60 bg-background/60 px-3 py-2.5 text-left transition-colors hover:border-primary/25"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground/90">
                  {run.strategy_name}
                </p>
                <p className="mt-0.5 text-[10px] text-foreground/40">
                  {formatDate(run.opened_at)}
                </p>
              </div>
              <span className={`font-mono text-sm ${(run.net_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                {formatPnl(run.net_pnl)}
              </span>
              <StatusBadge tone={statusTone(run.status)}>
                {run.status}
              </StatusBadge>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}
