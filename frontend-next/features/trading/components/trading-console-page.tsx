"use client";

import { useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";
import { Activity, Cpu, Radio, Wifi, WifiOff, AlertTriangle } from "lucide-react";

import { StatusBadge } from "@/components/operator/status-badge";
import { ApiClientError } from "@/lib/api/client";
import type { ControlStrategyGroup, TradingConsoleSnapshot, TradingStrategyGroup } from "@/features/trading/types";
import { exitPaperStrategy } from "@/features/trading/api";
import { MarketQuoteStrip } from "./market-quote-strip";
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

function pnlClass(value: number) {
  return value >= 0
    ? "font-mono font-semibold text-[var(--green)]"
    : "font-mono font-semibold text-[var(--red)]";
}

// ─── Runtime status indicators ───────────────────────────────────────────────

function RuntimeStatusRail({ runtime }: { runtime: TradingConsoleSnapshot["runtime"] }) {
  const brokerOk = runtime.brokerStatus === "connected";
  const brokerWarn = runtime.brokerStatus === "reconnecting" || runtime.brokerStatus === "degraded";
  const wsOk = runtime.websocketStatus === "connected" || runtime.websocketStatus === "active";

  return (
    <div className="flex items-center gap-3 text-[11px]">
      <span
        className={
          brokerOk
            ? "flex items-center gap-1 text-emerald-400"
            : brokerWarn
              ? "flex items-center gap-1 text-amber-400"
              : "flex items-center gap-1 text-rose-400"
        }
      >
        <Radio className="h-3 w-3" />
        <span className="uppercase tracking-wider">{runtime.brokerStatus}</span>
      </span>
      <span
        className={
          wsOk
            ? "flex items-center gap-1 text-emerald-400"
            : "flex items-center gap-1 text-foreground/40"
        }
      >
        {wsOk ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
        <span className="uppercase tracking-wider">ws</span>
      </span>
      <span
        className={
          runtime.paperAvailable
            ? "flex items-center gap-1 text-foreground/45"
            : "flex items-center gap-1 text-amber-400"
        }
      >
        <Cpu className="h-3 w-3" />
        <span className="uppercase tracking-wider">paper {runtime.paperAvailable ? "ready" : "offline"}</span>
      </span>
    </div>
  );
}

// ─── Compact sidebar section ──────────────────────────────────────────────────

function SidebarSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-2 text-[10px] uppercase tracking-[0.28em] text-foreground/35">{label}</p>
      {children}
    </div>
  );
}

// ─── Live unattributed exposure ───────────────────────────────────────────────

function UnattributedExposure({ unattributed }: { unattributed: TradingConsoleSnapshot["control"] extends null ? never : NonNullable<TradingConsoleSnapshot["control"]>["unattributed"] }) {
  if (unattributed.positions.length === 0) return null;
  return (
    <div className="rounded-lg border border-dashed border-border/50 bg-background/25 px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <AlertTriangle className="h-3 w-3 text-amber-400" />
          <span className="text-[11px] font-medium uppercase tracking-wider text-amber-400/80">Unattributed exposure</span>
        </div>
        <span className={`text-xs ${pnlClass(unattributed.netPnl)}`}>{formatCurrency(unattributed.netPnl)}</span>
      </div>
      <div className="mt-2 space-y-0.5">
        {unattributed.positions.map((pos, i) => (
          <p key={i} className="font-mono text-[11px] text-foreground/50">
            {String(pos.tradingsymbol ?? pos.position_key ?? "unknown")} · qty {String(pos.quantity ?? pos.net_quantity ?? "—")}
          </p>
        ))}
      </div>
    </div>
  );
}

// ─── Paper account summary banner ────────────────────────────────────────────

function PaperAccountBanner({
  paper,
  openLegCount,
}: {
  paper: TradingConsoleSnapshot["paper"];
  openLegCount: number;
}) {
  const net = paper.account.realizedPnl + paper.account.unrealizedPnl;
  return (
    <section
      aria-label="Paper account"
      className="rounded-[1.1rem] border border-border/55 bg-card/40 px-4 py-3"
    >
      <h2 className="mb-2.5 text-[10px] uppercase tracking-[0.28em] text-foreground/40">Paper account</h2>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div>
          <p className="text-[10px] uppercase tracking-wider text-foreground/35">Net P&L</p>
          <p className={`mt-1 text-base ${pnlClass(net)}`}>{formatCurrency(net)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wider text-foreground/35">Realized</p>
          <p className={`mt-1 text-base ${pnlClass(paper.account.realizedPnl)}`}>{formatCurrency(paper.account.realizedPnl)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wider text-foreground/35">Available funds</p>
          <p className="mt-1 font-mono text-base font-semibold text-foreground">
            {paper.account.availableFunds.toLocaleString("en-IN", { maximumFractionDigits: 0 })}
          </p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wider text-foreground/35">Open legs</p>
          <p className="mt-1 font-mono text-base font-semibold text-foreground">{openLegCount}</p>
        </div>
      </div>
    </section>
  );
}

// ─── Live execution summary bar ───────────────────────────────────────────────

function LiveSummaryBar({ control }: { control: NonNullable<TradingConsoleSnapshot["control"]> | null }) {
  if (!control) {
    return (
      <div className="rounded-[1.1rem] border border-border/50 bg-card/30 px-4 py-3">
        <p className="text-sm text-foreground/40">Control plane loading…</p>
      </div>
    );
  }

  const { totals } = control;
  return (
    <div className="grid grid-cols-3 gap-3 rounded-[1.1rem] border border-border/50 bg-card/35 px-4 py-3">
      <div>
        <p className="text-[10px] uppercase tracking-wider text-foreground/35">Open strategies</p>
        <p className="mt-1 font-mono text-base font-semibold text-foreground">{totals.openStrategyCount}</p>
        <p className="text-[10px] text-foreground/40">{totals.positionCount} positions tracked</p>
      </div>
      <div>
        <p className="text-[10px] uppercase tracking-wider text-foreground/35">Net P&L</p>
        <p className={`mt-1 text-base ${pnlClass(totals.netPnl)}`}>{formatCurrency(totals.netPnl)}</p>
        <p className="text-[10px] text-foreground/40">{formatCurrency(totals.realizedPnl)} realized</p>
      </div>
      <div>
        <p className="text-[10px] uppercase tracking-wider text-foreground/35">Workers</p>
        <p className={`mt-1 font-mono text-base font-semibold ${totals.staleWorkerCount > 0 ? "text-amber-300" : "text-foreground"}`}>
          {totals.staleWorkerCount > 0 ? `${totals.staleWorkerCount} stale` : "healthy"}
        </p>
        <p className="text-[10px] text-foreground/40">{totals.strategyCount} total tracked</p>
      </div>
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export function TradingConsolePage({ snapshot }: TradingConsolePageProps) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [exitingStrategyId, setExitingStrategyId] = useState<string | null>(null);

  const selectedMode = getSelectedMode(searchParams.get("mode"));
  const tabRefs = useRef<Record<"live" | "paper", HTMLButtonElement | null>>({
    live: null,
    paper: null,
  });

  const paperSections = useMemo(() => splitPaperStrategies(snapshot.paper.strategies), [snapshot.paper.strategies]);
  const liveSections = useMemo(() => splitLiveStrategies(snapshot.control?.strategies ?? []), [snapshot.control?.strategies]);
  const hiddenDryRunCount = paperSections.hiddenDryRunCount + liveSections.hiddenDryRunCount;
  const openPaperLegCount = paperSections.active.reduce((sum, strategy) => sum + strategy.openLegCount, 0);

  function moveModeFocus(currentMode: "live" | "paper", direction: "next" | "prev") {
    const targetMode = currentMode === "live"
      ? direction === "next" ? "paper" : "paper"
      : direction === "next" ? "live" : "live";
    setMode(targetMode);
    tabRefs.current[targetMode]?.focus();
  }

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
      const message =
        err instanceof ApiClientError
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
    <div className="space-y-4 pb-6">
      {/* ── Command header ──────────────────────────────────────────────────── */}
      <header className="rounded-[1.35rem] border border-border/60 bg-card/55 px-5 py-4 shadow-[0_18px_40px_rgba(0,0,0,0.12)] backdrop-blur">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          {/* Identity */}
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border/60 bg-background/50">
              <Activity className="h-4 w-4 text-foreground/60" />
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-[0.28em] text-foreground/35">Strategies workspace</p>
              <h1 className="text-base font-semibold tracking-tight text-foreground">Strategies</h1>
            </div>
          </div>

          {/* Mode switcher + runtime rail */}
          <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center sm:gap-4">
            <RuntimeStatusRail runtime={snapshot.runtime} />

            <div
              className="flex items-center gap-0 rounded-full border border-border/60 bg-background/40 p-0.5"
              role="tablist"
              aria-label="Strategy modes"
            >
              <button
                type="button"
                role="tab"
                aria-selected={selectedMode === "live"}
                aria-controls="strategies-panel-live"
                id="strategies-tab-live"
                tabIndex={selectedMode === "live" ? 0 : -1}
                ref={(node) => {
                  tabRefs.current.live = node;
                }}
                onClick={() => setMode("live")}
                onKeyDown={(event) => {
                  if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                    event.preventDefault();
                    moveModeFocus("live", "next");
                  } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                    event.preventDefault();
                    moveModeFocus("live", "prev");
                  }
                }}
                className={`rounded-full px-4 py-1.5 text-[11px] font-semibold uppercase tracking-[0.22em] transition-all ${
                  selectedMode === "live"
                    ? "bg-primary/15 text-primary shadow-sm"
                    : "text-foreground/55 hover:text-foreground/80"
                }`}
              >
                Live
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={selectedMode === "paper"}
                aria-controls="strategies-panel-paper"
                id="strategies-tab-paper"
                tabIndex={selectedMode === "paper" ? 0 : -1}
                ref={(node) => {
                  tabRefs.current.paper = node;
                }}
                onClick={() => setMode("paper")}
                onKeyDown={(event) => {
                  if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                    event.preventDefault();
                    moveModeFocus("paper", "next");
                  } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                    event.preventDefault();
                    moveModeFocus("paper", "prev");
                  }
                }}
                className={`rounded-full px-4 py-1.5 text-[11px] font-semibold uppercase tracking-[0.22em] transition-all ${
                  selectedMode === "paper"
                    ? "bg-card/80 text-foreground/80 shadow-sm"
                    : "text-foreground/55 hover:text-foreground/80"
                }`}
              >
                Paper
              </button>
            </div>

            {selectedMode === "live" ? (
              <StatusBadge tone="warning">live execution</StatusBadge>
            ) : (
              <StatusBadge tone="neutral">paper simulation</StatusBadge>
            )}
          </div>
        </div>

        {/* Market quotes — compact, below the header row */}
        {snapshot.quotes.length > 0 && (
          <div className="mt-3 border-t border-border/35 pt-3">
            <MarketQuoteStrip quotes={snapshot.quotes} compact className="flex-wrap gap-2" />
          </div>
        )}

        {/* Dry-run notice */}
        {hiddenDryRunCount > 0 && (
          <p className="mt-2 text-[11px] text-foreground/45">
            {hiddenDryRunCount} dry-run records hidden from operator execution sections.
          </p>
        )}
      </header>

      {/* ── Execution body ───────────────────────────────────────────────────── */}
      <div className="grid gap-4 xl:grid-cols-[1fr_300px]">
        {/* Main execution column */}
        <main className="min-w-0 space-y-4">
          {selectedMode === "live" ? (
            <section
              id="strategies-panel-live"
              role="tabpanel"
              aria-labelledby="strategies-tab-live"
              className="space-y-4"
            >
              <LiveSummaryBar control={snapshot.control ?? null} />

              <LiveStrategySection
                title="Active strategies"
                strategies={liveSections.active}
                emptyCopy="No active live strategies attributed to the control plane."
              />

              {snapshot.control && (
                <UnattributedExposure unattributed={snapshot.control.unattributed} />
              )}

              {liveSections.recent.length > 0 && (
                <LiveStrategySection
                  title="Recent strategies"
                  strategies={liveSections.recent}
                  emptyCopy="No recent closed live strategies."
                  testId="live-recent-strategies-panel"
                />
              )}
            </section>
          ) : (
            <section
              id="strategies-panel-paper"
              role="tabpanel"
              aria-labelledby="strategies-tab-paper"
              className="space-y-4"
            >
              <PaperAccountBanner paper={snapshot.paper} openLegCount={openPaperLegCount} />

              <StrategyGroupsPanel
                title="Active strategies"
                eyebrow="paper"
                testId="strategy-groups-panel"
                strategies={paperSections.active}
                emptyCopy="No active paper strategies. Paper runs will appear here once entered."
                renderActions={(strategy) => (
                  <button
                    type="button"
                    onClick={() => void handleExitPaperStrategy(strategy)}
                    disabled={
                      !strategy.isOpen ||
                      !strategy.capabilities.canExitStrategy ||
                      exitingStrategyId === strategy.strategyRunId
                    }
                    title={
                      !strategy.capabilities.canExitStrategy
                        ? (strategy.capabilities.exitReason ?? "Strategy exit unavailable")
                        : undefined
                    }
                    className="rounded-md border border-rose-400/30 bg-rose-400/10 px-2 py-1 text-[10px] font-medium uppercase tracking-[0.18em] text-rose-300 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {exitingStrategyId === strategy.strategyRunId ? "Exiting…" : "Exit"}
                  </button>
                )}
              />

              {paperSections.recent.length > 0 && (
                <StrategyGroupsPanel
                  title="Recent strategies"
                  eyebrow="paper"
                  testId="strategy-groups-recent-panel"
                  strategies={paperSections.recent}
                  emptyCopy="No recent closed paper strategies."
                />
              )}
            </section>
          )}
        </main>

        {/* Context rail — subordinate, demoted treatment */}
        <aside className="space-y-4 xl:space-y-3">
          <SidebarSection label="Runtime">
            <div className="space-y-1.5 rounded-xl border border-border/50 bg-card/40 px-3 py-3">
              <RuntimeRow label="Broker" value={snapshot.runtime.brokerStatus}
                tone={snapshot.runtime.brokerStatus === "connected" ? "positive" : snapshot.runtime.brokerStatus === "reconnecting" || snapshot.runtime.brokerStatus === "degraded" ? "warning" : "danger"}
              />
              <RuntimeRow label="WebSocket" value={snapshot.runtime.websocketStatus}
                tone={snapshot.runtime.websocketStatus === "connected" || snapshot.runtime.websocketStatus === "active" ? "positive" : "neutral"}
              />
              <RuntimeRow label="Paper engine" value={snapshot.runtime.paperAvailable ? "available" : "offline"}
                tone={snapshot.runtime.paperAvailable ? "positive" : "neutral"}
              />
            </div>
          </SidebarSection>

          <SidebarSection label="Broker positions">
            <BrokerPositionsPanel broker={snapshot.broker} />
          </SidebarSection>
        </aside>
      </div>
    </div>
  );
}

// ─── Runtime row (for sidebar) ────────────────────────────────────────────────

function RuntimeRow({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "positive" | "warning" | "danger" | "neutral";
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-foreground/45">{label}</span>
      <StatusBadge tone={tone}>{value}</StatusBadge>
    </div>
  );
}

// ─── Live strategy section ────────────────────────────────────────────────────

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
    <section
      className="rounded-[1.35rem] border border-border/70 bg-card/80 p-5 shadow-[0_18px_40px_rgba(0,0,0,0.18)] backdrop-blur"
      data-testid={testId}
    >
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-[0.28em] text-foreground/40">live</p>
          <h3 className="mt-2 text-lg font-semibold tracking-tight text-foreground">{title}</h3>
        </div>
        <StatusBadge tone={strategies.length > 0 ? "positive" : "neutral"}>{strategies.length}</StatusBadge>
      </div>

      {strategies.length === 0 ? (
        <p className="py-3 text-sm text-foreground/40">{emptyCopy}</p>
      ) : null}

      <div className="space-y-3">
        {strategies.map((strategy) => (
          <LiveStrategyRow key={strategy.strategyRunId} strategy={strategy} />
        ))}
      </div>
    </section>
  );
}

function LiveStrategyRow({ strategy }: { strategy: ControlStrategyGroup }) {
  const healthTone =
    strategy.healthStatus === "healthy"
      ? ("positive" as const)
      : strategy.healthStatus === "stale"
        ? ("warning" as const)
        : strategy.healthStatus === "disconnected"
          ? ("danger" as const)
          : ("neutral" as const);

  const protectionTone =
    strategy.protection.status === "active"
      ? ("positive" as const)
      : strategy.protection.status === "error" || strategy.protection.status === "triggered"
        ? ("danger" as const)
        : strategy.protection.status === "pending_exit" || strategy.protection.status === "stale"
          ? ("warning" as const)
          : ("neutral" as const);

  return (
    <div className="rounded-xl border border-border/50 bg-background/50 px-4 py-3">
      {/* Header row */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-foreground/90">{strategy.displayName}</span>
            <StatusBadge tone={strategy.isOpen ? "positive" : "neutral"}>
              {strategy.isOpen ? "open" : "closed"}
            </StatusBadge>
            <StatusBadge tone={healthTone}>{strategy.healthStatus}</StatusBadge>
          </div>
          <p className="font-mono text-[11px] text-foreground/40">
            run {strategy.strategyRunId}
            {strategy.workerName ? ` · ${strategy.workerName}` : ""}
            {strategy.heartbeatAgeSec != null ? ` · hb ${strategy.heartbeatAgeSec}s` : ""}
          </p>
        </div>

        <ControlStrategyActions strategy={strategy} />
      </div>

      {/* P&L row */}
      <div className="mt-2.5 flex flex-wrap items-center gap-4 border-t border-border/30 pt-2.5 text-xs">
        <span className="text-foreground/45">
          R{" "}
          <span className={strategy.realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>
            {formatCurrency(strategy.realizedPnl)}
          </span>
        </span>
        <span className="text-foreground/45">
          U{" "}
          <span className={strategy.unrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>
            {formatCurrency(strategy.unrealizedPnl)}
          </span>
        </span>
        <span className="ml-auto">
          Net{" "}
          <span className={`font-semibold ${strategy.netPnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
            {formatCurrency(strategy.netPnl)}
          </span>
        </span>
      </div>

      {/* Protection */}
      <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-border/35 bg-background/30 px-3 py-2 text-[11px]">
        <StatusBadge tone={protectionTone}>{strategy.protection.source} · {strategy.protection.status}</StatusBadge>
        <span className="text-foreground/45">{strategy.protection.summary}</span>
      </div>

      {/* Cancel order reason when blocked */}
      {!strategy.allowedActions.includes("cancel_orders") && strategy.actionReasons.cancel_orders ? (
        <p className="mt-2 text-[11px] text-foreground/35">{strategy.actionReasons.cancel_orders}</p>
      ) : null}
    </div>
  );
}
