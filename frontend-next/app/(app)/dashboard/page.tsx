"use client";

import { Panel } from "@/components/operator/panel";
import { CompactTradingDock } from "@/features/trading/components/compact-trading-dock";
import { MarketQuoteStrip } from "@/features/trading/components/market-quote-strip";
import { RuntimeHealthCard } from "@/features/trading/components/runtime-health-card";
import { TradingKpiGrid } from "@/features/trading/components/trading-kpi-grid";
import { useTradingConsoleData } from "@/features/trading/hooks/use-trading-console-data";

export default function DashboardPage() {
  const snapshot = useTradingConsoleData();

  return (
    <div className="space-y-4 pb-4">
      <Panel eyebrow="dashboard" title="Operator overview">
        <div className="space-y-4">
          <MarketQuoteStrip quotes={snapshot.quotes} />
          <TradingKpiGrid paper={snapshot.paper} broker={snapshot.broker} />
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
        <RuntimeHealthCard runtime={snapshot.runtime} />
        <CompactTradingDock workspace="/dashboard" paper={snapshot.paper} broker={snapshot.broker} />
      </div>
    </div>
  );
}
