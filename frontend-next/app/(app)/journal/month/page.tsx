"use client";

import { useSearchParams, useRouter } from "next/navigation";
import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { AlertCircleIcon } from "lucide-react";

import { fetchPeriodView } from "@/lib/journal/api-v2";
import { useWorkspace } from "@/components/workspace/workspace-provider";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { DateNav } from "@/components/shared/date-nav";
import { MetricValue } from "@/components/shared/metric-value";
import { PnlBadge } from "@/components/shared/pnl-badge";
import { CostBreakdownTable } from "@/components/shared/cost-breakdown-table";
import {
  PeriodKpiGridExtended,
  PeriodKpiSkeleton,
  StrategySummaryTable,
} from "@/components/journal/period-kpi-grid";
import { CalendarHeatmap } from "@/components/journal/calendar-heatmap";
import type { JournalV2PeriodBucket } from "@/lib/journal/types-v2";

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------

function parseDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 12));
}

function toIso(d: Date): string {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function todayIso(): string {
  return toIso(new Date());
}

/**
 * Returns [first day of month, last day of month] for the month containing `iso`.
 */
function monthRange(iso: string): [string, string] {
  const d = parseDate(iso);
  const y = d.getUTCFullYear();
  const m = d.getUTCMonth();
  const first = new Date(Date.UTC(y, m, 1, 12));
  const last = new Date(Date.UTC(y, m + 1, 0, 12));
  return [toIso(first), toIso(last)];
}

/**
 * Returns the Monday of the ISO week containing `iso`.
 */
function weekMonday(iso: string): string {
  const d = parseDate(iso);
  const dow = d.getUTCDay(); // 0=Sun
  const monday = new Date(d);
  monday.setUTCDate(d.getUTCDate() - ((dow + 6) % 7));
  return toIso(monday);
}

function formatWeekLabel(bucket: { bucket_start: string; bucket_end: string; label: string }): string {
  // Prefer server label if it is set and non-trivial
  if (bucket.label && bucket.label.length > 2) return bucket.label;
  // Fallback: build it from dates
  const s = parseDate(bucket.bucket_start);
  const e = parseDate(bucket.bucket_end);
  const fmtShort = (d: Date) =>
    d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "UTC" });
  return `${fmtShort(s)} – ${fmtShort(e)}`;
}

function fmtNum(v: string | number | null | undefined, dp = 2): string | null {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || isNaN(n)) return null;
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  });
}

// ---------------------------------------------------------------------------
// Build link preserving env/mode search params
// ---------------------------------------------------------------------------

function buildHref(
  base: string,
  date: string,
  params: URLSearchParams,
  workspace: { env?: string; mode?: string },
): string {
  const sp = new URLSearchParams();
  const env = params.get("env") ?? workspace.env;
  const mode = params.get("mode") ?? workspace.mode;
  if (env) sp.set("env", env);
  if (mode) sp.set("mode", mode);
  sp.set("date", date);
  return `${base}?${sp.toString()}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function WeekRow({
  bucket,
  params,
  workspace,
}: {
  bucket: JournalV2PeriodBucket;
  params: URLSearchParams;
  workspace: { env?: string; mode?: string };
}) {
  // Link to the week page anchored to the Monday of this bucket
  const monday = weekMonday(bucket.bucket_start);
  const href = buildHref("/journal/week", monday, params, workspace);
  const { metrics } = bucket;

  return (
    <div className="flex items-center justify-between gap-3 py-1.5 text-xs">
      <Link
        href={href}
        className="min-w-[10rem] font-medium text-foreground hover:underline"
      >
        {formatWeekLabel(bucket)}
      </Link>

      <div className="flex shrink-0 items-center gap-4 text-right tabular-nums">
        <span className="hidden w-14 text-muted-foreground sm:block">
          {metrics.closed_episode_count > 0
            ? `${metrics.closed_episode_count} ep`
            : "—"}
        </span>
        <span className="hidden w-14 text-muted-foreground sm:block">
          <MetricValue
            value={
              metrics.win_rate !== null && metrics.win_rate !== undefined
                ? `${Number(metrics.win_rate).toFixed(0)}%`
                : null
            }
          />
        </span>
        <PnlBadge value={metrics.net_pnl} className="w-20 justify-end text-xs" />
      </div>
    </div>
  );
}

function MonthViewSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <PeriodKpiSkeleton count={6} />
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-40 rounded-xl" />
      </div>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-24 rounded-xl" />
      </div>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-36" />
        <Skeleton className="h-20 rounded-xl" />
      </div>
    </div>
  );
}

function EmptyMonthState({ from, to }: { from: string; to: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border py-12 text-center">
      <p className="text-sm font-medium text-muted-foreground">
        No trading activity for {from} – {to}
      </p>
      <p className="text-xs text-muted-foreground/70">
        Use the navigator above to move to a different month.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function JournalMonthPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { selectedEnvironmentId, selectedMode } = useWorkspace();

  const environmentId = searchParams?.get("env") ?? selectedEnvironmentId ?? "";
  const anchorDate = searchParams?.get("date") ?? todayIso();
  const [from, to] = monthRange(anchorDate);

  const queryEnabled = Boolean(environmentId);

  const { data, isLoading, error } = useQuery({
    queryKey: ["journal", "period", environmentId, from, to, "week"],
    queryFn: () =>
      fetchPeriodView({
        environment_id: environmentId,
        from,
        to,
        granularity: "week",
      }),
    enabled: queryEnabled,
    staleTime: 60_000,
  });

  const {
    data: calendarData,
    isLoading: isCalendarLoading,
    error: calendarError,
  } = useQuery({
    queryKey: ["journal", "period", environmentId, from, to, "day", "calendar"],
    queryFn: () =>
      fetchPeriodView({
        environment_id: environmentId,
        from,
        to,
        granularity: "day",
      }),
    enabled: queryEnabled,
    staleTime: 60_000,
  });

  const safeParams = searchParams ?? new URLSearchParams();
  const linkScope = {
    env: safeParams.get("env") ?? selectedEnvironmentId ?? undefined,
    mode: safeParams.get("mode") ?? selectedMode,
  };

  const handleDateChange = useCallback(
    (newDate: string) => {
      const params = new URLSearchParams(safeParams.toString());
      params.set("date", newDate);
      router.push(`/journal/month?${params.toString()}`);
    },
    [router, safeParams],
  );

  if (!queryEnabled) {
    return (
      <div className="rounded-xl border border-dashed border-border/70 bg-background/40 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view the monthly journal.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 pb-8">
      {/* Month DateNav */}
      <div className="flex items-center gap-2">
        <DateNav date={anchorDate} view="month" onChange={handleDateChange} />
      </div>

      {isLoading ? (
        <MonthViewSkeleton />
      ) : error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Failed to load month view</AlertTitle>
          <AlertDescription>
            {error instanceof Error ? error.message : "Unknown error"}
          </AlertDescription>
        </Alert>
      ) : !data ? (
        <EmptyMonthState from={from} to={to} />
      ) : (
        <MonthContent
          data={data}
          params={safeParams}
          from={from}
          to={to}
          workspace={linkScope}
          calendarDays={
            calendarData?.buckets.map((bucket) => ({
              date: bucket.bucket_start,
              net_pnl: Number(bucket.metrics.net_pnl) || 0,
              run_count: bucket.metrics.closed_episode_count,
              win_count: bucket.metrics.win_count,
              loss_count: bucket.metrics.loss_count,
            })) ?? []
          }
          calendarLoading={isCalendarLoading}
          calendarError={
            calendarError
              ? calendarError instanceof Error
                ? calendarError.message
                : "Failed to load calendar"
              : null
          }
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Content
// ---------------------------------------------------------------------------

type PeriodData = Awaited<ReturnType<typeof fetchPeriodView>>;

function MonthContent({
  data,
  params,
  from,
  to,
  workspace,
  calendarDays,
  calendarLoading,
  calendarError,
}: {
  data: PeriodData;
  params: URLSearchParams;
  from: string;
  to: string;
  workspace: { env?: string; mode?: string };
  calendarDays: Array<{
    date: string;
    net_pnl: number;
    run_count: number;
    win_count: number;
    loss_count: number;
  }>;
  calendarLoading: boolean;
  calendarError: string | null;
}) {
  const { summary, buckets, strategies } = data;
  const hasBuckets = buckets.length > 0;
  const hasStrategies = strategies.length > 0;
  const hasActivity = hasBuckets || summary.closed_episode_count > 0;

  if (!hasActivity) {
    return <EmptyMonthState from={from} to={to} />;
  }

  const costBreakdown = {
    brokerage: Number(summary.cost_breakdown?.brokerage) || 0,
    exchange_txn_charge: Number(summary.cost_breakdown?.exchange_txn_charge) || 0,
    stt: Number(summary.cost_breakdown?.stt) || 0,
    stamp_duty: Number(summary.cost_breakdown?.stamp_duty) || 0,
    sebi_charge: Number(summary.cost_breakdown?.sebi_charge) || 0,
    gst: Number(summary.cost_breakdown?.gst) || 0,
    total_taxes: Number(summary.cost_breakdown?.total_taxes) || 0,
    total_charges: Number(summary.cost_breakdown?.total_charges) || 0,
  };

  return (
    <div className="flex flex-col gap-6">
      {/* KPI summary — extended for month */}
      <PeriodKpiGridExtended metrics={summary} />

      {/* Weekly breakdown */}
      {hasBuckets && (
        <section aria-label="Weekly breakdown">
          <div className="mb-2 flex items-center gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Weekly Breakdown
            </h3>
            <Badge variant="secondary" className="text-[10px]">
              {buckets.length} weeks
            </Badge>
          </div>
          <Card className="gap-0 py-0">
            <CardContent className="px-4 py-2">
              {/* Column headers */}
              <div className="flex items-center justify-between gap-3 pb-1 text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                <span>Week</span>
                <div className="flex shrink-0 items-center gap-4">
                  <span className="hidden w-14 text-right sm:block">Episodes</span>
                  <span className="hidden w-14 text-right sm:block">Win %</span>
                  <span className="w-20 text-right">Net P&L</span>
                </div>
              </div>
              <Separator className="mb-1" />
              <div className="divide-y divide-border/50">
                {buckets.map((bucket) => (
                  <WeekRow
                    key={bucket.bucket_start}
                    bucket={bucket}
                    params={params}
                    workspace={workspace}
                  />
                ))}
              </div>
            </CardContent>
          </Card>
        </section>
      )}

      {/* Calendar intelligence */}
      <section aria-label="Monthly calendar heatmap">
        <CalendarHeatmap
          days={calendarDays}
          loading={calendarLoading}
          error={calendarError}
          month={parseDate(from).getUTCMonth() + 1}
          year={parseDate(from).getUTCFullYear()}
          env={params.get("env") ?? workspace.env}
          mode={params.get("mode") ?? workspace.mode}
        />
      </section>

      {/* Strategy summary */}
      {hasStrategies && (
        <section aria-label="Strategy performance">
          <div className="mb-2">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Strategy Performance
            </h3>
          </div>
          <Card className="py-4">
            <CardContent className="px-4">
              <StrategySummaryTable strategies={strategies} />
            </CardContent>
          </Card>
        </section>
      )}

      {/* Monthly cost breakdown */}
      <section aria-label="Cost breakdown">
        <div className="mb-2">
          <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
            Monthly Cost Breakdown
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
