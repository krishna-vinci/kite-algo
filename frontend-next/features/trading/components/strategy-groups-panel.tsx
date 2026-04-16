"use client";

import { useState } from "react";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { TradingStrategyGroup } from "@/features/trading/types";
import { RiskAdjustmentSheet } from "./risk-adjustment-sheet";

type StrategyGroupsPanelProps = {
  strategies: TradingStrategyGroup[];
};

function formatCurrency(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

function riskSummary(s: TradingStrategyGroup) {
  const parts: string[] = [];
  const r = s.riskControls;
  if (r.combinedPremiumStoploss !== null) parts.push(`SL ${r.combinedPremiumStoploss}`);
  if (r.combinedPremiumTarget !== null) parts.push(`TGT ${r.combinedPremiumTarget}`);
  if (r.basketMtmStoploss !== null) parts.push(`MTM-SL ${r.basketMtmStoploss}`);
  if (r.basketMtmTarget !== null) parts.push(`MTM-TGT ${r.basketMtmTarget}`);
  if (r.indexLowerBoundary !== null || r.indexUpperBoundary !== null) {
    parts.push(`Bounds ${r.indexLowerBoundary ?? "—"}–${r.indexUpperBoundary ?? "—"}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "No risk controls set";
}

export function StrategyGroupsPanel({ strategies }: StrategyGroupsPanelProps) {
  const [editTarget, setEditTarget] = useState<TradingStrategyGroup | null>(null);

  return (
    <>
      <Panel eyebrow="strategies" title="Strategy groups" data-testid="strategy-groups-panel">
        {strategies.length === 0 && (
          <p className="py-4 text-center text-sm text-foreground/40">No strategies loaded</p>
        )}
        <div className="space-y-3">
          {strategies.map((s) => (
            <div
              key={s.strategyId}
              className="rounded-xl border border-border/50 bg-background/50 px-4 py-3"
            >
              {/* Header row */}
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-semibold text-foreground/90" data-testid="strategy-name">
                    {s.displayName}
                  </span>
                  <StatusBadge tone={s.isOpen ? "positive" : "neutral"}>
                    {s.isOpen ? "open" : "closed"}
                  </StatusBadge>
                  <span className="text-[10px] uppercase tracking-wider text-foreground/40">{s.mode}</span>
                </div>
                <div className="flex items-center gap-3 font-mono text-xs">
                  <span className="text-foreground/50">
                    {s.openLegCount} leg{s.openLegCount !== 1 ? "s" : ""}
                  </span>
                </div>
              </div>

              {/* P&L row */}
              <div className="mt-2 flex items-center gap-4 text-xs">
                <span className="text-foreground/50">
                  Realized{" "}
                  <span className={s.realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>
                    {formatCurrency(s.realizedPnl)}
                  </span>
                </span>
                <span className="text-foreground/50">
                  Unrealized{" "}
                  <span className={s.unrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>
                    {formatCurrency(s.unrealizedPnl)}
                  </span>
                </span>
              </div>

              {/* Risk controls summary */}
              <div className="mt-2 flex items-center justify-between gap-2">
                <p className="font-mono text-[11px] text-foreground/40">{riskSummary(s)}</p>
                {s.capabilities.canEditRisk && (
                  <button
                    onClick={() => setEditTarget(s)}
                    className="rounded-md border border-border/50 px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-foreground/60 hover:border-primary/40 hover:text-foreground/80"
                  >
                    Edit risk
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </Panel>

      {editTarget && (
        <RiskAdjustmentSheet
          open
          onOpenChange={(open) => {
            if (!open) setEditTarget(null);
          }}
          strategyId={editTarget.strategyId}
          displayName={editTarget.displayName}
          riskControls={editTarget.riskControls}
        />
      )}
    </>
  );
}
