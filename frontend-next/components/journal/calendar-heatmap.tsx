"use client";

import { Panel } from "@/components/operator/panel";
import Link from "next/link";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { CalendarDay } from "@/lib/journal/types";
import { cn } from "@/lib/utils";

type CalendarHeatmapProps = {
  days: CalendarDay[];
  loading: boolean;
  error: string | null;
  month?: number;
  year?: number;
  env?: string;
  mode?: string;
};

function pnlIntensity(pnl: number): string {
  if (pnl > 0) {
    if (pnl > 10000) return "bg-[var(--green)]/40";
    if (pnl > 5000) return "bg-[var(--green)]/25";
    return "bg-[var(--green)]/15";
  }
  if (pnl < 0) {
    if (pnl < -10000) return "bg-[var(--red)]/40";
    if (pnl < -5000) return "bg-[var(--red)]/25";
    return "bg-[var(--red)]/15";
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

function prevMonth(year: number, month: number): { year: number; month: number } {
  if (month === 1) return { year: year - 1, month: 12 };
  return { year, month: month - 1 };
}

function nextMonth(year: number, month: number): { year: number; month: number } {
  if (month === 12) return { year: year + 1, month: 1 };
  return { year, month: month + 1 };
}

function buildDayHref({
  date,
  env,
  mode,
}: {
  date: string;
  env?: string;
  mode?: string;
}): string {
  const sp = new URLSearchParams();
  if (env) sp.set("env", env);
  if (mode) sp.set("mode", mode);
  sp.set("date", date);
  const qs = sp.toString();
  return qs ? `/journal?${qs}` : "/journal";
}

function buildMonthHref({
  year,
  month,
  env,
  mode,
}: {
  year: number;
  month: number;
  env?: string;
  mode?: string;
}): string {
  const sp = new URLSearchParams();
  if (env) sp.set("env", env);
  if (mode) sp.set("mode", mode);
  sp.set("date", `${year}-${String(month).padStart(2, "0")}-01`);
  const qs = sp.toString();
  return qs ? `/journal/month?${qs}` : "/journal/month";
}

export function CalendarHeatmap({ days, loading, error, month, year, env, mode }: CalendarHeatmapProps) {
  const now = new Date();
  const displayYear = year ?? now.getFullYear();
  const displayMonth = month ?? now.getMonth() + 1;

  const daysInMonth = getDaysInMonth(displayYear, displayMonth);
  const firstDayOffset = getFirstDayOfWeek(displayYear, displayMonth);

  const dayMap = new Map(days.map((d) => [d.date, d]));

  const monthLabel = new Intl.DateTimeFormat("en-IN", { month: "long", year: "numeric" }).format(
    new Date(displayYear, displayMonth - 1, 1),
  );
  const prev = prevMonth(displayYear, displayMonth);
  const next = nextMonth(displayYear, displayMonth);

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
    <Panel
      eyebrow="calendar"
      title={monthLabel}
      action={
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon" className="h-7 w-7" asChild>
            <Link
              href={buildMonthHref({ year: prev.year, month: prev.month, env, mode })}
              aria-label="Previous month"
            >
              <ChevronLeftIcon data-icon="inline-start" />
            </Link>
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" asChild>
            <Link
              href={buildMonthHref({ year: next.year, month: next.month, env, mode })}
              aria-label="Next month"
            >
              <ChevronRightIcon data-icon="inline-end" />
            </Link>
          </Button>
        </div>
      }
    >
      <div className="grid grid-cols-7 gap-1">
        {WEEKDAYS.map((wd) => (
          <div key={wd} className="pb-1 text-center text-[9px] uppercase tracking-[0.2em] text-foreground/30">
            {wd}
          </div>
        ))}
        {cells.map((cell, i) => {
          if (cell.day == null) {
            return <div key={i} className="h-10 rounded-lg bg-transparent" aria-hidden="true" />;
          }

          const dateStr = `${displayYear}-${String(displayMonth).padStart(2, "0")}-${String(cell.day).padStart(2, "0")}`;
          const dayData = cell.data;
          const href = buildDayHref({ date: dateStr, env, mode });

          return (
            <Link
              key={i}
              href={href}
              className={cn(
                "flex h-10 flex-col items-center justify-center rounded-lg text-[11px] transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                dayData ? pnlIntensity(dayData.net_pnl) : "bg-foreground/[0.03] hover:bg-foreground/[0.06]",
              )}
              title={
                dayData
                  ? `${dayData.date}: ₹${dayData.net_pnl.toLocaleString("en-IN")} (${dayData.run_count} runs)`
                  : `${dateStr}: no trades`
              }
              aria-label={
                dayData
                  ? `${dateStr}, net pnl ${dayData.net_pnl}, ${dayData.run_count} runs`
                  : `${dateStr}, no trades`
              }
            >
              <span className="text-foreground/70">{cell.day}</span>
              {dayData && (
                <span className={cn("text-[8px] font-mono", dayData.net_pnl >= 0 ? "text-[var(--green)]" : "text-[var(--red)]")}>
                  {dayData.run_count}
                </span>
              )}
            </Link>
          );
        })}
      </div>

      <div className="mt-3 flex items-center gap-3 text-[10px] text-foreground/40">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded bg-[var(--green)]/25" /> profit
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded bg-[var(--red)]/25" /> loss
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded bg-foreground/[0.03]" /> no trades
        </span>
      </div>
    </Panel>
  );
}
