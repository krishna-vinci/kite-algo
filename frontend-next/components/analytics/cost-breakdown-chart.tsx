"use client";

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";

import type { StrategyCostAnalysisItem } from "@/lib/analytics/types";

type CostBreakdownChartProps = {
  strategies: StrategyCostAnalysisItem[];
  height?: number;
};

function toNum(v: string | number | null | undefined): number {
  const n = Number(v);
  return isNaN(n) ? 0 : n;
}

const COST_KEYS = [
  { key: "brokerage", label: "Brokerage", color: "hsl(221 83% 53%)" },
  { key: "exchange_txn_charge", label: "Exchange", color: "hsl(262 83% 58%)" },
  { key: "stt", label: "STT", color: "hsl(142 71% 45%)" },
  { key: "stamp_duty", label: "Stamp", color: "hsl(32 95% 44%)" },
  { key: "sebi_charge", label: "SEBI", color: "hsl(198 93% 60%)" },
  { key: "gst", label: "GST", color: "hsl(349 72% 51%)" },
] as const;

type CostKey = (typeof COST_KEYS)[number]["key"];

/**
 * CostBreakdownChart — stacked bar chart of trading cost components per strategy.
 * Uses Recharts with project-consistent colours.
 */
export function CostBreakdownChart({ strategies, height = 240 }: CostBreakdownChartProps) {
  if (strategies.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-xl border border-border/60 bg-muted/10 text-sm text-muted-foreground"
        style={{ height }}
      >
        No cost data for the selected period.
      </div>
    );
  }

  const chartData = strategies.map((s) => ({
    name: s.strategy.display_name ?? s.strategy.template_key ?? s.strategy.template_id,
    brokerage: toNum(s.cost_breakdown.brokerage),
    exchange_txn_charge: toNum(s.cost_breakdown.exchange_txn_charge),
    stt: toNum(s.cost_breakdown.stt),
    stamp_duty: toNum(s.cost_breakdown.stamp_duty),
    sebi_charge: toNum(s.cost_breakdown.sebi_charge),
    gst: toNum(s.cost_breakdown.gst),
    total: toNum(s.total_charges),
  }));

  const fmtCcy = (v: number) =>
    `₹${v.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;

  return (
    <div className="w-full">
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={chartData} margin={{ top: 4, right: 8, left: 8, bottom: 4 }}>
          <XAxis
            dataKey="name"
            tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`}
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            axisLine={false}
            tickLine={false}
            width={48}
          />
          <Tooltip
            formatter={(value, name) => [
              fmtCcy(Number(value ?? 0)),
              COST_KEYS.find((k) => k.key === String(name ?? ""))?.label ?? String(name ?? ""),
            ]}
            contentStyle={{
              background: "hsl(var(--background))",
              border: "1px solid hsl(var(--border))",
              borderRadius: "8px",
              fontSize: 11,
            }}
          />
          {COST_KEYS.map(({ key, color }) => (
            <Bar key={key} dataKey={key as CostKey} stackId="costs" fill={color} radius={key === "gst" ? [4, 4, 0, 0] : [0, 0, 0, 0]}>
              {chartData.map((_, idx) => (
                <Cell key={idx} fill={color} />
              ))}
            </Bar>
          ))}
        </BarChart>
      </ResponsiveContainer>
      <div className="mt-2 flex flex-wrap items-center gap-3 text-[10px] text-muted-foreground">
        {COST_KEYS.map(({ key, label, color }) => (
          <span key={key} className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: color }} />
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}
