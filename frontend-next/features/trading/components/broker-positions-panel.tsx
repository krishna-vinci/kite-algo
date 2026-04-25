"use client";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { TradingBrokerSnapshot } from "@/features/trading/types";

type BrokerPositionsPanelProps = {
  broker: TradingBrokerSnapshot;
};

function formatCurrency(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

export function BrokerPositionsPanel({ broker }: BrokerPositionsPanelProps) {
  // Only show positions with non-zero quantity (active/live)
  const active = broker.positions.filter((p) => p.quantity !== 0);

  return (
    <Panel
      eyebrow="broker"
      title="Live broker positions"
      action={
        <StatusBadge tone={active.length > 0 ? "positive" : "neutral"}>
          {active.length} active
        </StatusBadge>
      }
      data-testid="broker-positions-panel"
    >
      {active.length === 0 ? (
        <p className="py-4 text-center text-sm text-foreground/40">No active broker positions</p>
      ) : (
        <div className="space-y-2">
          {/* Header */}
          <div className="grid grid-cols-[1fr_60px_80px_80px_80px] gap-2 px-2 text-[10px] uppercase tracking-wider text-foreground/40">
            <span>Symbol</span>
            <span className="text-right">Qty</span>
            <span className="text-right">Avg price</span>
            <span className="text-right">LTP</span>
            <span className="text-right">P&L</span>
          </div>
          {active.map((p) => (
            <div
              key={p.positionKey}
              className="grid grid-cols-[1fr_60px_80px_80px_80px] items-center gap-2 rounded-lg border border-border/40 bg-background/40 px-2 py-2"
            >
              <div>
                <span className="font-mono text-xs font-medium text-foreground/80">
                  {p.tradingSymbol}
                </span>
                <span className="ml-1.5 text-[10px] text-foreground/40">
                  {p.exchange}·{p.product}
                </span>
              </div>
              <span
                className={`text-right font-mono text-xs ${p.quantity > 0 ? "text-emerald-400" : "text-rose-400"}`}
              >
                {p.quantity}
              </span>
              <span className="text-right font-mono text-xs text-foreground/60">
                {p.averagePrice.toFixed(2)}
              </span>
              <span className="text-right font-mono text-xs text-foreground/60">
                {p.lastPrice.toFixed(2)}
              </span>
              <span
                className={`text-right font-mono text-xs font-medium ${p.pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}
              >
                {formatCurrency(p.pnl)}
              </span>
            </div>
          ))}
        </div>
      )}
      <p className="mt-3 text-[10px] text-foreground/30">
        Only active quantities shown. Closed (qty=0) positions are excluded from this view.
      </p>
    </Panel>
  );
}
