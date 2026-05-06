import * as React from "react";

import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type MetricValueProps = {
  value: string | number | null | undefined;
  formatter?: (v: string | number) => string;
  className?: string;
  fallback?: React.ReactNode;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * MetricValue — renders a formatted value or an em-dash fallback when the
 * value is null / undefined / empty string.  Used as a leaf in all data-dense
 * table cells, stat cards, and P&L badges.
 */
export function MetricValue({
  value,
  formatter,
  className,
  fallback = "—",
}: MetricValueProps) {
  const isEmpty =
    value === null || value === undefined || value === "";

  if (isEmpty) {
    return (
      <span
        className={cn("text-muted-foreground tabular-nums", className)}
        aria-label="no data"
      >
        {fallback}
      </span>
    );
  }

  const displayed = formatter ? formatter(value) : String(value);

  return (
    <span className={cn("tabular-nums", className)}>{displayed}</span>
  );
}
