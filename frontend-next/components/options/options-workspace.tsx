"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { ChartStrip } from "@/components/options/chart-strip";
import { FloatingOrderTicket } from "@/components/options/floating-order-ticket";
import { NiftyImpactPanel } from "@/components/options/nifty-impact-panel";
import { OptionChainPanel } from "@/components/options/option-chain-panel";
import { OptionsHeader } from "@/components/options/options-header";
import { PositionsDock } from "@/components/options/positions-dock";
import { StrategyBuilderPanel } from "@/components/options/strategy-builder-panel";
import type { CandlePoint, ChartTimeframe, LivePosition, MiniChainSnapshot, NiftyImpactRow, OptionSessionSnapshot, RuntimeStatus, Underlying } from "@/components/options/types";
import { WorkspaceTabs } from "@/components/options/workspace-tabs";
import {
  ensureOptionsSessions,
  fetchCandles,
  fetchMiniChain,
  fetchNifty50Impact,
  fetchOptionSession,
  fetchRealtimePositions,
  fetchRuntimeStatus,
  loginToBroker,
} from "@/lib/options/api";

const INDEX_TOKENS: Record<Underlying, string> = {
  NIFTY: "256265",
  BANKNIFTY: "260105",
};

function createFallbackRuntimeStatus(): RuntimeStatus {
  return {
    brokerConnected: false,
    websocketStatus: "degraded",
    paperAvailable: true,
    appAuthenticated: false,
  };
}

function createFallbackMiniChain(underlying: Underlying): MiniChainSnapshot {
  const spotPrice = underlying === "BANKNIFTY" ? 48240 : 23460;
  const gap = underlying === "BANKNIFTY" ? 100 : 50;
  const atmStrike = Math.round(spotPrice / gap) * gap;
  const strikes = Array.from({ length: 11 }, (_, index) => {
    const strike = atmStrike + (index - 5) * gap;
    return {
      strike,
      isAtm: strike === atmStrike,
      ce: {
        instrumentToken: strike + 1,
        tradingSymbol: `${underlying}${strike}CE`,
        ltp: Math.max(12, 120 - Math.abs(strike - atmStrike) * 0.6),
        lotSize: underlying === "BANKNIFTY" ? 15 : 25,
        delta: Math.max(0.05, 0.52 - Math.abs(strike - atmStrike) / (gap * 12)),
        gamma: 0.03,
        theta: -2.1,
        vega: 5.5,
        iv: 14.8,
        oi: 1200 + index * 80,
      },
      pe: {
        instrumentToken: strike + 2,
        tradingSymbol: `${underlying}${strike}PE`,
        ltp: Math.max(12, 118 - Math.abs(strike - atmStrike) * 0.55),
        lotSize: underlying === "BANKNIFTY" ? 15 : 25,
        delta: -Math.max(0.05, 0.5 - Math.abs(strike - atmStrike) / (gap * 12)),
        gamma: 0.03,
        theta: -2.05,
        vega: 5.2,
        iv: 14.4,
        oi: 1100 + index * 75,
      },
    };
  });

  return {
    underlying,
    expiry: "2026-04-30",
    spotPrice,
    atmStrike,
    strikes,
  };
}

function createFallbackSession(underlying: Underlying): OptionSessionSnapshot {
  const mini = createFallbackMiniChain(underlying);
  return {
    underlying,
    spotLtp: mini.spotPrice,
    atmStrike: mini.atmStrike,
    expiries: [mini.expiry],
    rows: mini.strikes.map((row) => ({
      strike: row.strike,
      ce: row.ce
        ? {
            token: row.ce.instrumentToken,
            tsym: row.ce.tradingSymbol,
            ltp: row.ce.ltp,
            iv: row.ce.iv,
            oi: row.ce.oi ?? null,
            delta: row.ce.delta,
            gamma: row.ce.gamma,
            theta: row.ce.theta,
            vega: row.ce.vega,
          }
        : null,
      pe: row.pe
        ? {
            token: row.pe.instrumentToken,
            tsym: row.pe.tradingSymbol,
            ltp: row.pe.ltp,
            iv: row.pe.iv,
            oi: row.pe.oi ?? null,
            delta: row.pe.delta,
            gamma: row.pe.gamma,
            theta: row.pe.theta,
            vega: row.pe.vega,
          }
        : null,
      isAtm: row.isAtm,
    })),
  };
}

function createFallbackPositions(): LivePosition[] {
  return [
    {
      key: "nifty-short-call",
      tradingSymbol: "NIFTY 23500 CE",
      exchange: "NFO",
      product: "MIS",
      quantity: -25,
      averagePrice: 122.4,
      lastPrice: 108.6,
      pnl: 345,
      badge: "naked",
    },
    {
      key: "nifty-short-put",
      tradingSymbol: "NIFTY 23400 PE",
      exchange: "NFO",
      product: "MIS",
      quantity: -25,
      averagePrice: 114.2,
      lastPrice: 120.8,
      pnl: -165,
      badge: "unmanaged",
    },
  ];
}

function createFallbackImpact(): NiftyImpactRow[] {
  return [
    { symbol: "HDFCBANK", sector: "Banks", weight: 13.2, changePercent: 1.1, contribution: 0.15 },
    { symbol: "RELIANCE", sector: "Energy", weight: 8.7, changePercent: -0.45, contribution: -0.04 },
    { symbol: "ICICIBANK", sector: "Banks", weight: 8.2, changePercent: 0.62, contribution: 0.05 },
    { symbol: "TCS", sector: "IT", weight: 4.1, changePercent: -0.22, contribution: -0.01 },
  ];
}

export function OptionsWorkspace() {
  const [activeTab, setActiveTab] = useState<"chain" | "builder" | "impact">("builder");
  const [underlying, setUnderlying] = useState<Underlying>("NIFTY");
  const [chartHeight, setChartHeight] = useState(240);
  const [splitPercent, setSplitPercent] = useState(50);
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("15m");
  const [deltaFilter, setDeltaFilter] = useState(0.3);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>(createFallbackRuntimeStatus());
  const [loginPending, setLoginPending] = useState(false);
  const [expiries, setExpiries] = useState<string[]>(["2026-04-30"]);
  const [selectedExpiry, setSelectedExpiry] = useState("2026-04-30");
  const [chain, setChain] = useState<MiniChainSnapshot | null>(createFallbackMiniChain("NIFTY"));
  const [sessions, setSessions] = useState<Record<Underlying, OptionSessionSnapshot>>({
    NIFTY: createFallbackSession("NIFTY"),
    BANKNIFTY: createFallbackSession("BANKNIFTY"),
  });
  const [positions, setPositions] = useState<LivePosition[]>(createFallbackPositions());
  const [impactRows, setImpactRows] = useState<NiftyImpactRow[]>(createFallbackImpact());
  const [dockExpanded, setDockExpanded] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerStrike, setDrawerStrike] = useState<number | null>(null);
  const [drawerType, setDrawerType] = useState<"call" | "put">("call");
  const [drawerSide, setDrawerSide] = useState<"long" | "short">("long");
  const [drawerVersion, setDrawerVersion] = useState(0);
  const [chainLoading, setChainLoading] = useState(false);
  const [chartLoading, setChartLoading] = useState(false);
  const [chartCandles, setChartCandles] = useState<Record<Underlying, CandlePoint[]>>({ NIFTY: [], BANKNIFTY: [] });

  useEffect(() => {
    if (!runtimeStatus.appAuthenticated) {
      return;
    }
    void ensureOptionsSessions().catch(() => undefined);
  }, [runtimeStatus.appAuthenticated]);

  useEffect(() => {
    let disposed = false;

    async function loadStaticPanels() {
      const [statusResult, impactResult, positionsResult] = await Promise.allSettled([
        fetchRuntimeStatus(),
        runtimeStatus.appAuthenticated ? fetchNifty50Impact() : Promise.resolve(createFallbackImpact()),
        runtimeStatus.appAuthenticated ? fetchRealtimePositions() : Promise.resolve(createFallbackPositions()),
      ]);

      if (disposed) {
        return;
      }

      if (statusResult.status === "fulfilled") {
        setRuntimeStatus(statusResult.value);
      }
      if (impactResult.status === "fulfilled" && impactResult.value.length > 0) {
        setImpactRows(impactResult.value);
      }
      if (positionsResult.status === "fulfilled" && positionsResult.value.length > 0) {
        setPositions(positionsResult.value);
      }
    }

    void loadStaticPanels();
    const interval = window.setInterval(loadStaticPanels, 15000);
    return () => {
      disposed = true;
      window.clearInterval(interval);
    };
  }, [runtimeStatus.appAuthenticated]);

  useEffect(() => {
    let disposed = false;

    async function loadUnderlying() {
      if (!runtimeStatus.appAuthenticated) {
        setChain(createFallbackMiniChain(underlying));
        return;
      }
      setChainLoading(true);
      try {
        await ensureOptionsSessions();
        const sessionSnapshot = await fetchOptionSession(underlying);

        if (disposed) {
          return;
        }

        setSessions((current) => ({ ...current, [underlying]: sessionSnapshot }));
        setExpiries(sessionSnapshot.expiries.length > 0 ? sessionSnapshot.expiries : [createFallbackMiniChain(underlying).expiry]);
        const expiryToUse = sessionSnapshot.expiries[0] ?? createFallbackMiniChain(underlying).expiry;
        setSelectedExpiry(expiryToUse);

        try {
          const mini = await fetchMiniChain(underlying, expiryToUse, sessionSnapshot.atmStrike ?? undefined);
          if (!disposed) {
            setChain(mini);
          }
        } catch {
          if (!disposed) {
            setChain(createFallbackMiniChain(underlying));
          }
        }
      } catch {
        if (!disposed) {
          setExpiries([createFallbackMiniChain(underlying).expiry]);
          setSelectedExpiry(createFallbackMiniChain(underlying).expiry);
          setChain(createFallbackMiniChain(underlying));
        }
      } finally {
        if (!disposed) {
          setChainLoading(false);
        }
      }
    }

    void loadUnderlying();
    return () => {
      disposed = true;
    };
  }, [runtimeStatus.appAuthenticated, underlying]);

  useEffect(() => {
    let disposed = false;
    async function refreshMiniChain() {
      if (!runtimeStatus.appAuthenticated) {
        return;
      }
      if (!selectedExpiry) {
        return;
      }
      try {
        const next = await fetchMiniChain(underlying, selectedExpiry);
        if (!disposed) {
          setChain(next);
        }
      } catch {
        if (!disposed) {
          setChain((current) => current ?? createFallbackMiniChain(underlying));
        }
      }
    }

    void refreshMiniChain();
    return () => {
      disposed = true;
    };
  }, [runtimeStatus.appAuthenticated, selectedExpiry, underlying]);

  useEffect(() => {
    let disposed = false;

    async function loadCharts() {
      if (!runtimeStatus.appAuthenticated) {
        return;
      }
      setChartLoading(true);
      try {
        const toIso = new Date().toISOString();
        const fromDate = new Date();
        fromDate.setMonth(fromDate.getMonth() - 3);
        const fromIso = fromDate.toISOString();
        const [nifty, banknifty] = await Promise.all([
          fetchCandles({ identifier: INDEX_TOKENS.NIFTY, timeframe, fromIso, toIso }),
          fetchCandles({ identifier: INDEX_TOKENS.BANKNIFTY, timeframe, fromIso, toIso }),
        ]);
        if (!disposed) {
          setChartCandles({ NIFTY: nifty, BANKNIFTY: banknifty });
        }
      } catch {
        if (!disposed) {
          toast.error("Unable to load live candles. Check auth/session and candles API.");
        }
      } finally {
        if (!disposed) {
          setChartLoading(false);
        }
      }
    }

    void loadCharts();
    const interval = window.setInterval(loadCharts, 15000);
    return () => {
      disposed = true;
      window.clearInterval(interval);
    };
  }, [runtimeStatus.appAuthenticated, timeframe]);

  const primarySession = sessions.NIFTY;
  const secondarySession = sessions.BANKNIFTY;

  const primaryPrice = useMemo(() => {
    const candles = chartCandles.NIFTY;
    return candles.length > 0 ? candles[candles.length - 1]?.close ?? null : runtimeStatus.appAuthenticated ? primarySession?.spotLtp ?? null : null;
  }, [chartCandles.NIFTY, primarySession?.spotLtp, runtimeStatus.appAuthenticated]);
  const secondaryPrice = useMemo(() => {
    const candles = chartCandles.BANKNIFTY;
    return candles.length > 0 ? candles[candles.length - 1]?.close ?? null : runtimeStatus.appAuthenticated ? secondarySession?.spotLtp ?? null : null;
  }, [chartCandles.BANKNIFTY, runtimeStatus.appAuthenticated, secondarySession?.spotLtp]);
  const primaryChange = useMemo(() => {
    const candles = chartCandles.NIFTY;
    if (candles.length < 2) return null;
    const previous = candles[candles.length - 2]?.close ?? candles[0]?.close;
    const latest = candles[candles.length - 1]?.close;
    return previous ? ((latest - previous) / previous) * 100 : null;
  }, [chartCandles.NIFTY]);
  const secondaryChange = useMemo(() => {
    const candles = chartCandles.BANKNIFTY;
    if (candles.length < 2) return null;
    const previous = candles[candles.length - 2]?.close ?? candles[0]?.close;
    const latest = candles[candles.length - 1]?.close;
    return previous ? ((latest - previous) / previous) * 100 : null;
  }, [chartCandles.BANKNIFTY]);

  async function handleBrokerLogin() {
    if (!runtimeStatus.appAuthenticated) {
      toast.error("App login required before broker login");
      return;
    }
    setLoginPending(true);
    try {
      const response = await loginToBroker();
      toast.success(response.authenticated ? "Broker session refreshed" : "Broker login request sent");
    } catch {
      toast.error("Broker login failed");
    } finally {
      setLoginPending(false);
    }
  }

  return (
    <div className="flex h-[calc(100vh-5.5rem)] min-h-[48rem] flex-col gap-2 pb-2">
      <OptionsHeader status={runtimeStatus} onBrokerLogin={handleBrokerLogin} loginPending={loginPending} />

      <div className="flex items-center gap-2 rounded-2xl border border-[var(--border)] bg-[var(--panel)] px-3 py-2">
        {(["NIFTY", "BANKNIFTY"] as Underlying[]).map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setUnderlying(item)}
            className={`rounded-md border px-3 py-1.5 text-[11px] uppercase tracking-[0.16em] ${underlying === item ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]" : "border-[var(--border)] text-[var(--muted)]"}`}
          >
            {item}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-[var(--muted)]">Sessions are auto-started and monitored in the background.</span>
      </div>

      {!runtimeStatus.appAuthenticated ? (
        <div className="rounded-2xl border border-[var(--yellow)]/30 bg-[var(--yellow)]/10 px-4 py-3 text-[12px] text-[var(--muted)]">
          App login is required for real chain data, historical candles, dry-runs, and paper execution. Sign in from the dashboard first.
        </div>
      ) : null}

      <ChartStrip
        chartHeight={chartHeight}
        splitPercent={splitPercent}
        timeframe={timeframe}
        onChartHeightChange={setChartHeight}
        onSplitPercentChange={setSplitPercent}
        onTimeframeChange={setTimeframe}
        primary={{ label: "NIFTY", price: primaryPrice, changePercent: primaryChange, candles: chartCandles.NIFTY, loading: chartLoading }}
        secondary={{ label: "BANKNIFTY", price: secondaryPrice, changePercent: secondaryChange, candles: chartCandles.BANKNIFTY, loading: chartLoading }}
      />

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--bg)]/60">
        <WorkspaceTabs activeTab={activeTab} onTabChange={setActiveTab} />
        <div className="relative flex-1 overflow-hidden p-2">
          {activeTab === "chain" ? (
            <OptionChainPanel
              underlying={underlying}
              expiry={selectedExpiry}
              expiries={expiries}
              onExpiryChange={setSelectedExpiry}
              deltaFilter={deltaFilter}
              onDeltaFilterChange={setDeltaFilter}
              chain={chain}
              loading={chainLoading}
              onQuickOrder={({ strike, optionType, side }) => {
                setDrawerStrike(strike);
                setDrawerType(optionType);
                setDrawerSide(side);
                setDrawerVersion((value) => value + 1);
                setDrawerOpen(true);
              }}
            />
          ) : null}

          {activeTab === "builder" ? (
            <StrategyBuilderPanel
              underlying={underlying}
              expiry={selectedExpiry}
              currentSpot={chain?.spotPrice ?? primaryPrice ?? 0}
              deltaFilter={deltaFilter}
              chain={chain}
              appAuthenticated={runtimeStatus.appAuthenticated}
              paperAvailable={runtimeStatus.paperAvailable}
            />
          ) : null}

          {activeTab === "impact" ? <NiftyImpactPanel rows={impactRows} /> : null}

          <FloatingOrderTicket key={`${drawerVersion}-${drawerType}-${drawerStrike ?? "none"}`} open={drawerOpen} initialStrike={drawerStrike} initialOptionType={drawerType} initialSide={drawerSide} onClose={() => setDrawerOpen(false)} />
        </div>
      </div>

      <PositionsDock positions={positions} expanded={dockExpanded} onToggle={() => setDockExpanded((value) => !value)} />
    </div>
  );
}
