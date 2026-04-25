"use client";

import { KpiCard } from "@/components/operator/kpi-card";
import type { TradingPaperSummary, TradingBrokerSnapshot } from "@/features/trading/types";

type TradingKpiGridProps = {
  paper: TradingPaperSummary;
  broker: TradingBrokerSnapshot;
};

function formatCurrency(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

export function TradingKpiGrid({ paper, broker }: TradingKpiGridProps) {
  const totalRealized = paper.strategies.reduce((sum, s) => sum + s.realizedPnl, 0);
  const totalUnrealized = paper.strategies.reduce((sum, s) => sum + s.unrealizedPnl, 0);
  const openLegs = paper.strategies.reduce((sum, s) => sum + s.openLegCount, 0);

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <KpiCard
        label="Active strategies"
        value={String(paper.activeStrategyCount)}
        note={`${paper.strategies.length} total`}
      />
      <KpiCard
        label="Open legs"
        value={String(openLegs)}
        note={`${broker.activeCount} broker positions`}
      />
      <KpiCard
        label="Realized P&L"
        value={formatCurrency(totalRealized)}
        delta={totalRealized >= 0 ? "profit" : "loss"}
      />
      <KpiCard
        label="Unrealized P&L"
        value={formatCurrency(totalUnrealized)}
        delta={totalUnrealized >= 0 ? "profit" : "loss"}
      />
    </div>
  );
}
