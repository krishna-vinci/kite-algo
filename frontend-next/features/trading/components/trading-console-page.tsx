"use client";

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { ApiClientError } from "@/lib/api/client";
import type { ControlStrategyGroup, TradingConsoleSnapshot, TradingStrategyGroup } from "@/features/trading/types";
import { exitPaperStrategy } from "@/features/trading/api";
import { MarketQuoteStrip } from "./market-quote-strip";
import { RuntimeHealthCard } from "./runtime-health-card";
import { TradingKpiGrid } from "./trading-kpi-grid";
import { StrategyGroupsPanel } from "./strategy-groups-panel";
import { BrokerPositionsPanel } from "./broker-positions-panel";
import { ControlStrategyActions } from "./control-strategy-actions";

type TradingConsolePageProps = {
  snapshot: TradingConsoleSnapshot;
};

const ACCOUNT_SCOPE = "default";

function getSelectedMode(modeParam: string | null) {
  return modeParam === "paper" ? "paper" : "live";
}

function formatCurrency(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function modeButtonClass(active: boolean) {
  return active
    ? "border-primary/40 bg-primary/10 text-primary"
    : "border-border/70 bg-background/50 text-foreground/65 hover:border-primary/30 hover:text-foreground";
}

function splitPaperStrategies(strategies: TradingStrategyGroup[]) {
  const visible = strategies.filter((strategy) => strategy.mode !== "dry_run");
  return {
    active: visible.filter((strategy) => strategy.isOpen),
    recent: visible.filter((strategy) => !strategy.isOpen),
    hiddenDryRunCount: strategies.length - visible.length,
  };
}

function splitLiveStrategies(strategies: ControlStrategyGroup[]) {
  const visible = strategies.filter((strategy) => strategy.mode === "live");
  return {
    active: visible.filter((strategy) => strategy.isOpen),
    recent: visible.filter((strategy) => !strategy.isOpen),
    hiddenDryRunCount: strategies.filter((strategy) => strategy.mode === "dry_run").length,
  };
}

export function TradingConsolePage({ snapshot }: TradingConsolePageProps) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [exitingStrategyId, setExitingStrategyId] = useState<string | null>(null);

  const selectedMode = getSelectedMode(searchParams.get("mode"));

  const paperSections = useMemo(() => splitPaperStrategies(snapshot.paper.strategies), [snapshot.paper.strategies]);
  const liveSections = useMemo(() => splitLiveStrategies(snapshot.control?.strategies ?? []), [snapshot.control?.strategies]);
  const hiddenDryRunCount = paperSections.hiddenDryRunCount + liveSections.hiddenDryRunCount;

  function setMode(mode: "live" | "paper") {
    const next = new URLSearchParams(searchParams.toString());
    if (mode === "live") {
      next.delete("mode");
    } else {
      next.set("mode", mode);
    }
    const query = next.toString();
    router.replace(query ? `/strategies?${query}` : "/strategies");
  }

  async function handleExitPaperStrategy(strategy: TradingStrategyGroup) {
    setExitingStrategyId(strategy.strategyRunId);
    try {
      const result = await exitPaperStrategy(ACCOUNT_SCOPE, strategy.strategyRunId);
      toast.success(result.status === "noop" ? result.message ?? "No open positions for strategy" : `Exited strategy · ${strategy.displayName}`);
      await queryClient.invalidateQueries({ queryKey: ["trading", "paper-summary", ACCOUNT_SCOPE] });
    } catch (err) {
      const message = err instanceof ApiClientError
        ? typeof err.body === "object" && err.body !== null && "detail" in (err.body as Record<string, unknown>)
          ? String((err.body as Record<string, unknown>).detail)
          : err.message
        : err instanceof Error
          ? err.message
          : "Failed to exit strategy";
      toast.error(message);
    } finally {
      setExitingStrategyId(null);
    }
  }

  return (
    <div className="space-y-4 pb-4">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
        <div className="space-y-3">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Strategies</h1>
            <p className="text-sm text-foreground/60">Primary operator console for live and paper strategy runs.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label="Strategy modes">
            <button
              type="button"
              role="tab"
              aria-selected={selectedMode === "live"}
              className={`rounded-full border px-3 py-1.5 text-[11px] font-medium uppercase tracking-[0.24em] transition-colors ${modeButtonClass(selectedMode === "live")}`}
              onClick={() => setMode("live")}
            >
              Live
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={selectedMode === "paper"}
              className={`rounded-full border px-3 py-1.5 text-[11px] font-medium uppercase tracking-[0.24em] transition-colors ${modeButtonClass(selectedMode === "paper")}`}
              onClick={() => setMode("paper")}
            >
              Paper
            </button>
          </div>
        </div>
        <MarketQuoteStrip quotes={snapshot.quotes} />
      </div>

      {hiddenDryRunCount > 0 ? (
        <Panel eyebrow="filtered" title="Dry-run records hidden" className="p-4">
          <p className="text-sm text-foreground/65">
            {hiddenDryRunCount} dry-run strateg{hiddenDryRunCount === 1 ? "y is" : "ies are"} intentionally removed from the primary operator view.
          </p>
        </Panel>
      ) : null}

      <TradingKpiGrid paper={snapshot.paper} broker={snapshot.broker} />

      <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="space-y-4">
          {selectedMode === "live" ? (
            <>
              <LiveStrategySection title="Active" strategies={liveSections.active} emptyCopy="No active live strategies are currently attributed to the control plane." />
              <LiveStrategySection title="Recent" strategies={liveSections.recent} emptyCopy="No recent closed live strategies available yet." testId="live-recent-strategies-panel" />
            </>
          ) : (
            <>
              <StrategyGroupsPanel
                title="Active"
                eyebrow="paper"
                testId="strategy-groups-panel"
                strategies={paperSections.active}
                emptyCopy="No active paper strategies yet. Paper runs will appear here once they are entered."
                renderActions={(strategy) => (
                  <button
                    type="button"
                    onClick={() => void handleExitPaperStrategy(strategy)}
                    disabled={!strategy.isOpen || !strategy.capabilities.canExitStrategy || exitingStrategyId === strategy.strategyRunId}
                    title={!strategy.capabilities.canExitStrategy ? strategy.capabilities.exitReason ?? "Strategy exit unavailable" : undefined}
                    className="rounded-md border border-rose-400/30 bg-rose-400/10 px-2 py-1 text-[10px] font-medium uppercase tracking-[0.18em] text-rose-300 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {exitingStrategyId === strategy.strategyRunId ? "Exiting…" : "Exit"}
                  </button>
                )}
              />
              <StrategyGroupsPanel
                title="Recent"
                eyebrow="paper"
                testId="strategy-groups-recent-panel"
                strategies={paperSections.recent}
                emptyCopy="No recent closed paper strategies available yet."
              />
            </>
          )}
        </div>

        <div className="space-y-4">
          {selectedMode === "live" ? <LiveModeSummaryPanel snapshot={snapshot} /> : <PaperModeSummaryPanel snapshot={snapshot} />}
          <RuntimeHealthCard runtime={snapshot.runtime} />
          <BrokerPositionsPanel broker={snapshot.broker} />
        </div>
      </div>
    </div>
  );
}

function LiveStrategySection({
  title,
  strategies,
  emptyCopy,
  testId = "live-active-strategies-panel",
}: {
  title: string;
  strategies: ControlStrategyGroup[];
  emptyCopy: string;
  testId?: string;
}) {
  return (
    <Panel eyebrow="live" title={title} data-testid={testId}>
      {strategies.length === 0 ? <p className="py-4 text-center text-sm text-foreground/40">{emptyCopy}</p> : null}
      <div className="space-y-3">
        {strategies.map((strategy) => (
          <div key={strategy.strategyRunId} className="rounded-xl border border-border/50 bg-background/50 px-4 py-3">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-semibold text-foreground/90">{strategy.displayName}</span>
                  <StatusBadge tone={strategy.isOpen ? "positive" : "neutral"}>{strategy.isOpen ? "open" : "closed"}</StatusBadge>
                  <StatusBadge tone={strategy.healthStatus === "healthy" ? "positive" : strategy.healthStatus === "stale" ? "warning" : strategy.healthStatus === "disconnected" ? "danger" : "neutral"}>
                    {strategy.healthStatus}
                  </StatusBadge>
                </div>
                <p className="font-mono text-[11px] text-foreground/45">
                  run {strategy.strategyRunId}
                  {strategy.workerName ? ` · worker ${strategy.workerName}` : ""}
                  {strategy.heartbeatAgeSec != null ? ` · heartbeat ${strategy.heartbeatAgeSec}s ago` : ""}
                </p>
                <p className="text-xs text-foreground/50">
                  Realized <span className={strategy.realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatCurrency(strategy.realizedPnl)}</span>
                  {" · "}
                  Unrealized <span className={strategy.unrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatCurrency(strategy.unrealizedPnl)}</span>
                  {" · "}
                  Net <span className={strategy.netPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatCurrency(strategy.netPnl)}</span>
                </p>
                <p className="text-[11px] text-foreground/45">Protection · {strategy.protection.source} · {strategy.protection.status}</p>
                <p className="text-xs text-foreground/55">{strategy.protection.summary}</p>
              </div>
              <div className="space-y-2 lg:text-right">
                <ControlStrategyActions strategy={strategy} />
                {!strategy.allowedActions.includes("cancel_orders") && strategy.actionReasons.cancel_orders ? (
                  <p className="text-[11px] text-foreground/40">{strategy.actionReasons.cancel_orders}</p>
                ) : null}
              </div>
            </div>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function LiveModeSummaryPanel({ snapshot }: { snapshot: TradingConsoleSnapshot }) {
  const control = snapshot.control;

  return (
    <Panel eyebrow="live" title="Control overview">
      {!control ? <p className="text-sm text-foreground/60">Loading live control-plane snapshot…</p> : null}
      {control ? (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <MetricCard label="Open live strategies" value={String(control.totals.openStrategyCount)} />
            <MetricCard label="Tracked positions" value={String(control.totals.positionCount)} />
            <MetricCard label="Net P&L" value={formatCurrency(control.totals.netPnl)} />
            <MetricCard label="Stale workers" value={String(control.totals.staleWorkerCount)} />
          </div>
          <div className="rounded-xl border border-dashed border-border/60 px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold">{control.unattributed.displayName}</h3>
              <span className="font-mono text-xs text-foreground/50">Net {formatCurrency(control.unattributed.netPnl)}</span>
            </div>
            {control.unattributed.positions.length === 0 ? (
              <p className="mt-2 text-sm text-foreground/40">No unattributed broker exposure.</p>
            ) : (
              <div className="mt-2 space-y-1 text-xs text-foreground/60">
                {control.unattributed.positions.map((position, index) => (
                  <p key={index} className="font-mono">
                    {String(position.tradingsymbol ?? position.position_key ?? "unknown")} · qty {String(position.quantity ?? position.net_quantity ?? "—")}
                  </p>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : null}
    </Panel>
  );
}

function PaperModeSummaryPanel({ snapshot }: { snapshot: TradingConsoleSnapshot }) {
  const account = snapshot.paper.account;
  return (
    <Panel eyebrow="paper" title="Paper account">
      <div className="grid gap-3 sm:grid-cols-2">
        <MetricCard label="Net P&L" value={formatCurrency(account.realizedPnl + account.unrealizedPnl)} />
        <MetricCard label="Available funds" value={account.availableFunds.toLocaleString("en-IN", { maximumFractionDigits: 0 })} />
        <MetricCard label="Open legs" value={String(account.openPositionCount)} />
        <MetricCard label="Tracked strategies" value={String(snapshot.paper.strategies.filter((strategy) => strategy.mode !== "dry_run").length)} />
      </div>
    </Panel>
  );
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/50 px-3 py-2">
      <p className="text-[10px] uppercase tracking-wider text-foreground/40">{label}</p>
      <p className="mt-1 font-mono text-lg font-semibold">{value}</p>
    </div>
  );
}
