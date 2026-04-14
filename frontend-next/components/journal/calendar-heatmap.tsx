"use client";

import { Panel } from "@/components/operator/panel";
import type { CalendarDay } from "@/lib/journal/types";
import { cn } from "@/lib/utils";

type CalendarHeatmapProps = {
  days: CalendarDay[];
  loading: boolean;
  error: string | null;
  month?: number;
  year?: number;
};

function pnlIntensity(pnl: number): string {
  if (pnl > 0) {
    if (pnl > 10000) return "bg-emerald-500/60";
    if (pnl > 5000) return "bg-emerald-500/40";
    return "bg-emerald-500/20";
  }
  if (pnl < 0) {
    if (pnl < -10000) return "bg-rose-500/60";
    if (pnl < -5000) return "bg-rose-500/40";
    return "bg-rose-500/20";
  }
  return "bg-foreground/5";
}

function getDaysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

function getFirstDayOfWeek(year: number, month: number): number {
  return new Date(year, month - 1, 1).getDay();
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function CalendarHeatmap({ days, loading, error, month, year }: CalendarHeatmapProps) {
  const now = new Date();
  const displayYear = year ?? now.getFullYear();
  const displayMonth = month ?? now.getMonth() + 1;

  const daysInMonth = getDaysInMonth(displayYear, displayMonth);
  const firstDayOffset = getFirstDayOfWeek(displayYear, displayMonth);

  const dayMap = new Map(days.map((d) => [d.date, d]));

  const monthLabel = new Intl.DateTimeFormat("en-IN", { month: "long", year: "numeric" }).format(
    new Date(displayYear, displayMonth - 1, 1),
  );

  if (loading) {
    return (
      <Panel eyebrow="calendar" title={monthLabel}>
        <div className="h-[200px] animate-pulse rounded-xl bg-background/40" />
      </Panel>
    );
  }

  if (error) {
    return (
      <Panel eyebrow="calendar" title={monthLabel}>
        <p className="text-sm text-rose-300">Failed to load calendar data.</p>
      </Panel>
    );
  }

  const cells: Array<{ day: number | null; data: CalendarDay | null }> = [];

  for (let i = 0; i < firstDayOffset; i++) {
    cells.push({ day: null, data: null });
  }

  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${displayYear}-${String(displayMonth).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    cells.push({ day: d, data: dayMap.get(dateStr) ?? null });
  }

  return (
    <Panel eyebrow="calendar" title={monthLabel}>
      <div className="grid grid-cols-7 gap-1">
        {WEEKDAYS.map((wd) => (
          <div key={wd} className="pb-1 text-center text-[9px] uppercase tracking-[0.2em] text-foreground/30">
            {wd}
          </div>
        ))}
        {cells.map((cell, i) => (
          <div
            key={i}
            className={cn(
              "flex h-10 flex-col items-center justify-center rounded-lg text-[11px] transition-colors",
              cell.day == null
                ? "bg-transparent"
                : cell.data
                  ? pnlIntensity(cell.data.net_pnl)
                  : "bg-foreground/[0.03]",
            )}
            title={
              cell.data
                ? `${cell.data.date}: ₹${cell.data.net_pnl.toLocaleString("en-IN")} (${cell.data.run_count} runs)`
                : undefined
            }
          >
            {cell.day != null && (
              <>
                <span className="text-foreground/60">{cell.day}</span>
                {cell.data && (
                  <span className={`text-[8px] font-mono ${cell.data.net_pnl >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                    {cell.data.run_count}
                  </span>
                )}
              </>
            )}
          </div>
        ))}
      </div>

      <div className="mt-3 flex items-center gap-3 text-[10px] text-foreground/40">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded bg-emerald-500/40" /> profit
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded bg-rose-500/40" /> loss
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded bg-foreground/[0.03]" /> no trades
        </span>
      </div>
    </Panel>
  );
}
