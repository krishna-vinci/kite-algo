"use client";

import * as React from "react";

import { cn } from "@/lib/utils";
import { MetricValue } from "@/components/shared/metric-value";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type PnlBadgeProps = {
  value: number | string | null | undefined;
  /** Optional formatter — receives the raw number and returns the display string */
  formatter?: (v: number) => string;
  className?: string;
  /** Show a + prefix for positive values (default: true) */
  showSign?: boolean;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toNumber(v: number | string | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

function defaultFormat(n: number): string {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * PnlBadge — displays a P&L value with semantic positive / negative / zero
 * colouring using project CSS tokens (--green, --red).
 *
 * Null / undefined / empty string renders an em-dash via MetricValue.
 */
export function PnlBadge({
  value,
  formatter,
  className,
  showSign = true,
}: PnlBadgeProps) {
  const numeric = toNumber(value);

  if (numeric === null) {
    return (
      <MetricValue
        value={null}
        className={cn("text-xs font-medium", className)}
      />
    );
  }

  const isPositive = numeric > 0;
  const isNegative = numeric < 0;

  const fmt = formatter ?? defaultFormat;
  const formatted = fmt(Math.abs(numeric));
  const sign = isPositive && showSign ? "+" : isNegative ? "−" : "";
  const display = `${sign}${formatted}`;

  return (
    <span
      data-pnl={isPositive ? "positive" : isNegative ? "negative" : "zero"}
      className={cn(
        "inline-flex shrink-0 items-center tabular-nums text-xs font-medium",
        isPositive && "text-[var(--green)]",
        isNegative && "text-[var(--red)]",
        !isPositive && !isNegative && "text-muted-foreground",
        className
      )}
      aria-label={`P&L ${display}`}
    >
      {display}
    </span>
  );
}
