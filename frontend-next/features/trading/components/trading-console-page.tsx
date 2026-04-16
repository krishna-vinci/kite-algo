"use client";

import type { TradingConsoleSnapshot } from "@/features/trading/types";
import { MarketQuoteStrip } from "./market-quote-strip";
import { RuntimeHealthCard } from "./runtime-health-card";
import { TradingKpiGrid } from "./trading-kpi-grid";
import { StrategyGroupsPanel } from "./strategy-groups-panel";
import { BrokerPositionsPanel } from "./broker-positions-panel";

type TradingConsolePageProps = {
  snapshot: TradingConsoleSnapshot;
};

export function TradingConsolePage({ snapshot }: TradingConsolePageProps) {
  return (
    <div className="space-y-4 pb-4">
      {/* Page heading + quote strip */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Trading console</h1>
        <MarketQuoteStrip quotes={snapshot.quotes} />
      </div>

      {/* KPI summary */}
      <TradingKpiGrid paper={snapshot.paper} broker={snapshot.broker} />

      {/* Main content grid */}
      <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <StrategyGroupsPanel strategies={snapshot.paper.strategies} />
        <div className="space-y-4">
          <RuntimeHealthCard runtime={snapshot.runtime} />
          <BrokerPositionsPanel broker={snapshot.broker} />
        </div>
      </div>
    </div>
  );
}
