import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { JournalV2UnresolvedItem } from "@/lib/journal/types";

function toneForStatus(status: string): "positive" | "warning" | "neutral" {
  if (status === "resolved") return "positive";
  if (status === "pending" || status === "open" || status === "in_progress") return "warning";
  return "neutral";
}

export function UnresolvedQueuePanel({
  items,
  loading,
  error,
  compact = false,
}: Readonly<{
  items: JournalV2UnresolvedItem[];
  loading: boolean;
  error: string | null;
  compact?: boolean;
}>) {
  const visibleItems = compact ? items.slice(0, 3) : items;

  return (
    <Panel eyebrow="queue" title="Unresolved queue" className="p-4 md:p-5">
      {loading ? <p className="text-sm text-foreground/60">Loading unresolved queue…</p> : null}
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      {!loading && !error && items.length === 0 ? <p className="text-sm text-foreground/60">No unresolved items for this environment.</p> : null}
      <ul className="space-y-2">
        {visibleItems.map((item) => (
          <li key={item.id} className="rounded-xl border border-border/70 bg-background/30 p-3 text-sm">
            <div className="flex items-start justify-between gap-3">
              <p className="font-medium text-foreground">{item.reason}</p>
              <StatusBadge tone={toneForStatus(item.status)}>{item.status}</StatusBadge>
            </div>
            <p className="mt-1 text-xs text-foreground/60">Source: {item.source_system}</p>
            <p className="mt-1 text-xs text-foreground/60">Candidate mappings: {item.candidate_mappings.length}</p>
          </li>
        ))}
      </ul>
      {items.length > visibleItems.length ? <p className="mt-3 text-xs text-foreground/60">{items.length - visibleItems.length} more item(s) in the queue.</p> : null}
      {items.length > 0 ? <p className="mt-3 text-xs text-foreground/60">Resolve actions are shown only after a backend-safe resolve contract is available.</p> : null}
    </Panel>
  );
}
