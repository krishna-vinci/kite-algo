"use client";

import Link from "next/link";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import { KpiBand } from "@/components/shared/kpi-band";
import { RuntimeHealthCard } from "@/features/trading/components/runtime-health-card";
import { useTradingConsoleData } from "@/features/trading/hooks/use-trading-console-data";
import type { ControlStrategyGroup, TradingBrokerSnapshot, TradingPaperSummary } from "@/features/trading/types";

function formatCurrency(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function strategyHealthTone(status: ControlStrategyGroup["healthStatus"]): "positive" | "warning" | "danger" | "neutral" {
  if (status === "healthy") return "positive";
  if (status === "stale") return "warning";
  if (status === "disconnected") return "danger";
  return "neutral";
}

export default function DashboardPage() {
  const snapshot = useTradingConsoleData();
  const paperNet = snapshot.paper.account.realizedPnl + snapshot.paper.account.unrealizedPnl;
  const liveControlNet = snapshot.control?.totals.netPnl ?? null;
  const openBrokerPositions = snapshot.broker.positions.filter((position) => position.quantity !== 0);
  const openPaperLegs = snapshot.paper.strategies.reduce((sum, strategy) => sum + strategy.openLegCount, 0);
  const activeLiveStrategies = snapshot.control?.strategies.filter((strategy) => strategy.isOpen) ?? [];
  const activePaperStrategies = snapshot.paper.strategies.filter((strategy) => strategy.isOpen);
  const attentionStrategies = (snapshot.control?.strategies ?? []).filter((strategy) => strategy.healthStatus !== "healthy").slice(0, 3);
  const kpis = [
    {
      label: "Paper net P&L",
      value: formatCurrency(paperNet),
      meta: `${snapshot.paper.activeStrategyCount} active paper strategies`,
      tone: paperNet >= 0 ? ("positive" as const) : ("negative" as const),
    },
    {
      label: "Live control net",
      value: liveControlNet == null ? "—" : formatCurrency(liveControlNet),
      meta: snapshot.control ? `${snapshot.control.totals.openStrategyCount} open live strategies` : "Control plane loading",
      tone: liveControlNet != null && liveControlNet < 0 ? ("negative" as const) : ("default" as const),
    },
    {
      label: "Broker exposure",
      value: String(openBrokerPositions.length),
      meta: `${snapshot.broker.activeCount} broker positions tracked`,
    },
    {
      label: "Open paper legs",
      value: String(openPaperLegs),
      meta: snapshot.control ? `${snapshot.control.totals.staleWorkerCount} stale workers` : "Awaiting runtime state",
      tone: snapshot.control && snapshot.control.totals.staleWorkerCount > 0 ? ("warning" as const) : ("default" as const),
    },
  ];

  return (
    <div className="space-y-5 pb-6">
      <header className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-2">
          <p className="text-[11px] uppercase tracking-[0.24em] text-foreground/40">dashboard</p>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-foreground">Operator overview</h1>
            <p className="mt-2 max-w-2xl text-sm text-foreground/60">
              Minimal live orientation across control posture, broker exposure, and paper risk. Use Strategies for detailed execution management.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Link href="/strategies" className="rounded-lg border border-border/60 px-3 py-1.5 text-xs font-medium text-foreground/85 transition-colors hover:bg-card/60">
            Open Strategies
          </Link>
          <Link href="/journal" className="rounded-lg border border-border/60 px-3 py-1.5 text-xs font-medium text-foreground/75 transition-colors hover:bg-card/60">
            Open Journal
          </Link>
        </div>
      </header>

      <KpiBand items={kpis} />

      <AttentionTriageStrip
        staleWorkerCount={snapshot.control?.totals.staleWorkerCount ?? null}
        controlAvailable={Boolean(snapshot.control)}
        runtimeBrokerStatus={snapshot.runtime.brokerStatus}
        runtimeWebsocketStatus={snapshot.runtime.websocketStatus}
        strategies={attentionStrategies}
      />

      <div className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
        <LivePosturePanel liveStrategies={activeLiveStrategies} controlNet={liveControlNet} />
        <div className="space-y-4">
          <RuntimeHealthCard runtime={snapshot.runtime} />
          <BrokerPosturePanel broker={snapshot.broker} />
        </div>
      </div>

      <WatchAndHandoffPanel liveStrategies={activeLiveStrategies} paperStrategies={activePaperStrategies} />
    </div>
  );
}

function AttentionTriageStrip({
  staleWorkerCount,
  controlAvailable,
  runtimeBrokerStatus,
  runtimeWebsocketStatus,
  strategies,
}: {
  staleWorkerCount: number | null;
  controlAvailable: boolean;
  runtimeBrokerStatus: string;
  runtimeWebsocketStatus: string;
  strategies: ControlStrategyGroup[];
}) {
  return (
    <section className="rounded-[1.1rem] border border-border/55 bg-card/45 px-4 py-3">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <p className="text-[10px] uppercase tracking-[0.22em] text-foreground/40">Attention and triage</p>
          <p className="mt-1 text-sm text-foreground/65">
            {strategies.length === 0
              ? "No degraded live strategy health currently surfaced."
              : "Review degraded live strategy health before moving into detailed controls."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <StatusBadge tone={controlAvailable ? "neutral" : "warning"}>{controlAvailable ? "control online" : "control loading"}</StatusBadge>
          <StatusBadge tone={runtimeBrokerStatus === "connected" ? "positive" : runtimeBrokerStatus === "disconnected" ? "danger" : "warning"}>broker {runtimeBrokerStatus}</StatusBadge>
          <StatusBadge tone={runtimeWebsocketStatus === "connected" || runtimeWebsocketStatus === "active" ? "positive" : runtimeWebsocketStatus === "disconnected" ? "danger" : "warning"}>
            ws {runtimeWebsocketStatus}
          </StatusBadge>
          <StatusBadge tone={staleWorkerCount && staleWorkerCount > 0 ? "warning" : "neutral"}>
            {staleWorkerCount == null ? "workers —" : `stale workers ${staleWorkerCount}`}
          </StatusBadge>
        </div>
      </div>

      {strategies.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {strategies.map((strategy) => (
            <div key={strategy.strategyRunId} className="flex items-center gap-2 rounded-lg border border-border/50 bg-background/45 px-3 py-1.5">
              <StatusBadge tone={strategyHealthTone(strategy.healthStatus)}>{strategy.healthStatus}</StatusBadge>
              <span className="font-mono text-xs text-foreground/85">{strategy.displayName}</span>
              {strategy.heartbeatAgeSec != null ? <span className="text-[10px] text-foreground/45">{strategy.heartbeatAgeSec}s</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function LivePosturePanel({ liveStrategies, controlNet }: { liveStrategies: ControlStrategyGroup[]; controlNet: number | null }) {
  const shown = liveStrategies.slice(0, 4);

  return (
    <Panel eyebrow="live" title="Live posture">
      <div className="mb-3 grid gap-2 sm:grid-cols-3">
        <InlineMetric label="Open strategies" value={String(liveStrategies.length)} />
        <InlineMetric label="Live control net" value={controlNet == null ? "—" : formatCurrency(controlNet)} tone={controlNet != null && controlNet < 0 ? "negative" : "default"} />
        <InlineMetric
          label="Degraded health"
          value={String(liveStrategies.filter((strategy) => strategy.healthStatus !== "healthy").length)}
          tone={liveStrategies.some((strategy) => strategy.healthStatus !== "healthy") ? "warning" : "default"}
        />
      </div>

      {shown.length === 0 ? (
        <p className="text-sm text-foreground/55">No active live strategies are currently attributed to the control plane.</p>
      ) : (
        <div className="divide-y divide-border/40 rounded-xl border border-border/45 bg-background/35">
          {shown.map((strategy) => (
            <div key={strategy.strategyRunId} className="flex items-start justify-between gap-3 px-4 py-3">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <p className="font-mono text-sm font-medium text-foreground/90">{strategy.displayName}</p>
                  <StatusBadge tone={strategyHealthTone(strategy.healthStatus)}>{strategy.healthStatus}</StatusBadge>
                </div>
                <p className="mt-1 text-xs text-foreground/55">
                  {strategy.workerName ? `worker ${strategy.workerName}` : "worker unassigned"}
                  {strategy.heartbeatAgeSec != null ? ` · heartbeat ${strategy.heartbeatAgeSec}s` : ""}
                </p>
              </div>
              <p className={strategy.netPnl >= 0 ? "font-mono text-sm font-semibold text-[var(--green)]" : "font-mono text-sm font-semibold text-[var(--red)]"}>
                {formatCurrency(strategy.netPnl)}
              </p>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function BrokerPosturePanel({ broker }: { broker: TradingBrokerSnapshot }) {
  const activePositions = broker.positions.filter((position) => position.quantity !== 0);
  const shown = activePositions.slice(0, 3);

  return (
    <Panel
      eyebrow="broker"
      title="Broker posture"
      tone="subtle"
      action={<StatusBadge tone={activePositions.length > 0 ? "positive" : "neutral"}>{activePositions.length} active</StatusBadge>}
    >
      {shown.length === 0 ? (
        <p className="text-sm text-foreground/55">No active broker positions at the moment.</p>
      ) : (
        <div className="space-y-2">
          {shown.map((position) => (
            <div key={position.positionKey} className="grid grid-cols-[1fr_auto] items-center gap-3 rounded-lg border border-border/45 bg-background/40 px-3 py-2.5">
              <div>
                <p className="font-mono text-sm font-medium text-foreground/90">{position.tradingSymbol}</p>
                <p className="text-[11px] text-foreground/45">
                  {position.exchange} · {position.product} · qty {position.quantity}
                </p>
              </div>
              <p className={position.pnl >= 0 ? "font-mono text-sm font-semibold text-[var(--green)]" : "font-mono text-sm font-semibold text-[var(--red)]"}>
                {formatCurrency(position.pnl)}
              </p>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function WatchAndHandoffPanel({
  liveStrategies,
  paperStrategies,
}: {
  liveStrategies: ControlStrategyGroup[];
  paperStrategies: TradingPaperSummary["strategies"];
}) {
  const watchItems = [
    ...liveStrategies.slice(0, 2).map((strategy) => ({
      key: strategy.strategyRunId,
      name: strategy.displayName,
      posture: "live",
      note: strategy.protection.summary,
      pnl: strategy.netPnl,
    })),
    ...paperStrategies.slice(0, 2).map((strategy) => ({
      key: strategy.strategyRunId,
      name: strategy.displayName,
      posture: "paper",
      note: `${strategy.openLegCount} open legs`,
      pnl: strategy.realizedPnl + strategy.unrealizedPnl,
    })),
  ];

  return (
    <Panel eyebrow="watch" title="Watch and handoff" tone="subtle" action={<Link href="/strategies" className="text-xs font-medium text-primary underline-offset-2 hover:underline">Open workspace</Link>}>
      <div className="mb-3 grid gap-2 sm:grid-cols-2">
        <InlineMetric label="Live in watch" value={String(liveStrategies.length)} />
        <InlineMetric label="Paper in watch" value={String(paperStrategies.length)} />
      </div>

      {watchItems.length === 0 ? (
        <p className="text-sm text-foreground/55">No active strategies yet. New live or paper runs will appear here for handoff awareness.</p>
      ) : (
        <div className="divide-y divide-border/40 rounded-lg border border-border/45 bg-background/35">
          {watchItems.map((item) => (
            <div key={item.key} className="flex items-start justify-between gap-3 px-3 py-2.5">
              <div>
                <p className="font-mono text-sm text-foreground/90">{item.name}</p>
                <p className="mt-0.5 text-[11px] uppercase tracking-[0.16em] text-foreground/40">{item.posture}</p>
                <p className="mt-1 text-xs text-foreground/55">{item.note}</p>
              </div>
              <p className={item.pnl >= 0 ? "font-mono text-sm font-semibold text-[var(--green)]" : "font-mono text-sm font-semibold text-[var(--red)]"}>
                {formatCurrency(item.pnl)}
              </p>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function InlineMetric({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "default" | "negative" | "warning";
}) {
  return (
    <div className="rounded-lg border border-border/45 bg-background/35 px-3 py-2">
      <p className="text-[10px] uppercase tracking-[0.16em] text-foreground/40">{label}</p>
      <p className={tone === "negative" ? "mt-1 font-mono text-sm font-semibold text-[var(--red)]" : tone === "warning" ? "mt-1 font-mono text-sm font-semibold text-amber-300" : "mt-1 font-mono text-sm font-semibold text-foreground"}>
        {value}
      </p>
    </div>
  );
}
