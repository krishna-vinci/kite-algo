import { JournalPageLink } from "@/components/journal/journal-page-link";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { JournalEpisode } from "@/lib/journal/types";

function formatDateTime(value: string | null) {
  if (!value) return "Open";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function RecentEpisodesPanel({
  episodes,
  loading,
  error,
}: Readonly<{ episodes: JournalEpisode[]; loading: boolean; error: string | null }>) {
  return (
    <Panel eyebrow="review" title="Recent episodes" className="p-4 md:p-5">
      {loading ? <p className="text-sm text-foreground/60">Loading recent episodes…</p> : null}
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      {!loading && !error && episodes.length === 0 ? <p className="text-sm text-foreground/60">No episodes found for this environment.</p> : null}
      <div className="space-y-3">
        {episodes.map((episode) => (
          <JournalPageLink key={episode.id} href={`/journal/episodes/${episode.id}`} className="block rounded-2xl border border-border/70 bg-background/35 p-4 hover:border-primary/30">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-foreground">Episode #{episode.episode_seq}</p>
                <p className="mt-1 text-xs text-foreground/60">{episode.execution_context_id || "No execution context"}</p>
              </div>
              <StatusBadge tone={episode.closed_at ? "positive" : "warning"}>{episode.status}</StatusBadge>
            </div>
            <div className="mt-3 grid gap-2 text-xs text-foreground/65 md:grid-cols-2">
              <span>Opened: {formatDateTime(episode.opened_at)}</span>
              <span>Closed: {formatDateTime(episode.closed_at)}</span>
            </div>
          </JournalPageLink>
        ))}
      </div>
    </Panel>
  );
}
