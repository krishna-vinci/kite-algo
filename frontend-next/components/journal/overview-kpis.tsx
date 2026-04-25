import { KpiCard } from "@/components/operator/kpi-card";
import type { JournalSummary } from "@/lib/journal/types";

type OverviewKpisProps = {
  summary: JournalSummary | null;
  loading: boolean;
  error: string | null;
};

function formatCurrency(value: number | null | undefined): string {
  if (value == null) return "—";
  const abs = Math.abs(value);
  if (abs >= 100_000) return `${value < 0 ? "-" : ""}${(abs / 100_000).toFixed(1)}L`;
  return value.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function formatPercent(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function formatRatio(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toFixed(2);
}

export function OverviewKpis({ summary, loading, error }: OverviewKpisProps) {
  if (loading) {
    return (
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-[100px] animate-pulse rounded-[1.25rem] border border-border/70 bg-background/40" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-[1.25rem] border border-rose-400/30 bg-rose-400/5 p-4 text-sm text-rose-300">
        Failed to load summary: {error}
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="rounded-[1.25rem] border border-border/70 bg-background/40 p-4 text-sm text-foreground/50">
        No journal data available for this period.
      </div>
    );
  }

  const netDelta = summary.net_pnl >= 0 ? `+${formatCurrency(summary.net_pnl)}` : formatCurrency(summary.net_pnl);
  const excessDelta = summary.excess_return != null ? formatPercent(summary.excess_return) : undefined;

  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      <KpiCard
        label="Net P&L"
        value={`₹${formatCurrency(summary.net_pnl)}`}
        delta={netDelta}
        note={`${summary.closed_run_count} closed / ${summary.run_count} total runs`}
      />
      <KpiCard
        label="Win Rate"
        value={summary.win_rate != null ? `${(summary.win_rate * 100).toFixed(0)}%` : "—"}
        delta={summary.profit_factor != null ? `PF ${formatRatio(summary.profit_factor)}` : undefined}
        note={summary.expectancy != null ? `Expectancy ₹${formatCurrency(summary.expectancy)}` : "insufficient data"}
      />
      <KpiCard
        label="Excess Return"
        value={summary.excess_return != null ? formatPercent(summary.excess_return) : "—"}
        delta={excessDelta}
        note={summary.benchmark_return != null ? `Benchmark ${formatPercent(summary.benchmark_return)}` : "benchmark unavailable"}
      />
      <KpiCard
        label="Max Drawdown"
        value={summary.max_drawdown != null ? formatPercent(summary.max_drawdown) : "—"}
        note={summary.sharpe_ratio != null ? `Sharpe ${formatRatio(summary.sharpe_ratio)}` : "insufficient data for Sharpe"}
      />
    </div>
  );
}
