"use client";

import * as React from "react";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type Period = "day" | "week" | "month" | "year" | "since_inception";

type PeriodOption = {
  value: Period;
  label: string;
};

const DEFAULT_OPTIONS: PeriodOption[] = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "year", label: "Year" },
  { value: "since_inception", label: "All" },
];

type PeriodSelectorProps = {
  value: Period;
  onChange: (period: Period) => void;
  options?: PeriodOption[];
  /** Controls toggle-group size variant */
  size?: "sm" | "default";
  className?: string;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * PeriodSelector — compact ToggleGroup for selecting a reporting period.
 * Stateless / controlled.
 */
export function PeriodSelector({
  value,
  onChange,
  options = DEFAULT_OPTIONS,
  size = "sm",
  className,
}: PeriodSelectorProps) {
  return (
    <ToggleGroup
      type="single"
      value={value}
      onValueChange={(v) => {
        if (v) onChange(v as Period);
      }}
      size={size}
      variant="outline"
      className={cn("gap-0", className)}
      aria-label="Select period"
    >
      {options.map((opt) => (
        <ToggleGroupItem
          key={opt.value}
          value={opt.value}
          aria-label={opt.label}
          className="px-2.5 text-xs font-medium"
        >
          {opt.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
