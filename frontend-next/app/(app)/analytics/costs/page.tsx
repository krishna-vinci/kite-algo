"use client";

import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { fetchCostAnalysis } from "@/lib/analytics/api";
import { CostBreakdownChart } from "@/components/analytics/cost-breakdown-chart";
import { CostBreakdownTable } from "@/components/shared/cost-breakdown-table";
import type { Period } from "@/components/shared/period-selector";

function toNum(v: string | number | null | undefined): number | null {
  const n = Number(v);
  return isNaN(n) ? null : n;
}

export default function CostsPage() {
  const searchParams = useSearchParams();
  const envId = searchParams.get("env") ?? "";
  const period = (searchParams.get("period") ?? "month") as Period;

  const { data, isLoading, error } = useQuery({
    queryKey: ["analytics-costs", envId, period],
    queryFn: () => fetchCostAnalysis({ environment_id: envId, period }),
    enabled: !!envId,
  });

  if (!envId) {
    return (
      <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view cost analysis.
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4">
        <div className="h-[240px] animate-pulse rounded-xl border border-border/70 bg-background/40" />
        <div className="h-[200px] animate-pulse rounded-xl border border-border/70 bg-background/40" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-xl border border-rose-400/30 bg-rose-400/5 px-4 py-3 text-sm text-rose-300">
        Failed to load cost analysis: {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  if (!data) return null;

  const cb = data.cost_breakdown;

  return (
    <div className="flex flex-col gap-6">
      {/* Chart */}
      <div>
        <h3 className="mb-1 text-sm font-medium text-foreground/70">Cost Breakdown by Strategy</h3>
        <p className="mb-3 text-xs text-muted-foreground">
          Stacked trading costs per strategy for the selected period.
        </p>
        <div className="rounded-xl border border-border/60 bg-background/40 p-4">
          <CostBreakdownChart strategies={data.strategies} />
        </div>
      </div>

      {/* Totals */}
      <div className="grid gap-4 md:grid-cols-2">
        {/* Environment total breakdown */}
        <div className="rounded-xl border border-border/60 bg-background/40 p-4">
          <p className="mb-3 text-xs font-medium uppercase tracking-[0.28em] text-muted-foreground">
            Environment Total
          </p>
          <CostBreakdownTable
            values={{
              brokerage: toNum(cb.brokerage),
              exchange_txn_charge: toNum(cb.exchange_txn_charge),
              stt: toNum(cb.stt),
              stamp_duty: toNum(cb.stamp_duty),
              sebi_charge: toNum(cb.sebi_charge),
              gst: toNum(cb.gst),
              total_taxes: toNum(cb.total_taxes),
              total_charges: toNum(cb.total_charges),
            }}
          />
        </div>

        {/* Strategy table with cost ratio */}
        <div className="rounded-xl border border-border/60 bg-background/40 p-4">
          <p className="mb-3 text-xs font-medium uppercase tracking-[0.28em] text-muted-foreground">
            Per Strategy
          </p>
          {data.strategies.length === 0 ? (
            <p className="text-sm text-muted-foreground">No strategy data.</p>
          ) : (
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                  <th className="pb-2 font-medium">Strategy</th>
                  <th className="pb-2 text-right font-medium">Episodes</th>
                  <th className="pb-2 text-right font-medium">Total</th>
                  <th className="pb-2 text-right font-medium">Ratio</th>
                </tr>
              </thead>
              <tbody>
                {data.strategies.map((s) => (
                  <tr key={s.strategy.template_id} className="border-t border-border/40">
                    <td className="py-1.5 pr-2 font-medium text-foreground/80">
                      {s.strategy.display_name ?? s.strategy.template_key ?? s.strategy.template_id}
                    </td>
                    <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                      {s.closed_episode_count}
                    </td>
                    <td className="py-1.5 text-right tabular-nums text-[var(--red)]">
                      ₹{Number(toNum(s.total_charges) ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                    <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                      {s.cost_ratio !== null ? `${Number(s.cost_ratio).toFixed(1)}%` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
