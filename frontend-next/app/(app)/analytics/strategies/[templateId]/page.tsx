"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon } from "lucide-react";

import { fetchStrategyDeepDive } from "@/lib/analytics/api";
import { EquityCurveChart } from "@/components/analytics/equity-curve-chart";
import { MetricValue } from "@/components/shared/metric-value";
import { PnlBadge } from "@/components/shared/pnl-badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import type { Period } from "@/components/shared/period-selector";
import type { AnalyticsMetrics } from "@/lib/analytics/types";

// ---------------------------------------------------------------------------
// Formatters (local — same helpers as dashboard page)
// ---------------------------------------------------------------------------

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
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`;
}

function fmtRatio(v: string | number | null | undefined): string {
  const n = toNum(v);
  if (n === null) return "—";
  return n.toFixed(2);
}

function fmtDuration(seconds: number | null): string {
  if (seconds === null || seconds === 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MetricRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-right text-xs font-medium tabular-nums text-foreground/90">{value}</span>
    </div>
  );
}

function MetricSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-border/60 bg-background/40 p-4">
      <p className="mb-2 text-[10px] uppercase tracking-[0.28em] text-muted-foreground">{title}</p>
      <div className="divide-y divide-border/40">{children}</div>
    </div>
  );
}

function PerformanceMetrics({ m }: { m: AnalyticsMetrics }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {/* P&L */}
      <MetricSection title="P&amp;L">
        <MetricRow label="Gross P&L" value={fmtCcy(m.gross_pnl)} />
        <MetricRow label="Net P&L" value={<PnlBadge value={m.net_pnl} formatter={(n) => fmtCcy(n).replace("₹", "")} className="text-xs" />} />
        <MetricRow label="Total Charges" value={<span className="text-[var(--red)]">{fmtCcy(m.total_charges)}</span>} />
        <MetricRow label="Cost Ratio" value={m.cost_ratio !== null ? `${Number(m.cost_ratio).toFixed(1)}%` : "—"} />
      </MetricSection>

      {/* Win/Loss */}
      <MetricSection title="Win / Loss">
        <MetricRow label="Win Rate" value={m.win_rate !== null ? `${Number(m.win_rate).toFixed(1)}%` : "—"} />
        <MetricRow label="Wins / Losses" value={`${m.win_count}W · ${m.loss_count}L`} />
        <MetricRow label="Avg Win" value={<span className="text-[var(--green)]">{fmtCcy(m.average_win)}</span>} />
        <MetricRow label="Avg Loss" value={<span className="text-[var(--red)]">{fmtCcy(m.average_loss)}</span>} />
        <MetricRow label="Profit Factor" value={fmtRatio(m.profit_factor)} />
        <MetricRow label="Expectancy" value={fmtCcy(m.expectancy)} />
      </MetricSection>

      {/* Risk */}
      <MetricSection title="Risk">
        <MetricRow label="Sharpe Ratio" value={fmtRatio(m.sharpe_ratio)} />
        <MetricRow label="Sortino Ratio" value={fmtRatio(m.sortino_ratio)} />
        <MetricRow label="Max Drawdown" value={<span className="text-[var(--red)]">{m.max_drawdown !== null ? `${Number(m.max_drawdown).toFixed(1)}%` : "—"}</span>} />
        <MetricRow label="DD Duration" value={m.max_drawdown_duration_days !== null ? `${m.max_drawdown_duration_days}d` : "—"} />
        <MetricRow label="R-Multiple" value={fmtRatio(m.r_multiple)} />
        <MetricRow label="Cumulative Return" value={fmtPct(m.cumulative_return)} />
      </MetricSection>

      {/* Trade stats */}
      <MetricSection title="Trade Stats">
        <MetricRow label="Closed Episodes" value={<MetricValue value={m.closed_episode_count} />} />
        <MetricRow label="Avg Hold Time" value={fmtDuration(m.hold_seconds_avg)} />
        <MetricRow label="Max Win Streak" value={m.max_win_streak} />
        <MetricRow label="Max Loss Streak" value={m.max_loss_streak} />
        <MetricRow label="MAE" value={fmtCcy(m.mae)} />
        <MetricRow label="MFE" value={fmtCcy(m.mfe)} />
      </MetricSection>

      {/* Charges */}
      <MetricSection title="Charges Breakdown">
        <MetricRow label="Brokerage" value={fmtCcy(m.cost_breakdown.brokerage)} />
        <MetricRow label="Exchange TXN" value={fmtCcy(m.cost_breakdown.exchange_txn_charge)} />
        <MetricRow label="STT" value={fmtCcy(m.cost_breakdown.stt)} />
        <MetricRow label="Stamp Duty" value={fmtCcy(m.cost_breakdown.stamp_duty)} />
        <MetricRow label="SEBI" value={fmtCcy(m.cost_breakdown.sebi_charge)} />
        <MetricRow label="GST" value={fmtCcy(m.cost_breakdown.gst)} />
      </MetricSection>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function StrategyDeepDivePage() {
  const params = useParams<{ templateId: string }>();
  const searchParams = useSearchParams();

  const templateId = params.templateId;
  const envId = searchParams.get("env") ?? "";
  const period = (searchParams.get("period") ?? "month") as Period;

  // Preserve params for the back-link
  const backSp = new URLSearchParams();
  const envParam = searchParams.get("env");
  const modeParam = searchParams.get("mode");
  const periodParam = searchParams.get("period");
  if (envParam) backSp.set("env", envParam);
  if (modeParam) backSp.set("mode", modeParam);
  if (periodParam) backSp.set("period", periodParam);
  const backHref = `/analytics${backSp.toString() ? `?${backSp.toString()}` : ""}`;

  const { data, isLoading, error } = useQuery({
    queryKey: ["analytics-strategy", templateId, envId, period],
    queryFn: () => fetchStrategyDeepDive({ environment_id: envId, template_id: templateId, period }),
    enabled: !!envId && !!templateId,
  });

  // ── No env selected ───────────────────────────────────────────────────────
  if (!envId) {
    return (
      <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view strategy analytics.
      </div>
    );
  }

  // ── Loading ───────────────────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="flex flex-col gap-4">
        <div className="h-8 w-48 animate-pulse rounded-lg bg-muted/40" />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-[200px] animate-pulse rounded-xl border border-border/70 bg-background/40" />
          ))}
        </div>
        <div className="h-[280px] animate-pulse rounded-xl border border-border/70 bg-background/40" />
      </div>
    );
  }

  // ── Error ─────────────────────────────────────────────────────────────────
  if (error) {
    return (
      <div className="flex flex-col gap-4">
        <Button variant="ghost" size="sm" className="w-fit gap-1.5 px-2 text-xs text-muted-foreground" asChild>
          <Link href={backHref}>
            <ArrowLeftIcon data-icon="inline-start" />
            Analytics
          </Link>
        </Button>
        <div className="rounded-xl border border-rose-400/30 bg-rose-400/5 px-4 py-3 text-sm text-rose-300">
          Failed to load strategy data: {error instanceof Error ? error.message : "Unknown error"}
        </div>
      </div>
    );
  }

  // ── No data ───────────────────────────────────────────────────────────────
  if (!data) return null;

  const strategyName =
    data.strategy.display_name ??
    data.strategy.template_key ??
    data.strategy.template_id;

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              className="h-6 gap-1 px-1.5 text-xs text-muted-foreground"
              asChild
            >
              <Link href={backHref}>
                <ArrowLeftIcon data-icon="inline-start" />
                Analytics
              </Link>
            </Button>
          </div>
          <h3 className="text-base font-semibold tracking-tight">{strategyName}</h3>
          <p className="text-xs text-muted-foreground">
            {data.strategy.strategy_family?.replace(/_/g, " ")}{" "}
            {data.strategy.template_key && (
              <span className="ml-1 font-mono text-[10px] text-muted-foreground/60">
                {data.strategy.template_key}
              </span>
            )}
            <span className="mx-1 text-border">·</span>
            {data.period} period
            {data.anchor_date && (
              <span className="ml-1 text-muted-foreground/60">as of {data.anchor_date}</span>
            )}
          </p>
        </div>

        {/* Quick KPIs */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-1.5 text-center">
            <p className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Net P&L</p>
            <PnlBadge
              value={data.metrics.net_pnl}
              formatter={(n) => fmtCcy(n).replace("₹", "")}
              className="mt-0.5 text-sm font-semibold"
            />
          </div>
          <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-1.5 text-center">
            <p className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Win Rate</p>
            <p className="mt-0.5 text-sm font-semibold tabular-nums">
              {data.metrics.win_rate !== null ? `${Number(data.metrics.win_rate).toFixed(1)}%` : "—"}
            </p>
          </div>
          <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-1.5 text-center">
            <p className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Sharpe</p>
            <p className="mt-0.5 text-sm font-semibold tabular-nums">{fmtRatio(data.metrics.sharpe_ratio)}</p>
          </div>
          <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-1.5 text-center">
            <p className="text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Max DD</p>
            <p className="mt-0.5 text-sm font-semibold tabular-nums text-[var(--red)]">
              {data.metrics.max_drawdown !== null ? `${Number(data.metrics.max_drawdown).toFixed(1)}%` : "—"}
            </p>
          </div>
        </div>
      </div>

      <Separator />

      {/* Equity curve */}
      {data.equity_curve.length > 0 && (
        <div>
          <p className="mb-2 text-xs font-medium text-foreground/60">Equity Curve</p>
          <div className="rounded-xl border border-border/60 bg-background/40 p-4">
            <EquityCurveChart points={data.equity_curve} height={240} />
          </div>
        </div>
      )}

      {/* Detailed metrics grid */}
      <div>
        <p className="mb-3 text-xs font-medium text-foreground/60">Performance Breakdown</p>
        <PerformanceMetrics m={data.metrics} />
      </div>

      {/* Journal back-link */}
      <div className="pt-1">
        <Button
          variant="outline"
          size="sm"
          className="text-xs text-muted-foreground"
          asChild
        >
          <Link
            href={`/journal${backSp.toString() ? `?${backSp.toString()}` : ""}`}
          >
            View in Journal →
          </Link>
        </Button>
      </div>
    </div>
  );
}
