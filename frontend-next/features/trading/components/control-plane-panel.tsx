"use client";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { ControlPlaneSnapshot, ControlStrategyGroup } from "@/features/trading/types";
import { ControlStrategyActions } from "./control-strategy-actions";

type Props = {
  snapshot: ControlPlaneSnapshot | null;
  onRefresh?: () => void;
};

function formatCurrency(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function healthTone(status: ControlStrategyGroup["healthStatus"]): "positive" | "warning" | "danger" | "neutral" {
  if (status === "healthy") return "positive";
  if (status === "stale") return "warning";
  if (status === "disconnected") return "danger";
  return "neutral";
}

function protectionTone(status: string): "positive" | "warning" | "danger" | "neutral" {
  if (status === "active") return "positive";
  if (status === "error") return "danger";
  if (status === "pending_exit" || status === "stale") return "warning";
  return "neutral";
}

export function ControlPlanePanel({ snapshot, onRefresh }: Props) {
  if (!snapshot) {
    return (
      <Panel
        eyebrow="operator"
        title="Control plane"
        action={
          onRefresh ? (
            <button
              type="button"
              onClick={onRefresh}
              className="rounded-md border border-border/50 px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-foreground/60 hover:border-primary/40 hover:text-foreground/80"
            >
              Refresh
            </button>
          ) : undefined
        }
      >
        <p className="py-4 text-sm text-foreground/50">Control-plane snapshot is loading.</p>
      </Panel>
    );
  }

  return (
    <Panel
      eyebrow="operator"
      title="Control plane"
      action={
        onRefresh ? (
          <button
            type="button"
            onClick={onRefresh}
            className="rounded-md border border-border/50 px-2 py-1 text-[10px] font-medium uppercase tracking-wider text-foreground/60 hover:border-primary/40 hover:text-foreground/80"
          >
            Refresh
          </button>
        ) : undefined
      }
      data-testid="control-plane-panel"
    >
      <div className="mb-4 grid gap-3 md:grid-cols-4">
        <Metric label="Open strategies" value={snapshot.totals.openStrategyCount.toString()} />
        <Metric label="Positions" value={snapshot.totals.positionCount.toString()} />
        <Metric label="Net P&L" value={formatCurrency(snapshot.totals.netPnl)} />
        <Metric label="Stale workers" value={snapshot.totals.staleWorkerCount.toString()} />
      </div>

      {snapshot.strategies.length === 0 ? (
        <p className="py-4 text-center text-sm text-foreground/40">No control-plane strategies available</p>
      ) : (
        <div className="space-y-3">
          {snapshot.strategies.map((strategy) => (
            <div key={strategy.strategyRunId} className="rounded-xl border border-border/50 bg-background/50 px-4 py-3">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-semibold">{strategy.displayName}</span>
                    <StatusBadge tone={strategy.isOpen ? "positive" : "neutral"}>{strategy.isOpen ? "open" : "closed"}</StatusBadge>
                    <StatusBadge tone={healthTone(strategy.healthStatus)}>{strategy.healthStatus}</StatusBadge>
                    <span className="text-[10px] uppercase tracking-wider text-foreground/40">
                      {strategy.source} · {strategy.mode}
                    </span>
                  </div>
                  <p className="font-mono text-[11px] text-foreground/45">
                    run {strategy.strategyRunId}
                    {strategy.workerName ? ` · worker ${strategy.workerName}` : ""}
                    {strategy.heartbeatAgeSec != null ? ` · heartbeat ${strategy.heartbeatAgeSec}s ago` : ""}
                  </p>
                  <p className="text-xs text-foreground/50">
                    Realized <span className={strategy.realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatCurrency(strategy.realizedPnl)}</span>{" "}
                    · Unrealized <span className={strategy.unrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatCurrency(strategy.unrealizedPnl)}</span>{" "}
                    · Net <span className={strategy.netPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatCurrency(strategy.netPnl)}</span>
                  </p>
                  <div className="rounded-lg border border-border/40 bg-background/40 px-3 py-2 text-xs text-foreground/55">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[10px] uppercase tracking-wider text-foreground/40">Protection</span>
                      <StatusBadge tone={protectionTone(strategy.protection.status)}>
                        {strategy.protection.source} · {strategy.protection.status}
                      </StatusBadge>
                    </div>
                    <p className="mt-1">{strategy.protection.summary}</p>
                  </div>
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
      )}

      <div className="mt-4 rounded-xl border border-dashed border-border/60 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold">{snapshot.unattributed.displayName}</h3>
          <span className="font-mono text-xs text-foreground/50">Net {formatCurrency(snapshot.unattributed.netPnl)}</span>
        </div>
        {snapshot.unattributed.positions.length === 0 ? (
          <p className="mt-2 text-sm text-foreground/40">No unattributed broker exposure.</p>
        ) : (
          <div className="mt-2 space-y-1 text-xs text-foreground/60">
            {snapshot.unattributed.positions.map((position, index) => (
              <p key={index} className="font-mono">
                {String(position.tradingsymbol ?? position.position_key ?? "unknown")} · qty {String(position.quantity ?? position.net_quantity ?? "—")}
              </p>
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/50 px-3 py-2">
      <p className="text-[10px] uppercase tracking-wider text-foreground/40">{label}</p>
      <p className="mt-1 font-mono text-lg font-semibold">{value}</p>
    </div>
  );
}
