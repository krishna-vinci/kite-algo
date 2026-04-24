"use client";

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { KpiCard } from "@/components/operator/kpi-card";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";
import { ApiClientError } from "@/lib/api/client";
import { exitPaperStrategy } from "@/features/trading/api";
import { StrategyGroupsPanel } from "@/features/trading/components/strategy-groups-panel";
import { usePaperStrategySummary } from "@/features/trading/hooks/use-paper-strategy-summary";
import type { TradingStrategyGroup } from "@/features/trading/types";

const ACCOUNT_SCOPE = "default";

function currency(value: number) {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format(value ?? 0);
}

function toneForPnl(value: number) {
  if (value > 0) return "positive" as const;
  if (value < 0) return "danger" as const;
  return "neutral" as const;
}

function formatDate(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export default function PaperPage() {
  const queryClient = useQueryClient();
  const paperQuery = usePaperStrategySummary(ACCOUNT_SCOPE);
  const [exitingStrategyId, setExitingStrategyId] = useState<string | null>(null);
  const summary = paperQuery.data;
  const loading = paperQuery.isLoading;
  const refreshing = paperQuery.isFetching && !paperQuery.isLoading;
  const error = paperQuery.error instanceof Error ? paperQuery.error.message : null;

  const metrics = useMemo(() => {
    const account = summary?.account;
    const strategies = summary?.strategies ?? [];
    const openStrategies = strategies.filter((item) => item.isOpen).length;
    return [
      { label: "Net P&L", value: currency((account?.realizedPnl ?? 0) + (account?.unrealizedPnl ?? 0)), note: `${currency(account?.realizedPnl ?? 0)} realized · ${currency(account?.unrealizedPnl ?? 0)} unrealized` },
      { label: "Available funds", value: currency(account?.availableFunds ?? 0), note: `${currency(account?.blockedFunds ?? 0)} blocked margin` },
      { label: "Open strategies", value: String(openStrategies), note: `${account?.openPositionCount ?? 0} open legs tracked live` },
      { label: "Tracked strategies", value: String(strategies.length), note: `Account scope · ${ACCOUNT_SCOPE}` },
    ];
  }, [summary]);

  async function handleExitStrategy(strategy: TradingStrategyGroup) {
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
      <Panel
        eyebrow="paper"
        title="Strategy-centric paper book"
        action={<StatusBadge tone={error ? "danger" : refreshing ? "warning" : "positive"}>{error ? "degraded" : refreshing ? "refreshing" : "live"}</StatusBadge>}
      >
        <SectionLabel
          title="Paper account · default"
          description="Paper orders are simulated, but P&L and mark-to-market are driven by live market data. Exit works at strategy level and closes every open linked leg."
        />
      </Panel>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {metrics.map((metric) => (
          <KpiCard key={metric.label} {...metric} />
        ))}
      </div>

      {loading ? (
        <Panel eyebrow="loading" title="Loading paper strategies">
          <p className="text-sm text-foreground/60">Fetching account summary, strategy groups, orders, and trades…</p>
        </Panel>
      ) : error ? (
        <Panel eyebrow="error" title="Paper page unavailable" action={<button type="button" onClick={() => void paperQuery.refetch()} className="rounded-full border border-border/70 px-3 py-2 text-xs uppercase tracking-[0.24em] text-foreground/70">Retry</button>}>
          <p className="text-sm text-rose-300">{error}</p>
        </Panel>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
          <StrategyGroupsPanel
            strategies={summary?.strategies ?? []}
            emptyCopy="No paper strategies yet. Execute a paper strategy from the options builder and it will appear here."
            renderActions={(strategy) => (
              <button
                type="button"
                onClick={() => void handleExitStrategy(strategy)}
                disabled={!strategy.isOpen || !strategy.capabilities.canExitStrategy || exitingStrategyId === strategy.strategyRunId}
                title={!strategy.capabilities.canExitStrategy ? strategy.capabilities.exitReason ?? "Strategy exit unavailable" : undefined}
                className="rounded-md border border-rose-400/30 bg-rose-400/10 px-2 py-1 text-[10px] font-medium uppercase tracking-[0.18em] text-rose-300 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {exitingStrategyId === strategy.strategyRunId ? "Exiting…" : "Exit"}
              </button>
            )}
          />

          <Panel eyebrow="activity" title="Orders and fills">
            <div className="space-y-4">
              {(summary?.strategies ?? []).slice(0, 6).map((strategy) => (
                <div key={`${strategy.strategyRunId}:activity`} className="rounded-2xl border border-border/60 bg-background/60 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-sm font-semibold tracking-tight">{strategy.displayName}</p>
                      <p className="font-mono text-[11px] text-foreground/50">{strategy.strategyRunId}</p>
                    </div>
                    <StatusBadge tone={toneForPnl(strategy.unrealizedPnl)}>{strategy.orders.length} orders</StatusBadge>
                  </div>

                  <div className="mt-3 space-y-2">
                    {strategy.orders.slice(0, 3).map((order) => {
                      const typedOrder = order as Record<string, unknown>;
                      return (
                      <div key={String(typedOrder.order_id ?? typedOrder.orderId ?? "order")} className="rounded-xl border border-border/50 px-3 py-2 text-xs text-foreground/70">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono">{String(typedOrder.tradingsymbol ?? typedOrder.tradingSymbol ?? "Unknown")}</span>
                          <span>{String(typedOrder.status ?? "unknown")}</span>
                        </div>
                        <div className="mt-1 flex items-center justify-between gap-2 font-mono text-[11px] text-foreground/50">
                          <span>{String(typedOrder.transaction_type ?? typedOrder.transactionType ?? "—")} · {String(typedOrder.quantity ?? "—")}</span>
                          <span>{formatDate(typeof typedOrder.placed_at === "string" ? typedOrder.placed_at : typeof typedOrder.placedAt === "string" ? typedOrder.placedAt : null)}</span>
                        </div>
                      </div>
                    )})}
                    {strategy.orders.length === 0 ? <p className="text-xs text-foreground/50">No orders recorded.</p> : null}
                  </div>

                  <div className="mt-4 space-y-2">
                    <p className="text-[10px] uppercase tracking-[0.28em] text-foreground/40">Recent fills</p>
                    {strategy.trades.slice(0, 3).map((trade) => {
                      const typedTrade = trade as Record<string, unknown>;
                      return (
                      <div key={String(typedTrade.trade_id ?? typedTrade.tradeId ?? "trade")} className="rounded-xl border border-border/50 px-3 py-2 text-xs text-foreground/70">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono">{String(typedTrade.order_id ?? typedTrade.orderId ?? "order")}</span>
                          <span>{currency(typeof typedTrade.price === "number" ? typedTrade.price : 0)}</span>
                        </div>
                        <div className="mt-1 flex items-center justify-between gap-2 font-mono text-[11px] text-foreground/50">
                          <span>{String(typedTrade.transaction_type ?? typedTrade.transactionType ?? "—")} · {String(typedTrade.quantity ?? "—")}</span>
                          <span>{formatDate(typeof typedTrade.trade_timestamp === "string" ? typedTrade.trade_timestamp : typeof typedTrade.tradeTimestamp === "string" ? typedTrade.tradeTimestamp : null)}</span>
                        </div>
                      </div>
                    )})}
                    {strategy.trades.length === 0 ? <p className="text-xs text-foreground/50">No fills recorded.</p> : null}
                  </div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}
