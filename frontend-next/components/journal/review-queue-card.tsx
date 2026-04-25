"use client";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { ReviewQueueItem } from "@/lib/journal/types";

type ReviewQueueCardProps = {
  items: ReviewQueueItem[];
  loading: boolean;
  error: string | null;
  onSelect: (runId: string) => void;
};

function formatPnl(value: number | null): string {
  if (value == null) return "—";
  const prefix = value >= 0 ? "+" : "";
  return `${prefix}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function reviewTone(status: string): "warning" | "positive" | "neutral" {
  if (status === "pending") return "warning";
  if (status === "completed") return "positive";
  return "neutral";
}

export function ReviewQueueCard({ items, loading, error, onSelect }: ReviewQueueCardProps) {
  return (
    <Panel
      eyebrow="review"
      title="Review queue"
      action={
        items.length > 0 ? (
          <StatusBadge tone="warning">{items.length} pending</StatusBadge>
        ) : null
      }
    >
      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-14 animate-pulse rounded-xl bg-background/40" />
          ))}
        </div>
      )}

      {error && <p className="text-sm text-rose-300">Failed to load review queue.</p>}

      {!loading && !error && items.length === 0 && (
        <p className="text-sm text-foreground/50">All runs reviewed. Nothing pending.</p>
      )}

      {!loading && !error && items.length > 0 && (
        <div className="space-y-2">
          {items.slice(0, 5).map((item) => (
            <button
              key={item.run_id}
              type="button"
              onClick={() => onSelect(item.run_id)}
              className="flex w-full items-center justify-between gap-3 rounded-xl border border-border/60 bg-background/60 px-3 py-2.5 text-left transition-colors hover:border-primary/25"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground/90">
                  {item.strategy_name}
                </p>
                <p className="mt-0.5 text-[10px] uppercase tracking-[0.2em] text-foreground/40">
                  {item.strategy_family.replace(/_/g, " ")}
                </p>
              </div>
              <span className={`font-mono text-sm ${(item.net_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                {formatPnl(item.net_pnl)}
              </span>
              <StatusBadge tone={reviewTone(item.review_status)}>
                {item.review_status}
              </StatusBadge>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}
