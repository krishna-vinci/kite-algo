"use client";

import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { fetchEquityCurve } from "@/lib/analytics/api";
import { EquityCurveChart } from "@/components/analytics/equity-curve-chart";
import type { Period } from "@/components/shared/period-selector";

export default function EquityPage() {
  const searchParams = useSearchParams();
  const envId = searchParams.get("env") ?? "";
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

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="mb-1 text-sm font-medium text-foreground/70">Equity Curve</h3>
        <p className="text-xs text-muted-foreground">
          Cumulative net P&L with benchmark and excess return overlays.
        </p>
      </div>
      <div className="rounded-xl border border-border/60 bg-background/40 p-4">
        <EquityCurveChart points={points} height={320} />
      </div>
    </div>
  );
}
