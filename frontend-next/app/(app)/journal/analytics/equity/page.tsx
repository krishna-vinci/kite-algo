"use client";

import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { fetchEquityCurve } from "@/lib/analytics/api";
import { EquityCurveChart } from "@/components/analytics/equity-curve-chart";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import type { Period } from "@/components/shared/period-selector";

function fmtCcy(v: string | number | null | undefined): string {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || Number.isNaN(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtPct(v: string | number | null | undefined): string {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || Number.isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`;
}

function fmtRatio(v: string | number | null | undefined): string {
  const n = Number(v);
  if (v === null || v === undefined || v === "" || Number.isNaN(n)) return "—";
  return n.toFixed(2);
}

export default function JournalAnalyticsEquityPage() {
  const searchParams = useSearchParams();
  const { selectedEnvironmentId: workspaceEnvId } = useWorkspace();
  const envId = searchParams.get("env") || workspaceEnvId || "";
  const period = (searchParams.get("period") ?? "month") as Period;

  const { data, isLoading, error } = useQuery({
    queryKey: ["analytics-equity", envId, period],
    queryFn: () => fetchEquityCurve({ environment_id: envId, period }),
    enabled: !!envId,
  });

  if (!envId) {
    return (
      <div className="rounded-xl border border-border/60 bg-muted/20 px-4 py-8 text-center text-sm text-muted-foreground">
        Select an environment to view the equity curve.
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="h-[320px] animate-pulse rounded-xl border border-border/70 bg-background/40" />
    );
  }

  if (error) {
    return (
      <div className="rounded-xl border border-rose-400/30 bg-rose-400/5 px-4 py-3 text-sm text-rose-300">
        Failed to load equity curve: {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const points = data?.points ?? [];
  const metrics = data?.metrics;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="mb-1 text-sm font-medium text-foreground/70">Equity Curve</h3>
        <p className="text-xs text-muted-foreground">
          Cumulative net P&L with benchmark and excess return overlays.
        </p>
      </div>
      {metrics ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <StatTile label="Cumulative Return" value={fmtPct(metrics.cumulative_return)} />
          <StatTile label="Max Drawdown" value={metrics.max_drawdown !== null ? `${Number(metrics.max_drawdown).toFixed(1)}%` : "—"} tone="negative" />
          <StatTile label="Sharpe Ratio" value={fmtRatio(metrics.sharpe_ratio)} />
          <StatTile label="Total Charges" value={fmtCcy(metrics.total_charges)} tone="negative" />
        </div>
      ) : null}
      <div className="rounded-xl border border-border/60 bg-background/40 p-4">
        <EquityCurveChart points={points} height={320} />
      </div>
    </div>
  );
}

function StatTile({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "negative" }) {
  return (
    <div className="rounded-xl border border-border/60 bg-background/40 p-4">
      <p className="text-[10px] uppercase tracking-[0.24em] text-muted-foreground">{label}</p>
      <p className={tone === "negative" ? "mt-2 font-mono text-lg font-semibold text-[var(--red)]" : "mt-2 font-mono text-lg font-semibold text-foreground"}>{value}</p>
    </div>
  );
}
