"use client";

import { cn } from "@/lib/utils";
import type { MarketQuote } from "@/features/trading/types";

type MarketQuoteStripProps = {
  quotes: MarketQuote[];
  compact?: boolean;
  className?: string;
};

function formatPrice(value: number | null) {
  if (value === null) return "—";
  return value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatChange(value: number | null) {
  if (value === null) return "";
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function MarketQuoteStrip({ quotes, compact = false, className }: MarketQuoteStripProps) {
  return (
    <div className={cn("flex items-center gap-4", className)} data-testid="market-quote-strip">
      {quotes.map((q) => (
        <div
          key={q.symbol}
          className={cn(
            "flex items-center gap-2 rounded-lg border border-border/40 bg-background/35 px-3",
            compact ? "py-1" : "py-2",
          )}
        >
          <span
            className={cn(
              "font-mono font-semibold tracking-wider text-foreground/80",
              compact ? "text-xs" : "text-sm",
            )}
          >
            {q.symbol}
          </span>
          <span className={cn("font-mono text-primary", compact ? "text-xs" : "text-sm")}>
            {formatPrice(q.lastPrice)}
          </span>
          {q.changePercent !== null && (
            <span
              className={cn(
                "font-mono",
                compact ? "text-[10px]" : "text-xs",
                q.changePercent >= 0 ? "text-emerald-400" : "text-rose-400",
              )}
            >
              {formatChange(q.changePercent)}
            </span>
          )}
          {!q.connected && (
            <span className="flex items-center gap-1" aria-label={`${q.symbol} disconnected`}>
              <span className="h-1.5 w-1.5 rounded-full bg-amber-400" aria-hidden="true" />
              <span className="sr-only">Disconnected</span>
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
