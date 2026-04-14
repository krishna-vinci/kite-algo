import { Panel } from "@/components/operator/panel";
import type { StrategyPerformance } from "@/lib/journal/types";

type StrategyPerformanceTableProps = {
  strategies: StrategyPerformance[];
  loading: boolean;
  error: string | null;
};

function formatCurrency(value: number | null): string {
  if (value == null) return "—";
  const prefix = value >= 0 ? "+" : "";
  return `${prefix}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function StrategyPerformanceTable({ strategies, loading, error }: StrategyPerformanceTableProps) {
  if (loading) {
    return (
      <Panel eyebrow="strategies" title="Strategy performance">
        <div className="h-[200px] animate-pulse rounded-xl bg-background/40" />
      </Panel>
    );
  }

  if (error) {
    return (
      <Panel eyebrow="strategies" title="Strategy performance">
        <p className="text-sm text-rose-300">Failed to load strategy data.</p>
      </Panel>
    );
  }

  if (strategies.length === 0) {
    return (
      <Panel eyebrow="strategies" title="Strategy performance">
        <p className="text-sm text-foreground/50">No strategies recorded yet.</p>
      </Panel>
    );
  }

  return (
    <Panel eyebrow="strategies" title="Strategy performance">
      <div className="overflow-hidden rounded-2xl border border-border/60">
        <table className="w-full text-left text-sm">
          <thead className="bg-muted/30 text-[10px] uppercase tracking-[0.28em] text-foreground/40">
            <tr>
              <th className="px-3 py-2 font-medium">Strategy</th>
              <th className="px-3 py-2 font-medium">Family</th>
              <th className="px-3 py-2 font-medium">Runs</th>
              <th className="px-3 py-2 font-medium">Net P&L</th>
              <th className="px-3 py-2 font-medium">Win Rate</th>
              <th className="px-3 py-2 font-medium">PF</th>
              <th className="px-3 py-2 font-medium">Best</th>
              <th className="px-3 py-2 font-medium">Worst</th>
            </tr>
          </thead>
          <tbody>
            {strategies.map((s) => (
              <tr key={`${s.strategy_family}-${s.strategy_name}`} className="border-t border-border/60 text-foreground/80">
                <td className="px-3 py-3 text-sm font-medium text-foreground/90">{s.strategy_name}</td>
                <td className="px-3 py-3 text-[10px] uppercase tracking-[0.2em] text-foreground/40">
                  {s.strategy_family.replace(/_/g, " ")}
                </td>
                <td className="px-3 py-3 font-mono text-sm">
                  {s.closed_count}/{s.run_count}
                </td>
                <td className={`px-3 py-3 font-mono text-sm ${s.net_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {formatCurrency(s.net_pnl)}
                </td>
                <td className="px-3 py-3 font-mono text-sm">
                  {s.win_rate != null ? `${(s.win_rate * 100).toFixed(0)}%` : "—"}
                </td>
                <td className="px-3 py-3 font-mono text-sm">
                  {s.profit_factor != null ? s.profit_factor.toFixed(2) : "—"}
                </td>
                <td className="px-3 py-3 font-mono text-sm text-emerald-400/70">
                  {formatCurrency(s.best_run_pnl)}
                </td>
                <td className="px-3 py-3 font-mono text-sm text-rose-400/70">
                  {formatCurrency(s.worst_run_pnl)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
