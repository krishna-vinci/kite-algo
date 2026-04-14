"use client";

import type { AnalysisPeriod } from "@/lib/journal/types";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

const periods: { label: string; value: AnalysisPeriod }[] = [
  { label: "Day", value: "day" },
  { label: "Week", value: "week" },
  { label: "Month", value: "month" },
  { label: "Year", value: "year" },
  { label: "All", value: "inception" },
];

type JournalHeaderProps = {
  period: AnalysisPeriod;
  onPeriodChange: (p: AnalysisPeriod) => void;
  action?: ReactNode;
  showPeriodSelector?: boolean;
};

export function JournalHeader({ period, onPeriodChange, action, showPeriodSelector = true }: JournalHeaderProps) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold tracking-tight">Journal</h2>
        <span className="inline-flex items-center rounded-full border border-blue-400/30 bg-blue-400/10 px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.24em] text-blue-300">
          NIFTY50
        </span>
      </div>

      <div className="flex items-center gap-2">
        {showPeriodSelector ? (
          <div className="flex items-center gap-1 rounded-full border border-border/70 bg-background/60 p-0.5">
            {periods.map((p) => (
              <button
                key={p.value}
                type="button"
                onClick={() => onPeriodChange(p.value)}
                className={cn(
                  "rounded-full px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.2em] transition-colors",
                  period === p.value
                    ? "bg-primary/15 text-primary"
                    : "text-foreground/50 hover:text-foreground/80",
                )}
              >
                {p.label}
              </button>
            ))}
          </div>
        ) : null}
        {action}
      </div>
    </div>
  );
}
