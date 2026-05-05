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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { DateNav } from "@/components/shared/date-nav";
import { MetricValue } from "@/components/shared/metric-value";
import { PnlBadge } from "@/components/shared/pnl-badge";
import { CostBreakdownTable } from "@/components/shared/cost-breakdown-table";
import {
  PeriodKpiGrid,
  PeriodKpiSkeleton,
  StrategySummaryTable,
} from "@/components/journal/period-kpi-grid";
import type { JournalV2PeriodBucket } from "@/lib/journal/types-v2";

// ---------------------------------------------------------------------------
// Date helpers — ISO-week math, Monday start
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
 * Returns [monday, sunday] ISO strings for the ISO week containing `date`.
 */
function weekRange(iso: string): [string, string] {
  const d = parseDate(iso);
  const dow = d.getUTCDay(); // 0=Sun
  const monday = new Date(d);
  monday.setUTCDate(d.getUTCDate() - ((dow + 6) % 7));
  const sunday = new Date(monday);
  sunday.setUTCDate(monday.getUTCDate() + 6);
  return [toIso(monday), toIso(sunday)];
}

function formatDay(iso: string): string {
  const d = parseDate(iso);
  return d.toLocaleDateString("en-IN", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
  });
}

// ---------------------------------------------------------------------------
// Build link preserving env/mode/date search params
// ---------------------------------------------------------------------------

function buildHref(
  base: string,
  date: string,
  params: URLSearchParams,
): string {
  const sp = new URLSearchParams();
  const env = params.get("env");
  const mode = params.get("mode");
  if (env) sp.set("env", env);
  if (mode) sp.set("mode", mode);
  sp.set("date", date);
  return `${base}?${sp.toString()}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function DayRow({
  bucket,
  params,
}: {
  bucket: JournalV2PeriodBucket;
  params: URLSearchParams;
}) {
  const href = buildHref("/journal", bucket.bucket_start, params);
  const { metrics } = bucket;

  return (
    <div className="flex items-center justify-between gap-3 py-1.5 text-xs">
      <Link
        href={href}
        className="min-w-[6rem] font-medium text-foreground hover:underline"
      >
        {formatDay(bucket.bucket_start)}
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

function WeekViewSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <PeriodKpiSkeleton count={5} />
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-40 rounded-xl" />
      </div>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-24 rounded-xl" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function JournalWeekPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { selectedEnvironmentId } = useWorkspace();

  const environmentId = selectedEnvironmentId ?? searchParams?.get("env") ?? "";
  const anchorDate = searchParams?.get("date") ?? todayIso();
  const [from, to] = weekRange(anchorDate);

  const queryEnabled = Boolean(environmentId);

  const { data, isLoading, error } = useQuery({
    queryKey: ["journal", "period", environmentId, from, to, "day"],
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

  const handleDateChange = useCallback(
    (newDate: string) => {
      const params = new URLSearchParams(safeParams.toString());
      params.set("date", newDate);
      router.push(`/journal/week?${params.toString()}`);
    },
    [router, safeParams],
  );

  if (!queryEnabled) {
    return (
      <div className="rounded-xl border border-dashed border-border/70 bg-background/40 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view the weekly journal.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 pb-8">
      {/* Week DateNav */}
      <div className="flex items-center gap-2">
        <DateNav date={anchorDate} view="week" onChange={handleDateChange} />
      </div>

      {isLoading ? (
        <WeekViewSkeleton />
      ) : error ? (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Failed to load week view</AlertTitle>
          <AlertDescription>
            {error instanceof Error ? error.message : "Unknown error"}
          </AlertDescription>
        </Alert>
      ) : !data ? (
        <EmptyWeekState from={from} to={to} />
      ) : (
        <WeekContent data={data} params={safeParams} from={from} to={to} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Content
// ---------------------------------------------------------------------------

type PeriodData = Awaited<ReturnType<typeof fetchPeriodView>>;

function WeekContent({
  data,
  params,
  from,
  to,
}: {
  data: PeriodData;
  params: URLSearchParams;
  from: string;
  to: string;
}) {
  const { summary, buckets, strategies } = data;
  const hasBuckets = buckets.length > 0;
  const hasStrategies = strategies.length > 0;
  const hasActivity =
    hasBuckets || summary.closed_episode_count > 0;

  if (!hasActivity) {
    return <EmptyWeekState from={from} to={to} />;
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
      {/* KPI summary */}
      <PeriodKpiGrid metrics={summary} />

      {/* Daily breakdown */}
      {hasBuckets && (
        <section aria-label="Daily breakdown">
          <div className="mb-2 flex items-center gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Daily Breakdown
            </h3>
            <Badge variant="secondary" className="text-[10px]">
              {buckets.length} days
            </Badge>
          </div>
          <Card className="gap-0 py-0">
            <CardContent className="px-4 py-2">
              {/* Column headers */}
              <div className="flex items-center justify-between gap-3 pb-1 text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                <span>Date</span>
                <div className="flex shrink-0 items-center gap-4">
                  <span className="hidden w-14 text-right sm:block">Episodes</span>
                  <span className="hidden w-14 text-right sm:block">Win %</span>
                  <span className="w-20 text-right">Net P&L</span>
                </div>
              </div>
              <Separator className="mb-1" />
              <div className="divide-y divide-border/50">
                {buckets.map((bucket) => (
                  <DayRow
                    key={bucket.bucket_start}
                    bucket={bucket}
                    params={params}
                  />
                ))}
              </div>
            </CardContent>
          </Card>
        </section>
      )}

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

function EmptyWeekState({ from, to }: { from: string; to: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border py-12 text-center">
      <p className="text-sm font-medium text-muted-foreground">
        No trading activity for {from} – {to}
      </p>
      <p className="text-xs text-muted-foreground/70">
        Use the navigator above to move to a different week.
      </p>
    </div>
  );
}
