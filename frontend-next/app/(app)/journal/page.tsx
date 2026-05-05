"use client";

import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { AlertCircleIcon, CalendarDaysIcon } from "lucide-react";

import { fetchDailyView } from "@/lib/journal/api-v2";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { cn } from "@/lib/utils";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricValue } from "@/components/shared/metric-value";
import { PnlBadge } from "@/components/shared/pnl-badge";
import { CostBreakdownTable } from "@/components/shared/cost-breakdown-table";
import { JournalKpiCard } from "@/components/journal/journal-kpi-card";
import type {
  AnalyticsMetrics,
  CostBreakdown,
  JournalV2EpisodeCard,
  JournalV2OpenEpisodeCard,
  JournalV2StrategyGroup,
} from "@/lib/journal/types-v2";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function todayIso(): string {
  const d = new Date();
  return [
    d.getFullYear(),
    String(d.getMonth() + 1).padStart(2, "0"),
    String(d.getDate()).padStart(2, "0"),
  ].join("-");
}

function fmtNum(v: string | number | null | undefined, dp = 2): string | null {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || isNaN(n)) return null;
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  });
}

function fmtPct(v: string | number | null | undefined): string | null {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || isNaN(n)) return null;
  return `${n.toFixed(1)}%`;
}

function costBreakdownNums(cb: CostBreakdown) {
  return {
    brokerage: Number(cb.brokerage) || 0,
    exchange_txn_charge: Number(cb.exchange_txn_charge) || 0,
    stt: Number(cb.stt) || 0,
    stamp_duty: Number(cb.stamp_duty) || 0,
    sebi_charge: Number(cb.sebi_charge) || 0,
    gst: Number(cb.gst) || 0,
    total_taxes: Number(cb.total_taxes) || 0,
    total_charges: Number(cb.total_charges) || 0,
  };
}

function buildEpisodeHref(
  episodeId: string,
  params: URLSearchParams,
  workspace: { env?: string; mode?: string },
): string {
  const sp = new URLSearchParams();
  const env = params.get("env") ?? workspace.env;
  const mode = params.get("mode") ?? workspace.mode;
  const date = params.get("date");
  if (env) sp.set("env", env);
  if (mode) sp.set("mode", mode);
  if (date) sp.set("date", date);
  const qs = sp.toString();
  return `/journal/episodes/${episodeId}${qs ? `?${qs}` : ""}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function KpiSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {Array.from({ length: 5 }).map((_, i) => (
        <Skeleton key={i} className="h-20 rounded-xl" />
      ))}
    </div>
  );
}

function SummaryKpis({ metrics, openCount }: { metrics: AnalyticsMetrics; openCount: number }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      <JournalKpiCard label="Net P&L">
        <PnlBadge value={metrics.net_pnl} className="text-base font-semibold" />
      </JournalKpiCard>
      <JournalKpiCard label="Gross P&L">
        <PnlBadge value={metrics.gross_pnl} className="text-base font-semibold" />
      </JournalKpiCard>
      <JournalKpiCard label="Total Charges">
        <PnlBadge value={-Math.abs(Number(metrics.total_charges) || 0)} showSign={false} className="text-base font-semibold" />
      </JournalKpiCard>
      <JournalKpiCard label="Episodes">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-base font-semibold tabular-nums">
            {metrics.closed_episode_count}
          </span>
          <span className="text-xs text-muted-foreground">closed</span>
          {openCount > 0 && (
            <Badge variant="secondary" className="text-[10px]">
              {openCount} open
            </Badge>
          )}
        </div>
      </JournalKpiCard>
      <JournalKpiCard label="Win Rate">
        <span className="text-base font-semibold tabular-nums">
          <MetricValue value={fmtPct(metrics.win_rate)} />
        </span>
      </JournalKpiCard>
    </div>
  );
}

function directionToneClass(direction?: string | null) {
  const normalized = String(direction ?? "").trim().toLowerCase();
  if (normalized === "long") return "bg-sky-500/10 text-sky-300";
  if (normalized === "short") return "bg-orange-500/10 text-orange-300";
  return "bg-foreground/[0.06] text-foreground/65";
}

function SectionHeader({
  label,
  count,
}: {
  label: string;
  count?: string;
}) {
  return (
    <div className="mb-2 flex items-center justify-between gap-3">
      <h3 className="text-[11px] font-medium uppercase tracking-[0.22em] text-muted-foreground/70">
        {label}
      </h3>
      {count ? <span className="font-mono text-[10px] text-muted-foreground/60">{count}</span> : null}
    </div>
  );
}

function OpenEpisodeRow({
  episode,
  params,
  workspace,
}: {
  episode: JournalV2OpenEpisodeCard;
  params: URLSearchParams;
  workspace: { env?: string; mode?: string };
}) {
  const href = buildEpisodeHref(episode.episode_id, params, workspace);
  const pnl = episode.current_pnl_estimate;

  return (
    <div className="flex items-center justify-between gap-3 py-2 text-sm">
      <div className="min-w-0 flex-1">
        <Link href={href} className="truncate font-medium text-foreground hover:underline">
          {episode.strategy?.display_name ?? episode.strategy?.template_key ?? "Episode"}
        </Link>
        <span className="ml-2 font-mono text-[10px] text-muted-foreground/40">
          {episode.episode_id.slice(0, 8)}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-3">
        {episode.direction && (
          <span className={cn("rounded-sm px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em]", directionToneClass(episode.direction))}>
            {episode.direction}
          </span>
        )}
        <PnlBadge value={pnl} className="text-sm font-semibold" />
      </div>
    </div>
  );
}

function EpisodeRow({
  episode,
  params,
  workspace,
}: {
  episode: JournalV2EpisodeCard;
  params: URLSearchParams;
  workspace: { env?: string; mode?: string };
}) {
  const href = buildEpisodeHref(episode.episode_id, params, workspace);

  return (
    <div className="flex items-center justify-between gap-3 py-2 text-sm">
      <div className="min-w-0 flex-1">
        <Link href={href} className="truncate font-medium text-foreground hover:underline">
          {episode.strategy?.display_name ?? episode.strategy?.template_key ?? "Episode"}
        </Link>
        <span className="ml-2 font-mono text-[10px] text-muted-foreground/40">
          {episode.episode_id.slice(0, 8)}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-3">
        {episode.direction && (
          <span className={cn("rounded-sm px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em]", directionToneClass(episode.direction))}>
            {episode.direction}
          </span>
        )}
        <PnlBadge value={episode.outcome.net_pnl} className="text-sm font-semibold" />
      </div>
    </div>
  );
}

function StrategyGroupSection({
  group,
  params,
  workspace,
}: {
  group: JournalV2StrategyGroup;
  params: URLSearchParams;
  workspace: { env?: string; mode?: string };
}) {
  const strategyName =
    group.strategy.display_name ??
    group.strategy.template_key ??
    group.strategy.template_id;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2 py-1.5">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-medium text-foreground">
            {strategyName}
          </span>
          <span className="font-mono text-[10px] text-muted-foreground/60">{group.episodes.length} ep</span>
        </div>
        <PnlBadge value={group.metrics.net_pnl} className="text-sm font-semibold" />
      </div>
      <div className="divide-y divide-border/50">
        {group.episodes.map((ep) => (
          <EpisodeRow key={ep.episode_id} episode={ep} params={params} workspace={workspace} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton sections
// ---------------------------------------------------------------------------

function DayViewSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <KpiSkeleton />
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-24 rounded-xl" />
      </div>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-48" />
        <Skeleton className="h-32 rounded-xl" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyDayState({ date }: { date: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-border/40 bg-muted/10 px-6 py-12 text-center">
      <CalendarDaysIcon className="h-4 w-4 text-muted-foreground/40" />
      <p className="text-sm font-medium text-muted-foreground">
        No trading activity on {date}
      </p>
      <p className="text-xs text-muted-foreground/70">
        Use the date navigator to move to a different day.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function JournalDayPage() {
  const searchParams = useSearchParams();
  const { selectedEnvironmentId, selectedMode } = useWorkspace();

  // Prefer workspace context for environment; URL param as override for deep-links
  const environmentId = searchParams?.get("env") ?? selectedEnvironmentId ?? "";
  const date = searchParams?.get("date") ?? todayIso();

  const queryEnabled = Boolean(environmentId);

  const { data, isLoading, error } = useQuery({
    queryKey: ["journal", "daily", environmentId, date],
    queryFn: () => fetchDailyView({ environment_id: environmentId, date }),
    enabled: queryEnabled,
    staleTime: 60_000,
  });

  // Stable fallback so sub-components never receive null
  const safeParams = searchParams ?? new URLSearchParams();
  const linkScope = {
    env: safeParams.get("env") ?? selectedEnvironmentId ?? undefined,
    mode: safeParams.get("mode") ?? selectedMode,
  };

  // No environment selected
  if (!queryEnabled) {
    return (
      <div className="rounded-xl border border-dashed border-border/70 bg-background/40 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view the daily journal.
      </div>
    );
  }

  // Loading
  if (isLoading) {
    return <DayViewSkeleton />;
  }

  // Error
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Failed to load daily view</AlertTitle>
        <AlertDescription>
          {error instanceof Error ? error.message : "Unknown error"}
        </AlertDescription>
      </Alert>
    );
  }

  // No data
  if (!data) {
    return <EmptyDayState date={date} />;
  }

  const { summary, strategy_groups, open_episodes } = data;
  const { metrics } = summary;
  const hasClosedEpisodes = strategy_groups.length > 0;
  const hasOpenEpisodes = open_episodes.length > 0;
  const hasAnyActivity = hasClosedEpisodes || hasOpenEpisodes;

  if (!hasAnyActivity) {
    return <EmptyDayState date={date} />;
  }

  const costBreakdown = costBreakdownNums(metrics.cost_breakdown);

  return (
    <div className="flex flex-col gap-6 pb-8">
      {/* KPI cards */}
      <SummaryKpis metrics={metrics} openCount={summary.open_episode_count} />

      {/* Open episodes */}
      {hasOpenEpisodes && (
        <section aria-label="Open episodes">
          <SectionHeader label="Open Episodes" count={`${open_episodes.length} active`} />
          <Card className="gap-0 py-0">
            <CardContent className="divide-y divide-border/50 px-3 py-1.5">
              {open_episodes.map((ep) => (
                <OpenEpisodeRow key={ep.episode_id} episode={ep} params={safeParams} workspace={linkScope} />
              ))}
            </CardContent>
          </Card>
        </section>
      )}

      {/* Strategy groups with closed episodes */}
      {hasClosedEpisodes && (
        <section aria-label="Closed episodes by strategy">
          <SectionHeader label="Closed Episodes" count={`${metrics.closed_episode_count} closed`} />
          <Card className="gap-0 py-0">
            <CardContent className="flex flex-col divide-y divide-border/50 px-3 py-1.5">
              {strategy_groups.map((group) => (
                <StrategyGroupSection
                  key={group.strategy.template_id}
                  group={group}
                  params={safeParams}
                  workspace={linkScope}
                />
              ))}
            </CardContent>
          </Card>
        </section>
      )}

      {/* Cost breakdown */}
      <section aria-label="Cost breakdown">
        <SectionHeader label="Cost Breakdown" />
        <div className="rounded-lg border border-border/50 bg-muted/20 px-3 py-3">
          <CostBreakdownTable values={costBreakdown} />
        </div>
      </section>
    </div>
  );
}
