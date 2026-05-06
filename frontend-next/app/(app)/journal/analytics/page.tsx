"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { fetchAnalyticsSummary } from "@/lib/analytics/api";
import { PnlBadge } from "@/components/shared/pnl-badge";
import { MetricValue } from "@/components/shared/metric-value";
import { KpiCard } from "@/components/operator/kpi-card";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import type { Period } from "@/components/shared/period-selector";
import type { AnalyticsStrategySummaryItem } from "@/lib/analytics/types";

function toNum(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

function fmtCcy(v: string | number | null | undefined): string {
  const n = toNum(v);
  if (n === null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_00_00_000) return `${n < 0 ? "-" : ""}₹${(abs / 1_00_00_000).toFixed(2)}Cr`;
  if (abs >= 1_00_000) return `${n < 0 ? "-" : ""}₹${(abs / 1_00_000).toFixed(2)}L`;
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtPct(v: string | number | null | undefined): string {
  const n = toNum(v);
  if (n === null) return "—";
  return `${n >= 0 ? "+" : ""}${Number(n).toFixed(1)}%`;
}

function fmtRatio(v: string | number | null | undefined): string {
  const n = toNum(v);
  if (n === null) return "—";
  return Number(n).toFixed(2);
}

function StrategyTable({
  strategies,
  envParam,
  modeParam,
  periodParam,
}: {
  strategies: AnalyticsStrategySummaryItem[];
  envParam: string;
  modeParam: string;
  periodParam: string;
}) {
  if (strategies.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No strategy data for this period.</p>
    );
  }

  function buildHref(templateId: string) {
    const sp = new URLSearchParams();
    if (envParam) sp.set("env", envParam);
    if (modeParam) sp.set("mode", modeParam);
    if (periodParam) sp.set("period", periodParam);
    const qs = sp.toString();
    return `/journal/analytics/strategies/${templateId}${qs ? `?${qs}` : ""}`;
  }

  return (
    <div className="overflow-hidden rounded-xl border border-border/60">
      <table className="w-full text-left text-sm">
        <thead className="bg-muted/30 text-[10px] uppercase tracking-[0.28em] text-muted-foreground">
          <tr>
            <th className="px-3 py-2 font-medium">Strategy</th>
            <th className="px-3 py-2 text-right font-medium">Episodes</th>
            <th className="px-3 py-2 text-right font-medium">Net P&amp;L</th>
            <th className="px-3 py-2 text-right font-medium">Win Rate</th>
            <th className="px-3 py-2 text-right font-medium">PF</th>
            <th className="px-3 py-2 text-right font-medium">Sharpe</th>
            <th className="px-3 py-2 text-right font-medium">Max DD</th>
          </tr>
        </thead>
        <tbody>
          {strategies.map((s) => (
            <tr
              key={s.strategy.template_id}
              className="border-t border-border/60 text-foreground/80 hover:bg-muted/20"
            >
              <td className="px-3 py-2.5">
                <Link
                  href={buildHref(s.strategy.template_id)}
                  className="group flex items-center gap-1.5"
                >
                  <span className="text-sm font-medium text-foreground/90 underline-offset-2 group-hover:underline">
                    {s.strategy.display_name ?? s.strategy.template_key ?? s.strategy.template_id}
                  </span>
                  <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                    {s.strategy.strategy_family?.replace(/_/g, " ")}
                  </span>
                </Link>
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums text-xs">
                <MetricValue value={s.metrics.closed_episode_count} />
              </td>
              <td className="px-3 py-2.5 text-right">
                <PnlBadge value={s.metrics.net_pnl} formatter={(n) => fmtCcy(n).replace("₹", "")} className="text-xs" />
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums text-xs">
                <MetricValue value={s.metrics.win_rate} formatter={(v) => `${Number(v).toFixed(1)}%`} />
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums text-xs">
                <MetricValue value={s.metrics.profit_factor} formatter={(v) => Number(v).toFixed(2)} />
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums text-xs">
                <MetricValue value={s.metrics.sharpe_ratio} formatter={(v) => Number(v).toFixed(2)} />
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums text-xs">
                <MetricValue
                  value={s.metrics.max_drawdown}
                  formatter={(v) => `${Number(v).toFixed(1)}%`}
                  className="text-[var(--red)]"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="border-t border-border/60 bg-background/40 px-3 py-2 text-[10px] text-muted-foreground">
        Strategy rows link to detailed analytics while preserving current period and workspace context.
      </div>
    </div>
  );
}

function StrategyCallout({
  label,
  item,
  tone,
}: {
  label: string;
  item: AnalyticsStrategySummaryItem | null;
  tone: "positive" | "negative";
}) {
  const title = item ? item.strategy.display_name ?? item.strategy.template_key ?? item.strategy.template_id : "—";
  const pnl = item ? item.metrics.net_pnl : null;
  return (
    <div className={tone === "positive" ? "rounded-xl border border-emerald-400/20 bg-emerald-400/5 p-4" : "rounded-xl border border-rose-400/20 bg-rose-400/5 p-4"}>
      <p className="text-[10px] uppercase tracking-[0.24em] text-muted-foreground">{label}</p>
      <div className="mt-2 flex items-end justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-foreground/90">{title}</p>
          {item ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {item.metrics.closed_episode_count} episodes · {item.metrics.win_rate !== null ? `${Number(item.metrics.win_rate).toFixed(1)}%` : "—"} win rate
            </p>
          ) : null}
        </div>
        <PnlBadge value={pnl} formatter={(n) => fmtCcy(n).replace("₹", "")} className="text-sm font-semibold" />
      </div>
    </div>
  );
}

export default function JournalAnalyticsDashboardPage() {
  const searchParams = useSearchParams();
  const { selectedEnvironmentId: workspaceEnvId, selectedMode } = useWorkspace();
  const envId = searchParams.get("env") || workspaceEnvId || "";
  const period = (searchParams.get("period") ?? "month") as Period;

  const { data, isLoading, error } = useQuery({
    queryKey: ["analytics-summary", envId, period],
    queryFn: () => fetchAnalyticsSummary({ environment_id: envId, period }),
    enabled: !!envId,
  });

  if (!envId) {
    return (
      <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view analytics.
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex flex-col gap-6">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="h-[100px] animate-pulse rounded-xl border border-border/70 bg-background/40"
            />
          ))}
        </div>
        <div className="h-[200px] animate-pulse rounded-xl border border-border/70 bg-background/40" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-xl border border-rose-400/30 bg-rose-400/5 px-4 py-3 text-sm text-rose-300">
        Failed to load analytics: {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  if (!data) return null;

  const m = data.metrics;
  const bestStrategy = [...data.strategies].sort((a, b) => Number(b.metrics.net_pnl ?? 0) - Number(a.metrics.net_pnl ?? 0))[0] ?? null;
  const worstStrategy = [...data.strategies].sort((a, b) => Number(a.metrics.net_pnl ?? 0) - Number(b.metrics.net_pnl ?? 0))[0] ?? null;
  const envParam = searchParams.get("env") ?? workspaceEnvId ?? "";
  const modeParam = searchParams.get("mode") ?? selectedMode;
  const periodParam = searchParams.get("period") ?? "month";

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          label="Net P&L"
          value={fmtCcy(m.net_pnl)}
          delta={fmtPct(m.cumulative_return)}
          note={`${m.closed_episode_count} closed episodes`}
        />
        <KpiCard
          label="Win Rate"
          value={m.win_rate !== null ? `${Number(m.win_rate).toFixed(1)}%` : "—"}
          delta={m.profit_factor !== null ? `PF ${fmtRatio(m.profit_factor)}` : undefined}
          note={m.expectancy !== null ? `Expectancy ${fmtCcy(m.expectancy)} · streak ${m.max_win_streak}W/${m.max_loss_streak}L` : `streak ${m.max_win_streak}W/${m.max_loss_streak}L`}
        />
        <KpiCard
          label="Sharpe Ratio"
          value={fmtRatio(m.sharpe_ratio)}
          delta={m.sortino_ratio !== null ? `Sortino ${fmtRatio(m.sortino_ratio)}` : undefined}
          note={m.r_multiple !== null ? `R-Multiple ${fmtRatio(m.r_multiple)}` : "insufficient data"}
        />
        <KpiCard
          label="Max Drawdown"
          value={m.max_drawdown !== null ? `${Number(m.max_drawdown).toFixed(1)}%` : "—"}
          note={
            m.max_drawdown_duration_days !== null
              ? `${m.max_drawdown_duration_days}d duration`
              : "no drawdown recorded"
          }
        />
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <StrategyCallout label="Best strategy" item={bestStrategy} tone="positive" />
        <StrategyCallout label="Worst strategy" item={worstStrategy} tone="negative" />
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <div className="rounded-xl border border-border/60 bg-background/40 p-4">
          <p className="mb-1 text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Gross P&L</p>
          <p className="text-lg font-semibold tabular-nums">{fmtCcy(m.gross_pnl)}</p>
        </div>
        <div className="rounded-xl border border-border/60 bg-background/40 p-4">
          <p className="mb-1 text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Total Charges</p>
          <p className="text-lg font-semibold tabular-nums text-[var(--red)]">{fmtCcy(m.total_charges)}</p>
          {m.cost_ratio !== null && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              Cost ratio {Number(m.cost_ratio).toFixed(1)}%
            </p>
          )}
        </div>
        <div className="rounded-xl border border-border/60 bg-background/40 p-4">
          <p className="mb-1 text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Avg Win / Loss</p>
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-semibold tabular-nums text-[var(--green)]">
              {fmtCcy(m.average_win)}
            </span>
            <span className="text-xs text-muted-foreground">/</span>
            <span className="text-sm font-semibold tabular-nums text-[var(--red)]">
              {fmtCcy(m.average_loss)}
            </span>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {m.win_count}W · {m.loss_count}L streak {m.max_win_streak}W/{m.max_loss_streak}L
          </p>
        </div>
      </div>

      <div>
        <h3 className="mb-3 text-sm font-medium text-foreground/70">
          Strategy Breakdown{" "}
          <span className="text-xs text-muted-foreground">
            ({data.strategies.length} {data.strategies.length === 1 ? "strategy" : "strategies"})
          </span>
        </h3>
        <StrategyTable
          strategies={data.strategies}
          envParam={envParam}
          modeParam={modeParam}
          periodParam={periodParam}
        />
      </div>

      <div className="rounded-xl border border-border/60 bg-background/40 px-3 py-2 text-[10px] text-muted-foreground">
        Analytics mode uses the period control in Journal header. Equity and Costs subviews share the same environment and period context.
      </div>
    </div>
  );
}
