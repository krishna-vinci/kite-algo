"use client";

import Link from "next/link";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { BrokerPositionsPanel } from "@/features/trading/components/broker-positions-panel";
import { MarketQuoteStrip } from "@/features/trading/components/market-quote-strip";
import { RuntimeHealthCard } from "@/features/trading/components/runtime-health-card";
import { StrategyGroupsPanel } from "@/features/trading/components/strategy-groups-panel";
import { useTradingConsoleData } from "@/features/trading/hooks/use-trading-console-data";

export default function AlgosPage() {
  const snapshot = useTradingConsoleData();
  const activeStrategies = snapshot.paper.strategies.filter((strategy) => strategy.isOpen);

  return (
    <div className="grid gap-4 pb-4 xl:grid-cols-[0.95fr_1.05fr]">
      <div className="space-y-4">
        <Panel
          eyebrow="algos"
          title="Algo runtime overview"
          action={<StatusBadge tone={snapshot.runtime.brokerConnected ? "positive" : "warning"}>{snapshot.runtime.brokerStatus}</StatusBadge>}
        >
          <p className="text-sm text-foreground/60">
            This page now reads the same canonical runtime, paper-strategy, and broker-position truth as the Trading Console.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Link href="/trading" className="rounded-full border border-primary/30 bg-primary/10 px-3 py-2 text-xs font-medium uppercase tracking-[0.24em] text-primary">
              Open trading console
            </Link>
            <span className="text-xs text-foreground/50">{activeStrategies.length} active strategy runs · {snapshot.broker.activeCount} live broker positions</span>
          </div>
          <MarketQuoteStrip quotes={snapshot.quotes} compact className="mt-4" />
        </Panel>

        <RuntimeHealthCard runtime={snapshot.runtime} />
        <BrokerPositionsPanel broker={snapshot.broker} />
      </div>

      <StrategyGroupsPanel
        strategies={snapshot.paper.strategies}
        emptyCopy="No active algo-linked strategy runs are available yet. Launch an options paper strategy or open the Trading Console for full ledgers."
        renderActions={() => (
          <Link
            href="/trading"
            className="rounded-md border border-border/50 px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-foreground/60 hover:border-primary/40 hover:text-foreground/80"
          >
            Inspect
          </Link>
        )}
      />
    </div>
  );
}
