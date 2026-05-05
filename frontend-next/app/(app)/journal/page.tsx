"use client";

import { useSearchParams, useRouter } from "next/navigation";
import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { AlertCircleIcon } from "lucide-react";

import { fetchDailyView } from "@/lib/journal/api-v2";
import { useWorkspace } from "@/components/workspace/workspace-provider";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricValue } from "@/components/shared/metric-value";
import { PnlBadge } from "@/components/shared/pnl-badge";
import { CostBreakdownTable } from "@/components/shared/cost-breakdown-table";
import type {
  AnalyticsMetrics,
  CostBreakdown,
  JournalV2EpisodeCard,
  JournalV2OpenEpisodeCard,
  JournalV2StrategyGroup,
} from "@/lib/journal/types-v2";
import { cn } from "@/lib/utils";

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
): string {
  const sp = new URLSearchParams();
  const env = params.get("env");
  const mode = params.get("mode");
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

function KpiCard({
  label,
  children,
  className,
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("gap-3 py-4", className)}>
      <CardHeader className="px-4 pb-0 pt-0">
        <CardTitle className="text-[11px] font-medium uppercase tracking-widest text-muted-foreground">
          {label}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4">{children}</CardContent>
    </Card>
  );
}

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
      <KpiCard label="Net P&L">
        <PnlBadge value={metrics.net_pnl} className="text-base font-semibold" />
      </KpiCard>
      <KpiCard label="Gross P&L">
        <PnlBadge value={metrics.gross_pnl} className="text-base font-semibold" />
      </KpiCard>
      <KpiCard label="Total Charges">
        <span className="text-base font-semibold tabular-nums text-[var(--red)]">
          <MetricValue value={fmtNum(metrics.total_charges)} />
        </span>
      </KpiCard>
      <KpiCard label="Episodes">
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
      </KpiCard>
      <KpiCard label="Win Rate">
        <span className="text-base font-semibold tabular-nums">
          <MetricValue value={fmtPct(metrics.win_rate)} />
        </span>
      </KpiCard>
    </div>
  );
}

function OpenEpisodeRow({
  episode,
  params,
}: {
  episode: JournalV2OpenEpisodeCard;
  params: URLSearchParams;
}) {
  const href = buildEpisodeHref(episode.episode_id, params);
  const pnl = episode.current_pnl_estimate;

  return (
    <div className="flex items-center justify-between gap-3 py-1.5 text-sm">
      <Link
        href={href}
        className="truncate font-medium text-foreground hover:underline"
      >
        {episode.strategy?.display_name ?? episode.strategy?.template_key ?? "Episode"}
        <span className="ml-1.5 text-xs font-normal text-muted-foreground">
          {episode.episode_id.slice(0, 8)}
        </span>
      </Link>
      <div className="flex shrink-0 items-center gap-3">
        {episode.direction && (
          <Badge variant="outline" className="text-[10px]">
            {episode.direction}
          </Badge>
        )}
        <PnlBadge value={pnl} />
      </div>
    </div>
  );
}

function EpisodeRow({
  episode,
  params,
}: {
  episode: JournalV2EpisodeCard;
  params: URLSearchParams;
}) {
  const href = buildEpisodeHref(episode.episode_id, params);

  return (
    <div className="flex items-center justify-between gap-3 py-1.5 text-sm">
      <Link
        href={href}
        className="truncate font-medium text-foreground hover:underline"
      >
        {episode.strategy?.display_name ?? episode.strategy?.template_key ?? "Episode"}
        <span className="ml-1.5 text-xs font-normal text-muted-foreground">
          {episode.episode_id.slice(0, 8)}
        </span>
      </Link>
      <div className="flex shrink-0 items-center gap-3">
        {episode.direction && (
          <Badge variant="outline" className="text-[10px]">
            {episode.direction}
          </Badge>
        )}
        <PnlBadge value={episode.outcome.net_pnl} />
      </div>
    </div>
  );
}

function StrategyGroupSection({
  group,
  params,
}: {
  group: JournalV2StrategyGroup;
  params: URLSearchParams;
}) {
  const strategyName =
    group.strategy.display_name ??
    group.strategy.template_key ??
    group.strategy.template_id;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2 py-1">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-foreground">
            {strategyName}
          </span>
          <Badge variant="outline" className="text-[10px]">
            {group.episodes.length} ep
          </Badge>
        </div>
        <PnlBadge value={group.metrics.net_pnl} className="text-xs" />
      </div>
      <div className="divide-y divide-border/50">
        {group.episodes.map((ep) => (
          <EpisodeRow key={ep.episode_id} episode={ep} params={params} />
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
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border py-12 text-center">
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
  const { selectedEnvironmentId } = useWorkspace();

  // Prefer workspace context for environment; URL param as override for deep-links
  const environmentId = selectedEnvironmentId ?? searchParams?.get("env") ?? "";
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
          <div className="mb-2 flex items-center gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Open Episodes
            </h3>
            <Badge variant="secondary" className="text-[10px]">
              {open_episodes.length}
            </Badge>
          </div>
          <Card className="gap-0 py-0">
            <CardContent className="divide-y divide-border/50 px-4 py-2">
              {open_episodes.map((ep) => (
                <OpenEpisodeRow key={ep.episode_id} episode={ep} params={safeParams} />
              ))}
            </CardContent>
          </Card>
        </section>
      )}

      {/* Strategy groups with closed episodes */}
      {hasClosedEpisodes && (
        <section aria-label="Closed episodes by strategy">
          <div className="mb-2 flex items-center gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Closed Episodes
            </h3>
            <Badge variant="secondary" className="text-[10px]">
              {metrics.closed_episode_count}
            </Badge>
          </div>
          <Card className="gap-0 py-0">
            <CardContent className="flex flex-col divide-y divide-border/50 px-4 py-2">
              {strategy_groups.map((group) => (
                <StrategyGroupSection
                  key={group.strategy.template_id}
                  group={group}
                  params={safeParams}
                />
              ))}
            </CardContent>
          </Card>
        </section>
      )}

      {/* Cost breakdown */}
      <section aria-label="Cost breakdown">
        <div className="mb-2">
          <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
            Cost Breakdown
          </h3>
        </div>
        <Card className="py-4">
          <CardContent className="px-4">
            <CostBreakdownTable values={costBreakdown} />
          </CardContent>
        </Card>
      </section>
    </div>
  );
}
