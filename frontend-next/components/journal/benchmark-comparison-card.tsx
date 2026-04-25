import { Panel } from "@/components/operator/panel";
import type { BenchmarkComparison } from "@/lib/journal/types";

type BenchmarkComparisonCardProps = {
  data: BenchmarkComparison | null;
  loading: boolean;
  error: string | null;
};

function formatPercent(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
}

export function BenchmarkComparisonCard({ data, loading, error }: BenchmarkComparisonCardProps) {
  if (loading) {
    return (
      <Panel eyebrow="benchmark" title="Benchmark comparison">
        <div className="h-[120px] animate-pulse rounded-xl bg-background/40" />
      </Panel>
    );
  }

  if (error) {
    return (
      <Panel eyebrow="benchmark" title="Benchmark comparison">
        <p className="text-sm text-rose-300">Failed to load benchmark data.</p>
      </Panel>
    );
  }

  if (!data) {
    return (
      <Panel eyebrow="benchmark" title="Benchmark comparison">
        <p className="text-sm text-foreground/50">No benchmark data available for this period.</p>
      </Panel>
    );
  }

  const excess = data.excess_return;
  const excessTone = excess != null ? (excess >= 0 ? "text-emerald-400" : "text-rose-400") : "text-foreground/50";

  return (
    <Panel eyebrow="benchmark" title="Benchmark comparison">
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-2xl border border-border/60 bg-background/60 px-4 py-3">
          <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">portfolio</p>
          <p className="mt-2 font-mono text-lg text-primary">{formatPercent(data.portfolio_return)}</p>
        </div>
        <div className="rounded-2xl border border-border/60 bg-background/60 px-4 py-3">
          <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{data.benchmark_name}</p>
          <p className="mt-2 font-mono text-lg text-foreground/80">{formatPercent(data.benchmark_return)}</p>
        </div>
        <div className="rounded-2xl border border-border/60 bg-background/60 px-4 py-3">
          <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">excess</p>
          <p className={`mt-2 font-mono text-lg ${excessTone}`}>{formatPercent(excess)}</p>
        </div>
      </div>

      {data.portfolio_series.length > 0 && (
        <div className="mt-3 rounded-xl border border-border/60 bg-background/40 p-3">
          <div className="flex items-center gap-4 text-[10px] uppercase tracking-[0.2em] text-foreground/40">
            <span className="inline-flex items-center gap-1">
              <span className="inline-block h-2 w-2 rounded-full bg-primary" />
              portfolio
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="inline-block h-2 w-2 rounded-full bg-amber-400" />
              {data.benchmark_name}
            </span>
          </div>
          <div className="mt-2 flex h-16 items-end gap-px">
            {data.portfolio_series.slice(-30).map((pt, i) => {
              const max = Math.max(
                ...data.portfolio_series.slice(-30).map((p) => Math.abs(p.value)),
                ...data.benchmark_series.slice(-30).map((p) => Math.abs(p.value)),
                1,
              );
              const height = Math.max(4, (Math.abs(pt.value) / max) * 100);
              return (
                <div
                  key={i}
                  className="flex-1 rounded-t bg-primary/60"
                  style={{ height: `${height}%` }}
                  title={`${pt.date}: ${pt.value.toFixed(2)}`}
                />
              );
            })}
          </div>
        </div>
      )}
    </Panel>
  );
}
