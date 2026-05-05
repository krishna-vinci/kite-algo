"use client";

import { TableCell, TableRow } from "@/components/ui/table";
import type { JournalV2StrategyScorecard } from "@/lib/journal/types";

type StrategyMetricRowProps = {
  item: JournalV2StrategyScorecard;
  envParam?: string;
};

function formatINR(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(n);
}

function formatRate(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value ?? null);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(1)}%`;
}

function formatNum(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value ?? null);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function pnlClass(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(n)) return "";
  if (n > 0) return "text-emerald-400 tabular-nums";
  if (n < 0) return "text-rose-400 tabular-nums";
  return "tabular-nums";
}

export function StrategyMetricRow({ item, envParam }: StrategyMetricRowProps) {
  const m = item.metrics;
  const netPnl = typeof m.net_pnl === "number" ? m.net_pnl : Number(m.net_pnl ?? 0);
  const episodeCount =
    typeof m.closed_episode_count === "number" ? m.closed_episode_count : 0;

  // Analytics link — placeholder if analytics route doesn't exist yet
  const analyticsHref = envParam
    ? `/journal/analytics?environment_id=${encodeURIComponent(envParam)}&template_id=${encodeURIComponent(item.template_id)}`
    : `/journal/analytics?template_id=${encodeURIComponent(item.template_id)}`;

  return (
    <TableRow className="group cursor-pointer hover:bg-muted/30">
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-medium text-foreground">
            {item.display_name || item.template_id}
          </span>
          <span className="text-[10px] uppercase tracking-[0.15em] text-foreground/40">
            {item.strategy_family}
          </span>
        </div>
      </TableCell>
      <TableCell className="text-right tabular-nums text-sm text-foreground/80">
        {episodeCount}
      </TableCell>
      <TableCell className={`text-right text-sm ${pnlClass(netPnl)}`}>
        {formatINR(m.net_pnl)}
      </TableCell>
      <TableCell className="text-right text-sm text-foreground/80 tabular-nums">
        {formatRate(m.win_rate)}
      </TableCell>
      <TableCell className="text-right text-sm text-foreground/70 tabular-nums">
        {formatINR(m.total_charges)}
      </TableCell>
      <TableCell className="text-right text-sm text-foreground/70 tabular-nums">
        {formatNum(m.average_win)}
      </TableCell>
      <TableCell className="text-right text-sm text-foreground/70 tabular-nums">
        {formatNum(m.average_loss)}
      </TableCell>
      <TableCell className="text-right text-sm text-foreground/70 tabular-nums">
        {formatNum(m.expectancy)}
      </TableCell>
      <TableCell className="text-right">
        <a
          href={analyticsHref}
          className="text-[10px] uppercase tracking-[0.15em] text-foreground/40 opacity-0 transition-opacity group-hover:opacity-100 hover:text-foreground/70"
          tabIndex={-1}
        >
          Analytics →
        </a>
      </TableCell>
    </TableRow>
  );
}
