import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { JournalInsight, InsightKind } from "@/lib/journal/types";

type InsightsFeedProps = {
  insights: JournalInsight[];
  loading: boolean;
  error: string | null;
};

function insightTone(kind: InsightKind): "positive" | "warning" | "neutral" {
  if (kind === "milestone") return "positive";
  if (kind === "anomaly" || kind === "streak") return "warning";
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

export function InsightsFeed({ insights, loading, error }: InsightsFeedProps) {
  if (loading) {
    return (
      <Panel eyebrow="insights" title="Journal insights">
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl bg-background/40" />
          ))}
        </div>
      </Panel>
    );
  }

  if (error) {
    return (
      <Panel eyebrow="insights" title="Journal insights">
        <p className="text-sm text-rose-300">Failed to load insights.</p>
      </Panel>
    );
  }

  if (insights.length === 0) {
    return (
      <Panel eyebrow="insights" title="Journal insights">
        <p className="text-sm text-foreground/50">No insights generated yet. Insights appear as more trading data accumulates.</p>
      </Panel>
    );
  }

  return (
    <Panel eyebrow="insights" title="Journal insights">
      <div className="space-y-3">
        {insights.map((insight) => (
          <div
            key={insight.id}
            className="rounded-xl border border-border/60 bg-background/60 px-4 py-3"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-foreground/90">{insight.title}</p>
                <p className="mt-1 text-xs leading-5 text-foreground/50">{insight.description}</p>
              </div>
              <StatusBadge tone={insightTone(insight.kind)}>{insight.kind}</StatusBadge>
            </div>
            <div className="mt-2 flex items-center gap-3 text-[10px] text-foreground/30">
              <span>{formatDate(insight.created_at)}</span>
              {insight.related_run_ids.length > 0 && (
                <span>{insight.related_run_ids.length} related runs</span>
              )}
              {insight.relevance_score != null && (
                <span>score {(insight.relevance_score * 100).toFixed(0)}</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}
