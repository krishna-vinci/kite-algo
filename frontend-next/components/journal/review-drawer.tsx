"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { JournalRun, ReviewStatus } from "@/lib/journal/types";
import { fetchJournalRun, updateRunReview } from "@/lib/journal/api";

type ReviewDrawerProps = {
  runId: string | null;
  onClose: () => void;
  onUpdated: () => void;
};

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

function formatPnl(value: number | null): string {
  if (value == null) return "—";
  const prefix = value >= 0 ? "+" : "";
  return `${prefix}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function ReviewDrawer({ runId, onClose, onUpdated }: ReviewDrawerProps) {
  const [run, setRun] = useState<JournalRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      setNotes("");
      return;
    }

    let disposed = false;
    setLoading(true);
    setError(null);

    fetchJournalRun(runId)
      .then((data) => {
        if (!disposed) {
          setRun(data);
          setNotes(data.notes ?? "");
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!disposed) {
          setError(err instanceof Error ? err.message : "Failed to load run");
          setLoading(false);
        }
      });

    return () => {
      disposed = true;
    };
  }, [runId]);

  async function handleUpdateStatus(status: ReviewStatus) {
    if (!runId) return;
    setSaving(true);
    setSaveError(null);
    try {
      await updateRunReview(runId, { review_status: status, notes: notes || undefined });
      onUpdated();
      onClose();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Failed to update review");
    } finally {
      setSaving(false);
    }
  }

  if (!runId) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-label="Review drawer">
      <button
        type="button"
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close drawer"
      />
      <div className="relative w-full max-w-md overflow-y-auto border-l border-border/70 bg-[var(--panel,#0c0d12)] p-4">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold tracking-tight">Review run</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border/70 px-2.5 py-1 text-xs text-foreground/60 transition-colors hover:text-foreground"
          >
            Close
          </button>
        </div>

        {loading && <div className="h-[300px] animate-pulse rounded-xl bg-background/40" />}

        {error && <p className="text-sm text-rose-300">{error}</p>}

        {run && !loading && (
          <div className="space-y-4">
            <Panel>
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium text-foreground/90">{run.strategy_name}</p>
                  <StatusBadge tone={run.status === "closed" ? "positive" : "warning"}>
                    {run.status}
                  </StatusBadge>
                </div>
                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.2em] text-foreground/40">Family</p>
                    <p className="mt-1 text-foreground/80">{run.strategy_family.replace(/_/g, " ")}</p>
                  </div>
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.2em] text-foreground/40">P&L</p>
                    <p className={`mt-1 font-mono ${(run.net_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {formatPnl(run.net_pnl)}
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.2em] text-foreground/40">Opened</p>
                    <p className="mt-1 text-foreground/80">{formatDate(run.opened_at)}</p>
                  </div>
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.2em] text-foreground/40">Closed</p>
                    <p className="mt-1 text-foreground/80">{run.closed_at ? formatDate(run.closed_at) : "—"}</p>
                  </div>
                </div>

                {run.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {run.tags.map((tag) => (
                      <span
                        key={tag}
                        className="rounded-full border border-border/60 px-2 py-0.5 text-[10px] text-foreground/50"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </Panel>

            {run.decision_events.length > 0 && (
              <Panel eyebrow="timeline" title="Decision events">
                <div className="space-y-2">
                  {run.decision_events.map((event) => (
                    <div
                      key={event.id}
                      className="rounded-xl border border-border/60 bg-background/60 px-3 py-2"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] uppercase tracking-[0.2em] text-foreground/40">
                          {event.event_type}
                        </span>
                        <span className="text-[10px] text-foreground/30">
                          {formatDate(event.created_at)}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-foreground/70">{event.description}</p>
                    </div>
                  ))}
                </div>
              </Panel>
            )}

            <Panel eyebrow="notes" title="Review notes">
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                placeholder="Add your review notes here..."
                rows={4}
                name="journal-review-notes"
                autoComplete="off"
                className="w-full rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm text-foreground/90 placeholder:text-foreground/30 focus:border-primary/40 focus:outline-none"
              />
            </Panel>

            {saveError && (
              <p className="text-sm text-rose-300">{saveError}</p>
            )}

            <div className="flex gap-2">
              <button
                type="button"
                disabled={saving}
                onClick={() => handleUpdateStatus("completed")}
                className="flex-1 rounded-xl border border-emerald-400/30 bg-emerald-400/10 px-4 py-2 text-xs font-medium uppercase tracking-[0.2em] text-emerald-300 transition-colors hover:bg-emerald-400/20 disabled:opacity-50"
              >
                {saving ? "Saving..." : "Mark reviewed"}
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={() => handleUpdateStatus("skipped")}
                className="rounded-xl border border-border/70 bg-background/60 px-4 py-2 text-xs font-medium uppercase tracking-[0.2em] text-foreground/60 transition-colors hover:text-foreground disabled:opacity-50"
              >
                Skip
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
