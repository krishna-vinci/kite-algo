"use client";

import type { JournalTimelineEvent } from "@/lib/journal/types";

type EpisodeTimelineProps = {
  events: JournalTimelineEvent[];
};

export function EpisodeTimeline({ events }: EpisodeTimelineProps) {
  if (!events.length) {
    return <p className="text-sm text-foreground/50">No timeline events yet.</p>;
  }
  return (
    <ol className="space-y-2">
      {events.map((event) => (
        <li key={event.id} className="rounded-lg border border-border/60 bg-background/60 p-2">
          <p className="text-xs uppercase tracking-[0.16em] text-foreground/50">{event.event_type}</p>
          <p className="text-sm text-foreground/80">{new Date(event.occurred_at).toLocaleString()}</p>
        </li>
      ))}
    </ol>
  );
}
