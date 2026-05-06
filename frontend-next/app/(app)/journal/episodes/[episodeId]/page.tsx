"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { useOptionalJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { Panel } from "@/components/operator/panel";
import { KpiCard } from "@/components/operator/kpi-card";
import { StatusBadge } from "@/components/operator/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchEpisodeDetail, patchEpisodeNotes } from "@/lib/journal/api-v2";
import type {
  JournalV2EpisodeDetailResponse,
  JournalV2EpisodeLegView,
  JournalV2ExecutionFillView,
  JournalV2TimelineEventView,
} from "@/lib/journal/types-v2";
import type { AnalysisPeriod } from "@/lib/journal/types";

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmt(value: string | number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined) return "—";
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(n)) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtDt(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function pnlTone(value: string | number | null): "positive" | "danger" | "neutral" {
  if (value === null || value === undefined) return "neutral";
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(n)) return "neutral";
  if (n > 0) return "positive";
  if (n < 0) return "danger";
  return "neutral";
}

function episodeStatusTone(status: string): "positive" | "warning" | "danger" | "neutral" {
  switch (status?.toLowerCase()) {
    case "closed":
      return "positive";
    case "open":
      return "warning";
    case "error":
      return "danger";
    default:
      return "neutral";
  }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-24 w-full rounded-xl" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-20 rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-40 w-full rounded-xl" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Legs table
// ---------------------------------------------------------------------------

function LegsTable({ legs }: { legs: JournalV2EpisodeLegView[] }) {
  if (!legs.length) {
    return (
      <p className="text-sm text-foreground/50">No leg records for this episode.</p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-10">#</TableHead>
            <TableHead>Symbol</TableHead>
            <TableHead>Exchange</TableHead>
            <TableHead>Product</TableHead>
            <TableHead>Direction</TableHead>
            <TableHead className="text-right">Open Qty</TableHead>
            <TableHead className="text-right">Closed Qty</TableHead>
            <TableHead className="text-right">Net Qty</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {legs.map((leg) => (
            <TableRow key={leg.leg_id ?? leg.leg_seq}>
              <TableCell className="text-foreground/50">{leg.leg_seq}</TableCell>
              <TableCell className="font-mono text-sm">
                {leg.tradingsymbol ?? "—"}
              </TableCell>
              <TableCell>{leg.exchange ?? "—"}</TableCell>
              <TableCell>{leg.product ?? "—"}</TableCell>
              <TableCell>
                {leg.direction ? (
                  <Badge variant="outline" className="text-xs uppercase tracking-wide">
                    {leg.direction}
                  </Badge>
                ) : (
                  "—"
                )}
              </TableCell>
              <TableCell className="text-right font-mono">{leg.opened_quantity}</TableCell>
              <TableCell className="text-right font-mono">{leg.closed_quantity}</TableCell>
              <TableCell className="text-right font-mono">{leg.net_quantity}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Fills table
// ---------------------------------------------------------------------------

function FillsTable({ fills }: { fills: JournalV2ExecutionFillView[] }) {
  if (!fills.length) {
    return (
      <p className="text-sm text-foreground/50">No fill records for this episode.</p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Side</TableHead>
            <TableHead className="text-right">Qty</TableHead>
            <TableHead className="text-right">Price</TableHead>
            <TableHead className="text-right">Gross Flow</TableHead>
            <TableHead className="text-right">Fees</TableHead>
            <TableHead className="text-right">Taxes</TableHead>
            <TableHead className="text-right">STT</TableHead>
            <TableHead className="text-right">Brokerage</TableHead>
            <TableHead>Charges</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {fills.map((fill) => (
            <TableRow key={fill.fact_id ?? fill.source_fact_key}>
              <TableCell className="whitespace-nowrap text-xs text-foreground/70">
                {fmtDt(fill.fill_timestamp)}
              </TableCell>
              <TableCell>
                <Badge
                  variant="outline"
                  className={
                    fill.side?.toLowerCase() === "buy"
                      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400 text-xs uppercase tracking-wide"
                      : "border-rose-500/30 bg-rose-500/10 text-rose-400 text-xs uppercase tracking-wide"
                  }
                >
                  {fill.side ?? "—"}
                </Badge>
              </TableCell>
              <TableCell className="text-right font-mono text-sm">{fill.quantity}</TableCell>
              <TableCell className="text-right font-mono text-sm">{fmt(fill.price)}</TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmt(fill.gross_cash_flow)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-foreground/70">
                {fmt(fill.fees_amount)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-foreground/70">
                {fmt(fill.taxes_amount)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-foreground/70">
                {fmt(fill.stt)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-foreground/70">
                {fmt(fill.brokerage)}
              </TableCell>
              <TableCell>
                {fill.charges_status ? (
                  <Badge variant="outline" className="text-xs">
                    {fill.charges_status}
                  </Badge>
                ) : (
                  <span className="text-xs text-foreground/40">—</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Timeline list
// ---------------------------------------------------------------------------

function TimelineList({ events }: { events: JournalV2TimelineEventView[] }) {
  if (!events.length) {
    return <p className="text-sm text-foreground/50">No timeline events yet.</p>;
  }
  return (
    <ol className="flex flex-col gap-2">
      {events.map((evt, idx) => (
        <li
          key={evt.event_id ?? `${evt.event_type}-${idx}`}
          className="rounded-lg border border-border/60 bg-background/60 px-3 py-2"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[11px] uppercase tracking-[0.2em] text-foreground/50">
              {evt.event_type}
            </span>
            {evt.channel ? (
              <Badge variant="outline" className="text-[10px] uppercase tracking-wide">
                {evt.channel}
              </Badge>
            ) : null}
          </div>
          <p className="mt-1 text-xs text-foreground/60">{fmtDt(evt.occurred_at)}</p>
          {evt.actor_type ? (
            <p className="text-[10px] text-foreground/40">actor: {evt.actor_type}</p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Notes panel (explicit save button)
// ---------------------------------------------------------------------------

type NotesPanelProps = {
  initialNotes: string;
  environmentId: string;
  episodeId: string;
  onSaved: (notes: string) => void;
};

function NotesPanel({ initialNotes, environmentId, episodeId, onSaved }: NotesPanelProps) {
  const [draft, setDraft] = useState(initialNotes);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<Date | null>(null);
  const dirty = draft !== initialNotes;

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    try {
      const res = await patchEpisodeNotes({
        environment_id: environmentId,
        episode_id: episodeId,
        notes: draft,
      });
      onSaved(res.notes ?? draft);
      setSavedAt(new Date());
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Failed to save notes");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <Textarea
        aria-label="Episode notes"
        rows={6}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder="Write observations, trade rationale, mistakes, or follow-up actions…"
        className="resize-y font-mono text-sm"
      />
      {saveError ? (
        <p className="text-xs text-destructive">{saveError}</p>
      ) : null}
      {savedAt && !dirty ? (
        <p className="text-xs text-foreground/50">Saved {savedAt.toLocaleTimeString()}</p>
      ) : null}
      <Button
        onClick={handleSave}
        disabled={saving || !dirty}
        size="sm"
        className="self-end"
      >
        {saving ? "Saving…" : "Save Notes"}
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main detail content
// ---------------------------------------------------------------------------

type EpisodeDetailPageProps = {
  params: { episodeId: string };
};

export default function JournalEpisodeDetailPage(props: EpisodeDetailPageProps) {
  return (
    <Suspense
      fallback={
        <div className="p-4 text-sm text-foreground/60">Loading episode…</div>
      }
    >
      <EpisodeDetailContent {...props} />
    </Suspense>
  );
}

function EpisodeDetailContent({ params }: EpisodeDetailPageProps) {
  const searchParams = useSearchParams();
  const workspace = useOptionalJournalWorkspace();
  const [period, setPeriod] = useState<AnalysisPeriod>("month");

  const environmentId =
    searchParams?.get("environment_id")?.trim() ||
    searchParams?.get("env")?.trim() ||
    workspace?.selectedEnvironmentId ||
    "";

  const requestKey = environmentId ? `${params.episodeId}:${environmentId}` : "";

  const [detail, setDetail] = useState<JournalV2EpisodeDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolvedKey, setResolvedKey] = useState("");
  // Track current notes separately so save can update without refetch
  const [notes, setNotes] = useState("");
  const notesInitRef = useRef(false);

  useEffect(() => {
    if (!environmentId) return;
    let cancelled = false;

    fetchEpisodeDetail({
      environment_id: environmentId,
      episode_id: params.episodeId,
    })
      .then((res) => {
        if (!cancelled) {
          setDetail(res);
          setError(null);
          setResolvedKey(requestKey);
          if (!notesInitRef.current) {
            setNotes(res.notes ?? "");
            notesInitRef.current = true;
          }
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setDetail(null);
          setError(err instanceof Error ? err.message : "Failed to load episode");
          setResolvedKey(requestKey);
        }
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey]);

  const isLoading = Boolean(requestKey) && resolvedKey !== requestKey;
  const displayDetail = requestKey && resolvedKey === requestKey ? detail : null;
  const displayError = requestKey && resolvedKey === requestKey ? error : null;

  const episode = displayDetail?.episode ?? null;
  const legs = displayDetail?.legs ?? [];
  const fills = displayDetail?.fills ?? [];
  const timeline = displayDetail?.timeline ?? [];
  const outcome = episode?.outcome ?? null;

  const strategyLabel =
    episode?.strategy?.display_name ||
    episode?.strategy?.template_key ||
    episode?.strategy?.strategy_family ||
    null;

  const backHref = environmentId
    ? `/journal/episodes?environment_id=${encodeURIComponent(environmentId)}`
    : "/journal/episodes";

  return (
    <div className="flex flex-col gap-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      {/* No env guard */}
      {!environmentId ? (
        <Panel className="p-4 md:p-5">
          <p className="text-sm text-foreground/70">
            Add <span className="font-mono text-foreground/90">environment_id</span> to the URL
            or select an environment to load this episode.
          </p>
        </Panel>
      ) : null}

      {/* Header panel */}
      <Panel
        eyebrow={
          strategyLabel
            ? `Strategy · ${strategyLabel}`
            : episode
              ? `Episode ${params.episodeId}`
              : "Episode detail"
        }
        title={
          episode
            ? `Episode ${episode.episode_id.slice(0, 8)}…`
            : `Episode ${params.episodeId}`
        }
        action={
          <Link
            href={backHref}
            className="rounded-full border border-border/70 bg-background/60 px-3 py-1.5 text-xs font-medium uppercase tracking-[0.2em] text-foreground/70 transition-colors hover:bg-background/90"
          >
            ← Episodes
          </Link>
        }
        className="p-4 md:p-5"
      >
        {isLoading ? <DetailSkeleton /> : null}

        {displayError ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load episode</AlertTitle>
            <AlertDescription>{displayError}</AlertDescription>
          </Alert>
        ) : null}

        {!isLoading && !displayError && episode ? (
          <div className="flex flex-col gap-4">
            {/* Status badges row */}
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge tone={episodeStatusTone(episode.status)}>
                {episode.status}
              </StatusBadge>
              {episode.direction ? (
                <StatusBadge tone="neutral">{episode.direction}</StatusBadge>
              ) : null}
              {episode.fill_count > 0 ? (
                <Badge variant="outline" className="text-xs">
                  {episode.fill_count} fill{episode.fill_count !== 1 ? "s" : ""}
                </Badge>
              ) : null}
              {episode.leg_count > 0 ? (
                <Badge variant="outline" className="text-xs">
                  {episode.leg_count} leg{episode.leg_count !== 1 ? "s" : ""}
                </Badge>
              ) : null}
            </div>

            {/* Timestamps row */}
            <div className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2">
                <p className="text-[10px] uppercase tracking-[0.22em] text-foreground/40">Opened</p>
                <p className="mt-1 font-mono text-sm text-foreground/80">
                  {fmtDt(episode.opened_at)}
                </p>
              </div>
              <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2">
                <p className="text-[10px] uppercase tracking-[0.22em] text-foreground/40">Closed</p>
                <p className="mt-1 font-mono text-sm text-foreground/80">
                  {episode.closed_at ? fmtDt(episode.closed_at) : (
                    <span className="text-amber-400">Still open</span>
                  )}
                </p>
              </div>
            </div>

            {/* KPI grid */}
            {outcome ? (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <KpiCard
                  label="Net P&L"
                  value={`₹${fmt(outcome.net_pnl)}`}
                />
                <KpiCard
                  label="Gross P&L"
                  value={`₹${fmt(outcome.gross_pnl)}`}
                />
                <KpiCard
                  label="Total Charges"
                  value={`₹${fmt(outcome.total_charges)}`}
                />
                <KpiCard
                  label="Realized P&L"
                  value={`₹${fmt(outcome.realized_pnl)}`}
                />
              </div>
            ) : null}

            {/* Cost breakdown */}
            {outcome?.cost_breakdown ? (
              <div className="rounded-lg border border-border/60 bg-background/30 px-4 py-3">
                <p className="mb-2 text-[10px] uppercase tracking-[0.22em] text-foreground/40">
                  Cost Breakdown
                </p>
                <div className="flex flex-wrap gap-4 text-xs text-foreground/70">
                  {[
                    ["Brokerage", outcome.cost_breakdown.brokerage],
                    ["Exch TXN", outcome.cost_breakdown.exchange_txn_charge],
                    ["STT", outcome.cost_breakdown.stt],
                    ["Stamp Duty", outcome.cost_breakdown.stamp_duty],
                    ["SEBI", outcome.cost_breakdown.sebi_charge],
                    ["GST", outcome.cost_breakdown.gst],
                    ["Total Taxes", outcome.cost_breakdown.total_taxes],
                  ].map(([label, val]) => (
                    <span key={label as string} className="flex gap-1.5">
                      <span className="text-foreground/40">{label as string}</span>
                      <span className="font-mono">₹{fmt(val as string | number)}</span>
                    </span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {!isLoading && !displayError && !episode && environmentId ? (
          <p className="text-sm text-foreground/50">Episode not found.</p>
        ) : null}
      </Panel>

      {/* Legs + Fills */}
      {environmentId ? (
        <div className="flex flex-col gap-5">
          <Panel eyebrow="Positions" title="Legs" className="p-4 md:p-5">
            {isLoading ? (
              <Skeleton className="h-24 w-full rounded-lg" />
            ) : (
              <LegsTable legs={legs} />
            )}
          </Panel>

          <Panel eyebrow="Executions" title="Fills" className="p-4 md:p-5">
            {isLoading ? (
              <Skeleton className="h-32 w-full rounded-lg" />
            ) : (
              <FillsTable fills={fills} />
            )}
          </Panel>
        </div>
      ) : null}

      {/* Timeline + Notes */}
      {environmentId ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel eyebrow="Activity" title="Timeline" className="p-4 md:p-5">
            {isLoading ? (
              <div className="flex flex-col gap-2">
                {[1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-14 w-full rounded-lg" />
                ))}
              </div>
            ) : (
              <TimelineList events={timeline} />
            )}
          </Panel>

          <Panel eyebrow="Review" title="Notes" className="p-4 md:p-5">
            {isLoading ? (
              <Skeleton className="h-36 w-full rounded-lg" />
            ) : (
              <NotesPanel
                initialNotes={notes}
                environmentId={environmentId}
                episodeId={params.episodeId}
                onSaved={(saved) => setNotes(saved)}
              />
            )}
          </Panel>
        </div>
      ) : null}
    </div>
  );
}
