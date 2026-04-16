"use client";

import { Fragment } from "react";

import type { TradingBrokerSnapshot, TradingPaperSummary } from "@/features/trading/types";

type CompactTradingDockProps = {
  workspace: string;
  paper: TradingPaperSummary;
  broker: TradingBrokerSnapshot;
};

function formatPnl(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function CompactTradingDock({ workspace, paper, broker }: CompactTradingDockProps) {
  const activeStrategies = paper.strategies.filter((strategy) => strategy.isOpen).slice(0, 3);
  const livePnl = paper.strategies.filter((strategy) => strategy.isOpen).reduce((sum, strategy) => sum + strategy.unrealizedPnl, 0);

  return (
    <footer className="border-t border-[var(--border)] bg-[var(--panel)]">
      <div className="flex items-center gap-2 border-b border-[var(--border-soft)] px-4 py-2 text-[11px]">
        <span className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1 text-[var(--text)]">active strategies</span>
        <span className="rounded-md px-2 py-1 text-[var(--dim)]">broker positions</span>
        <span className="rounded-md px-2 py-1 text-[var(--dim)]">fills</span>
        <span className="flex-1" />
        <span className="text-[9px] uppercase tracking-[0.08em] text-[var(--dim)]">workspace</span>
        <span className="text-[10px] text-[var(--muted)]">{workspace}</span>
        <span className="ml-3 text-[9px] uppercase tracking-[0.08em] text-[var(--dim)]">live p/l</span>
        <span className={livePnl >= 0 ? "font-semibold text-[var(--green)]" : "font-semibold text-[var(--red)]"}>{formatPnl(livePnl)}</span>
      </div>
      <div className="overflow-x-auto px-4 py-2 text-[11px]">
        {activeStrategies.length === 0 ? (
          <div className="text-[var(--muted)]">No active strategies. Broker active positions: {broker.activeCount}.</div>
        ) : (
          <div className="grid min-w-[780px] grid-cols-[220px_80px_80px_120px_120px_120px] gap-3 text-[var(--muted)]">
            <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">strategy</span>
            <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">mode</span>
            <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">legs</span>
            <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">realized</span>
            <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">unrealized</span>
            <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">risk</span>
            {activeStrategies.map((strategy) => (
              <Fragment key={strategy.strategyId}>
                <span key={`${strategy.strategyId}:name`} className="rounded bg-[var(--accent-soft)] px-2 py-1 text-[9px] font-bold text-[var(--accent)]">{strategy.displayName}</span>
                <span key={`${strategy.strategyId}:mode`}>{strategy.mode}</span>
                <span key={`${strategy.strategyId}:legs`}>{strategy.openLegCount}</span>
                <span key={`${strategy.strategyId}:realized`} className={strategy.realizedPnl >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}>{formatPnl(strategy.realizedPnl)}</span>
                <span key={`${strategy.strategyId}:unrealized`} className={strategy.unrealizedPnl >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}>{formatPnl(strategy.unrealizedPnl)}</span>
                <span key={`${strategy.strategyId}:risk`} className="truncate">{strategy.capabilities.canEditRisk ? "editable" : "locked"}</span>
              </Fragment>
            ))}
          </div>
        )}
      </div>
    </footer>
  );
}
