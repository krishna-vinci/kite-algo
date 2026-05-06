"use client";

import * as React from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type DateNavView = "day" | "week" | "month" | "year";

type DateNavProps = {
  /** ISO date string (YYYY-MM-DD) representing the current anchor date */
  date: string;
  view: DateNavView;
  onChange: (newDate: string) => void;
  className?: string;
};

// ---------------------------------------------------------------------------
// Pure date helpers — no external deps
// ---------------------------------------------------------------------------

function parseDate(iso: string): Date {
  // Parse as UTC-noon to avoid timezone edge-cases when formatting
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 12));
}

function toIso(d: Date): string {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function todayIso(): string {
  return toIso(new Date());
}

function stepDate(iso: string, view: DateNavView, direction: -1 | 1): string {
  const d = parseDate(iso);
  switch (view) {
    case "day":
      d.setUTCDate(d.getUTCDate() + direction);
      break;
    case "week":
      d.setUTCDate(d.getUTCDate() + direction * 7);
      break;
    case "month":
      d.setUTCMonth(d.getUTCMonth() + direction);
      break;
    case "year":
      d.setUTCFullYear(d.getUTCFullYear() + direction);
      break;
  }
  return toIso(d);
}

/** Human-readable label for the current anchor depending on view */
function formatLabel(iso: string, view: DateNavView): string {
  const d = parseDate(iso);
  const locale = "en-IN";

  switch (view) {
    case "day":
      return d.toLocaleDateString(locale, {
        weekday: "short",
        day: "2-digit",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      });

    case "week": {
      // Show Mon–Sun of the ISO week containing 'date'
      const dow = d.getUTCDay(); // 0=Sun
      const monday = new Date(d);
      monday.setUTCDate(d.getUTCDate() - ((dow + 6) % 7));
      const sunday = new Date(monday);
      sunday.setUTCDate(monday.getUTCDate() + 6);
      const fmtShort = (dt: Date) =>
        dt.toLocaleDateString(locale, {
          day: "2-digit",
          month: "short",
          timeZone: "UTC",
        });
      const yearPart = sunday.toLocaleDateString(locale, {
        year: "numeric",
        timeZone: "UTC",
      });
      return `${fmtShort(monday)} – ${fmtShort(sunday)}, ${yearPart}`;
    }

    case "month":
      return d.toLocaleDateString(locale, {
        month: "long",
        year: "numeric",
        timeZone: "UTC",
      });

    case "year":
      return String(d.getUTCFullYear());
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * DateNav — stateless controlled date navigator with prev / next / today.
 * Stepping logic is view-aware: day ±1d, week ±7d, month ±1mo, year ±1yr.
 */
export function DateNav({ date, view, onChange, className }: DateNavProps) {
  const label = formatLabel(date, view);
  const isToday = date === todayIso();

  return (
    <nav
      className={cn("flex items-center gap-1", className)}
      aria-label="Date navigation"
    >
      <Button
        variant="ghost"
        size="icon"
        className="size-7"
        onClick={() => onChange(stepDate(date, view, -1))}
        aria-label="Previous"
      >
        <ChevronLeftIcon data-icon="inline-start" />
      </Button>

      <span className="min-w-[11rem] text-center text-sm font-medium tabular-nums select-none">
        {label}
      </span>

      <Button
        variant="ghost"
        size="icon"
        className="size-7"
        onClick={() => onChange(stepDate(date, view, 1))}
        aria-label="Next"
      >
        <ChevronRightIcon data-icon="inline-end" />
      </Button>

      <Button
        variant="outline"
        size="sm"
        className="ml-1 h-7 px-2.5 text-xs"
        onClick={() => onChange(todayIso())}
        disabled={isToday && view === "day"}
        aria-label="Go to today"
      >
        Today
      </Button>
    </nav>
  );
}
