"use client";

import { useEffect, useState } from "react";
import type { AnalysisPeriod, CalendarDay } from "@/lib/journal/types";
import { fetchCalendar } from "@/lib/journal/api";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalHeader } from "@/components/journal/journal-header";
import { CalendarHeatmap } from "@/components/journal/calendar-heatmap";

export default function JournalCalendarPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");

  const now = new Date();
  const [month, setMonth] = useState(now.getMonth() + 1);
  const [year, setYear] = useState(now.getFullYear());

  const [days, setDays] = useState<CalendarDay[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;

    fetchCalendar({ month: String(month), year: String(year) })
      .then((d) => {
        if (!disposed) { setDays(d); }
      })
      .catch((e) => {
        if (!disposed) { setError(e instanceof Error ? e.message : "Failed to load calendar"); }
      })
      .finally(() => {
        if (!disposed) { setLoading(false); }
      });

    return () => { disposed = true; };
  }, [month, year]);

  function goToPreviousMonth() {
    setLoading(true);
    setError(null);
    if (month === 1) {
      setMonth(12);
      setYear(year - 1);
    } else {
      setMonth(month - 1);
    }
  }

  function goToNextMonth() {
    setLoading(true);
    setError(null);
    if (month === 12) {
      setMonth(1);
      setYear(year + 1);
    } else {
      setMonth(month + 1);
    }
  }

  return (
    <div className="space-y-4 pb-4">
      <JournalHeader
        period={period}
        onPeriodChange={setPeriod}
        showPeriodSelector={false}
        action={
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={goToPreviousMonth}
              className="rounded-lg border border-border/70 bg-background/60 px-2.5 py-1.5 text-xs font-medium text-foreground/70 transition-colors hover:text-foreground"
              aria-label="Previous month"
            >
              ←
            </button>
            <button
              type="button"
              onClick={goToNextMonth}
              className="rounded-lg border border-border/70 bg-background/60 px-2.5 py-1.5 text-xs font-medium text-foreground/70 transition-colors hover:text-foreground"
              aria-label="Next month"
            >
              →
            </button>
          </div>
        }
      />
      <JournalNav />

      <CalendarHeatmap days={days} loading={loading} error={error} month={month} year={year} />
    </div>
  );
}
