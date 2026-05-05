"use client";

import { Badge } from "@/components/ui/badge";
import { MetricValue } from "@/components/shared/metric-value";
import { PnlBadge } from "@/components/shared/pnl-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { JournalKpiCard } from "@/components/journal/journal-kpi-card";
import type { AnalyticsMetrics } from "@/lib/journal/types-v2";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

function fmtRatio(v: string | number | null | undefined): string | null {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || isNaN(n)) return null;
  return n.toFixed(2);
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export function PeriodKpiSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div
      className={cn(
        "grid gap-3",
        count <= 4
          ? "grid-cols-2 sm:grid-cols-4"
          : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-5",
      )}
    >
      {Array.from({ length: count }).map((_, i) => (
        <Skeleton key={i} className="h-20 rounded-xl" />
      ))}
    </div>
  );
}

/**
 * PeriodKpiGrid — dense KPI summary strip for week/month period views.
 * Reuses the same card pattern as the day view's SummaryKpis.
 */
export function PeriodKpiGrid({ metrics }: { metrics: AnalyticsMetrics }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      <JournalKpiCard label="Net P&L">
        <PnlBadge value={metrics.net_pnl} className="text-base font-semibold" />
      </JournalKpiCard>

      <JournalKpiCard label="Gross P&L">
        <PnlBadge value={metrics.gross_pnl} className="text-base font-semibold" />
      </JournalKpiCard>

      <JournalKpiCard label="Total Charges">
        <span className="text-base font-semibold tabular-nums text-[var(--red)]">
          <MetricValue value={fmtNum(metrics.total_charges)} />
        </span>
      </JournalKpiCard>

      <JournalKpiCard label="Episodes">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-base font-semibold tabular-nums">
            {metrics.closed_episode_count}
          </span>
          <span className="text-xs text-muted-foreground">closed</span>
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

/**
 * Extended KPI grid with extra stats for the month view.
 */
export function PeriodKpiGridExtended({ metrics }: { metrics: AnalyticsMetrics }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <JournalKpiCard label="Net P&L">
        <PnlBadge value={metrics.net_pnl} className="text-base font-semibold" />
      </JournalKpiCard>

      <JournalKpiCard label="Gross P&L">
        <PnlBadge value={metrics.gross_pnl} className="text-base font-semibold" />
      </JournalKpiCard>

      <JournalKpiCard label="Total Charges">
        <span className="text-base font-semibold tabular-nums text-[var(--red)]">
          <MetricValue value={fmtNum(metrics.total_charges)} />
        </span>
      </JournalKpiCard>

      <JournalKpiCard label="Episodes">
        <span className="text-base font-semibold tabular-nums">
          {metrics.closed_episode_count}
        </span>
      </JournalKpiCard>

      <JournalKpiCard label="Win Rate">
        <span className="text-base font-semibold tabular-nums">
          <MetricValue value={fmtPct(metrics.win_rate)} />
        </span>
      </JournalKpiCard>

      <JournalKpiCard label="Profit Factor">
        <span className="text-base font-semibold tabular-nums">
          <MetricValue value={fmtRatio(metrics.profit_factor)} />
        </span>
      </JournalKpiCard>
    </div>
  );
}

/**
 * Strategy summary table for period views.
 */
export function StrategySummaryTable({
  strategies,
}: {
  strategies: Array<{
    strategy: { template_id: string; template_key: string | null; display_name: string | null };
    metrics: AnalyticsMetrics;
    episode_count: number;
  }>;
}) {
  if (strategies.length === 0) {
    return (
      <p className="py-4 text-center text-xs text-muted-foreground">
        No strategy data for this period.
      </p>
    );
  }

  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border">
            <th className="pb-2 pr-4 text-left font-medium text-muted-foreground">Strategy</th>
            <th className="pb-2 pr-4 text-right font-medium text-muted-foreground">Episodes</th>
            <th className="pb-2 pr-4 text-right font-medium text-muted-foreground">Win Rate</th>
            <th className="pb-2 pr-4 text-right font-medium text-muted-foreground">Net P&L</th>
            <th className="pb-2 text-right font-medium text-muted-foreground">Charges</th>
          </tr>
        </thead>
        <tbody>
          {strategies.map((item) => {
            const name =
              item.strategy.display_name ??
              item.strategy.template_key ??
              item.strategy.template_id;
            return (
              <tr
                key={item.strategy.template_id}
                className="border-b border-border/40 last:border-0"
              >
                <td className="py-1.5 pr-4 font-medium text-foreground">
                  <span className="truncate">{name}</span>
                </td>
                <td className="py-1.5 pr-4 text-right tabular-nums text-muted-foreground">
                  {item.episode_count}
                </td>
                <td className="py-1.5 pr-4 text-right tabular-nums">
                  <MetricValue value={fmtPct(item.metrics.win_rate)} />
                </td>
                <td className="py-1.5 pr-4 text-right">
                  <PnlBadge value={item.metrics.net_pnl} />
                </td>
                <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                  <MetricValue value={fmtNum(item.metrics.total_charges)} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
