"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { KpiCard } from "@/components/operator/kpi-card";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";
import { ApiClientError } from "@/lib/api/client";
import { exitPaperStrategy, fetchPaperStrategies, type PaperStrategyGroup, type PaperStrategySummaryResponse } from "@/lib/paper/api";

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
  const [summary, setSummary] = useState<PaperStrategySummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exitingStrategyId, setExitingStrategyId] = useState<string | null>(null);

  const load = useCallback(async (options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent);
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const data = await fetchPaperStrategies(ACCOUNT_SCOPE);
      setSummary(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load paper strategies");
    } finally {
      if (silent) {
        setRefreshing(false);
      } else {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const id = window.setInterval(() => {
      void load({ silent: true });
    }, 4000);
    return () => window.clearInterval(id);
  }, [load]);

  const metrics = useMemo(() => {
    const account = summary?.account;
    const strategies = summary?.strategies ?? [];
    const openStrategies = strategies.filter((item) => item.open_leg_count > 0).length;
    return [
      { label: "Net P&L", value: currency((account?.realized_pnl ?? 0) + (account?.unrealized_pnl ?? 0)), note: `${currency(account?.realized_pnl ?? 0)} realized · ${currency(account?.unrealized_pnl ?? 0)} unrealized` },
      { label: "Available funds", value: currency(account?.available_funds ?? 0), note: `${currency(account?.blocked_funds ?? 0)} blocked margin` },
      { label: "Open strategies", value: String(openStrategies), note: `${account?.open_position_count ?? 0} open legs tracked live` },
      { label: "Tracked strategies", value: String(strategies.length), note: `Account scope · ${ACCOUNT_SCOPE}` },
    ];
  }, [summary]);

  async function handleExitStrategy(strategy: PaperStrategyGroup) {
    setExitingStrategyId(strategy.strategy_id);
    try {
      const result = await exitPaperStrategy(ACCOUNT_SCOPE, strategy.strategy_id);
      toast.success(result.status === "noop" ? result.message ?? "No open positions for strategy" : `Exited strategy · ${strategy.display_name}`);
      await load();
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
        <Panel eyebrow="error" title="Paper page unavailable" action={<button type="button" onClick={() => void load()} className="rounded-full border border-border/70 px-3 py-2 text-xs uppercase tracking-[0.24em] text-foreground/70">Retry</button>}>
          <p className="text-sm text-rose-300">{error}</p>
        </Panel>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
          <Panel eyebrow="strategies" title="Tracked paper strategies">
            <div className="space-y-4">
              {(summary?.strategies ?? []).length === 0 ? (
                <div className="rounded-2xl border border-dashed border-border/70 p-6 text-sm text-foreground/60">No paper strategies yet. Execute a paper strategy from the options builder and it will appear here.</div>
              ) : (
                summary?.strategies.map((strategy) => {
                  const netPnl = strategy.realized_pnl + strategy.unrealized_pnl;
                  return (
                    <article key={strategy.strategy_id} className="rounded-[1.25rem] border border-border/70 bg-background/50 p-4">
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div className="space-y-1">
                          <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{strategy.strategy_tag ?? "manual strategy"}</p>
                          <h3 className="text-lg font-semibold tracking-tight">{strategy.display_name}</h3>
                          <p className="font-mono text-xs text-foreground/50">{strategy.strategy_id}</p>
                          <p className="text-sm text-foreground/60">{strategy.open_leg_count} open legs · {strategy.orders.length} orders · {strategy.trades.length} fills · updated {formatDate(strategy.last_updated_at)}</p>
                        </div>
                        <div className="flex flex-col items-start gap-2 sm:items-end">
                          <StatusBadge tone={strategy.open_leg_count > 0 ? "warning" : "neutral"}>{strategy.open_leg_count > 0 ? "open" : "closed"}</StatusBadge>
                          <p className={`font-mono text-sm ${netPnl >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{netPnl >= 0 ? "+" : ""}{currency(netPnl)}</p>
                          <button
                            type="button"
                            onClick={() => void handleExitStrategy(strategy)}
                            disabled={strategy.open_leg_count === 0 || exitingStrategyId === strategy.strategy_id}
                            className="rounded-full border border-rose-400/30 bg-rose-400/10 px-3 py-2 text-xs font-medium uppercase tracking-[0.24em] text-rose-300 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {exitingStrategyId === strategy.strategy_id ? "Exiting…" : "Exit strategy"}
                          </button>
                        </div>
                      </div>

                      <div className="mt-4 grid gap-3 md:grid-cols-3">
                        <KpiCard label="Unrealized" value={currency(strategy.unrealized_pnl)} className="p-3" />
                        <KpiCard label="Realized" value={currency(strategy.realized_pnl)} className="p-3" />
                        <KpiCard label="Margin in use" value={currency(strategy.margin_in_use)} className="p-3" />
                      </div>

                      <div className="mt-4 overflow-hidden rounded-2xl border border-border/60">
                        <table className="w-full text-left text-sm">
                          <thead className="bg-muted/30 text-[10px] uppercase tracking-[0.28em] text-foreground/40">
                            <tr>
                              <th className="px-3 py-2 font-medium">Leg</th>
                              <th className="px-3 py-2 font-medium">Side</th>
                              <th className="px-3 py-2 font-medium">Qty</th>
                              <th className="px-3 py-2 font-medium">Avg</th>
                              <th className="px-3 py-2 font-medium">LTP</th>
                              <th className="px-3 py-2 font-medium">MTM</th>
                            </tr>
                          </thead>
                          <tbody>
                            {strategy.positions.map((position) => (
                              <tr key={`${strategy.strategy_id}:${position.instrument_token}:${position.product}`} className="border-t border-border/60 text-foreground/80">
                                <td className="px-3 py-3 font-mono text-sm">{position.tradingsymbol ?? position.instrument_token}</td>
                                <td className="px-3 py-3 text-xs uppercase tracking-[0.24em]">{position.side}</td>
                                <td className="px-3 py-3 font-mono text-sm">{position.net_quantity}</td>
                                <td className="px-3 py-3 font-mono text-sm">{position.average_price.toFixed(2)}</td>
                                <td className="px-3 py-3 font-mono text-sm">{position.last_price.toFixed(2)}</td>
                                <td className={`px-3 py-3 font-mono text-sm ${position.unrealized_pnl >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{currency(position.unrealized_pnl)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </article>
                  );
                })
              )}
            </div>
          </Panel>

          <Panel eyebrow="activity" title="Orders and fills">
            <div className="space-y-4">
              {(summary?.strategies ?? []).slice(0, 6).map((strategy) => (
                <div key={`${strategy.strategy_id}:activity`} className="rounded-2xl border border-border/60 bg-background/60 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-sm font-semibold tracking-tight">{strategy.display_name}</p>
                      <p className="font-mono text-[11px] text-foreground/50">{strategy.strategy_id}</p>
                    </div>
                    <StatusBadge tone={toneForPnl(strategy.unrealized_pnl)}>{strategy.orders.length} orders</StatusBadge>
                  </div>

                  <div className="mt-3 space-y-2">
                    {strategy.orders.slice(0, 3).map((order) => (
                      <div key={order.order_id} className="rounded-xl border border-border/50 px-3 py-2 text-xs text-foreground/70">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono">{order.tradingsymbol}</span>
                          <span>{order.status}</span>
                        </div>
                        <div className="mt-1 flex items-center justify-between gap-2 font-mono text-[11px] text-foreground/50">
                          <span>{order.transaction_type} · {order.quantity}</span>
                          <span>{formatDate(order.placed_at)}</span>
                        </div>
                      </div>
                    ))}
                    {strategy.orders.length === 0 ? <p className="text-xs text-foreground/50">No orders recorded.</p> : null}
                  </div>

                  <div className="mt-4 space-y-2">
                    <p className="text-[10px] uppercase tracking-[0.28em] text-foreground/40">Recent fills</p>
                    {strategy.trades.slice(0, 3).map((trade) => (
                      <div key={trade.trade_id} className="rounded-xl border border-border/50 px-3 py-2 text-xs text-foreground/70">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono">{trade.order_id}</span>
                          <span>{currency(trade.price)}</span>
                        </div>
                        <div className="mt-1 flex items-center justify-between gap-2 font-mono text-[11px] text-foreground/50">
                          <span>{trade.transaction_type} · {trade.quantity}</span>
                          <span>{formatDate(trade.trade_timestamp)}</span>
                        </div>
                      </div>
                    ))}
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
