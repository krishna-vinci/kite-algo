"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { FloatingOrderTicket } from "@/components/options/floating-order-ticket";
import { NiftyImpactPanel } from "@/components/options/nifty-impact-panel";
import { OptionChainPanel } from "@/components/options/option-chain-panel";
import { OptionsHeader } from "@/components/options/options-header";
import { PositionsDock } from "@/components/options/positions-dock";
import { StrategyBuilderPanel } from "@/components/options/strategy-builder-panel";
import type { LivePosition, MiniChainSnapshot, NiftyImpactRow, OptionSessionSnapshot, RuntimeStatus, Underlying } from "@/components/options/types";
import { WorkspaceTabs } from "@/components/options/workspace-tabs";
import {
  buildOptionsSessionSseUrl,
  ensureOptionsSessions,
  fetchNifty50Impact,
  fetchOptionSession,
  fetchRealtimePositions,
  fetchRuntimeStatus,
  loginToBroker,
  mergeOptionSessionSnapshot,
  normalizeOptionSessionSnapshot,
} from "@/lib/options/api";

const INDEX_TOKENS: Record<Underlying, string> = {
  NIFTY: "256265",
  BANKNIFTY: "260105",
};

function createFallbackRuntimeStatus(): RuntimeStatus {
  return {
    brokerConnected: false,
    brokerStatus: "unknown",
    brokerMode: "system",
    brokerLastSuccessAt: null,
    brokerLastFailureAt: null,
    brokerLastError: null,
    brokerNextRefreshAt: null,
    websocketStatus: "degraded",
    paperAvailable: true,
    appAuthenticated: false,
  };
}

function createFallbackMiniChain(underlying: Underlying, expiry = "2026-04-30"): MiniChainSnapshot {
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
    expiry,
    spotPrice,
    atmStrike,
    strikes,
  };
}

function createFallbackSession(underlying: Underlying): OptionSessionSnapshot {
  const mini = createFallbackMiniChain(underlying);
  const rows = mini.strikes.map((row) => ({
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
  }));
  return {
    underlying,
    spotLtp: mini.spotPrice,
    atmStrike: mini.atmStrike,
    expiries: [mini.expiry],
    perExpiry: {
      [mini.expiry]: {
        forward: mini.spotPrice,
        sigmaExpiry: null,
        atmStrike: mini.atmStrike,
        strikes: mini.strikes.map((row) => row.strike),
        rows,
      },
    },
    rows,
    updatedAt: null,
  };
}

function toMiniChainSnapshot(session: OptionSessionSnapshot | null | undefined, expiry: string | null): MiniChainSnapshot | null {
  if (!session || !expiry) {
    return null;
  }
  const expiryData = session.perExpiry[expiry];
  if (!expiryData) {
    return null;
  }
  return {
    underlying: session.underlying,
    expiry,
    spotPrice: session.spotLtp ?? 0,
    atmStrike: expiryData.atmStrike ?? session.atmStrike ?? 0,
    strikes: expiryData.rows.map((row) => ({
      strike: row.strike,
      isAtm: Boolean(row.isAtm),
      ce: row.ce
        ? {
            instrumentToken: row.ce.token,
            tradingSymbol: row.ce.tsym,
            ltp: row.ce.ltp ?? 0,
            lotSize: row.ce.lotSize ?? (session.underlying === "BANKNIFTY" ? 15 : 25),
            delta: row.ce.delta ?? 0,
            gamma: row.ce.gamma ?? 0,
            theta: row.ce.theta ?? 0,
            vega: row.ce.vega ?? 0,
            iv: row.ce.iv ?? 0,
            oi: row.ce.oi ?? undefined,
          }
        : null,
      pe: row.pe
        ? {
            instrumentToken: row.pe.token,
            tradingSymbol: row.pe.tsym,
            ltp: row.pe.ltp ?? 0,
            lotSize: row.pe.lotSize ?? (session.underlying === "BANKNIFTY" ? 15 : 25),
            delta: row.pe.delta ?? 0,
            gamma: row.pe.gamma ?? 0,
            theta: row.pe.theta ?? 0,
            vega: row.pe.vega ?? 0,
            iv: row.pe.iv ?? 0,
            oi: row.pe.oi ?? undefined,
          }
        : null,
    })),
  };
}

function readForwardPrice(session: OptionSessionSnapshot | null | undefined) {
  if (!session) {
    return null;
  }
  const firstExpiry = session.expiries[0];
  return firstExpiry ? session.perExpiry[firstExpiry]?.forward ?? null : null;
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

function SpotTicker({ label, price, forwardPrice }: Readonly<{ label: string; price: number | null; forwardPrice: number | null }>) {
  const basis = price !== null && forwardPrice !== null ? forwardPrice - price : null;
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--dim)]">{label}</span>
      <span className="font-mono text-xs text-[var(--text)]">
        {price === null ? "—" : price.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
      </span>
      {forwardPrice !== null && (
        <>
          <span className="text-[9px] uppercase text-[var(--dim)]">f</span>
          <span className="font-mono text-[11px] text-[var(--blue)]">
            {forwardPrice.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
          </span>
        </>
      )}
      {basis !== null && (
        <span className="font-mono text-[10px] text-[var(--muted)]">
          {basis >= 0 ? "+" : ""}{basis.toFixed(1)}
        </span>
      )}
    </div>
  );
}

export function OptionsWorkspace() {
  const [activeTab, setActiveTab] = useState<"chain" | "builder" | "impact">("builder");
  const [underlying, setUnderlying] = useState<Underlying>("NIFTY");
  const [deltaFilter, setDeltaFilter] = useState(0.3);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>(createFallbackRuntimeStatus());
  const [loginPending, setLoginPending] = useState(false);
  const [expiries, setExpiries] = useState<string[]>(["2026-04-30"]);
  const [selectedExpiry, setSelectedExpiry] = useState("2026-04-30");
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
    if (!runtimeStatus.appAuthenticated) {
      return;
    }

    let disposed = false;
    const streams: EventSource[] = [];

    async function primeSessions() {
      try {
        await ensureOptionsSessions();
        const results = await Promise.allSettled((Object.keys(INDEX_TOKENS) as Underlying[]).map((item) => fetchOptionSession(item)));
        if (disposed) {
          return;
        }
        setSessions((current) => {
          const next = { ...current };
          results.forEach((result, index) => {
            if (result.status === "fulfilled") {
              const key = (Object.keys(INDEX_TOKENS) as Underlying[])[index];
              next[key] = mergeOptionSessionSnapshot(current[key], result.value);
            }
          });
          return next;
        });
      } catch {
        if (!disposed) {
          toast.error("Unable to load live option snapshots.");
        }
      }
    }

    void primeSessions();

    for (const item of Object.keys(INDEX_TOKENS) as Underlying[]) {
      const source = new EventSource(buildOptionsSessionSseUrl(item), { withCredentials: true });
      source.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as { type?: string } & Record<string, unknown>;
          if (payload.type === "error") {
            return;
          }
          const snapshot = normalizeOptionSessionSnapshot(payload as never);
          if (!disposed) {
            setSessions((current) => ({ ...current, [item]: mergeOptionSessionSnapshot(current[item], snapshot) }));
          }
        } catch {
          // ignore malformed keep-alive payloads
        }
      };
      streams.push(source);
    }

    return () => {
      disposed = true;
      streams.forEach((source) => source.close());
    };
  }, [runtimeStatus.appAuthenticated]);

  useEffect(() => {
    const session = sessions[underlying];
    const fallbackExpiry = createFallbackMiniChain(underlying).expiry;
    const nextExpiries = session?.expiries?.length ? session.expiries : [fallbackExpiry];
    setExpiries(nextExpiries);
    setSelectedExpiry((current) => (nextExpiries.includes(current) ? current : nextExpiries[0] ?? fallbackExpiry));
  }, [sessions, underlying]);

  const primarySession = sessions.NIFTY;
  const secondarySession = sessions.BANKNIFTY;
  const chain = useMemo(() => {
    const liveChain = toMiniChainSnapshot(sessions[underlying], selectedExpiry);
    if (liveChain) {
      return liveChain;
    }
    return createFallbackMiniChain(underlying, selectedExpiry || undefined);
  }, [selectedExpiry, sessions, underlying]);
  const chainLoading = runtimeStatus.appAuthenticated && !toMiniChainSnapshot(sessions[underlying], selectedExpiry);
  const primaryForward = useMemo(() => (runtimeStatus.appAuthenticated ? readForwardPrice(primarySession) : null), [primarySession, runtimeStatus.appAuthenticated]);
  const secondaryForward = useMemo(() => (runtimeStatus.appAuthenticated ? readForwardPrice(secondarySession) : null), [secondarySession, runtimeStatus.appAuthenticated]);

  const primaryPrice = useMemo(() => (runtimeStatus.appAuthenticated ? primarySession?.spotLtp ?? null : null), [primarySession?.spotLtp, runtimeStatus.appAuthenticated]);
  const secondaryPrice = useMemo(() => (runtimeStatus.appAuthenticated ? secondarySession?.spotLtp ?? null : null), [secondarySession?.spotLtp, runtimeStatus.appAuthenticated]);

  async function handleBrokerLogin() {
    if (!runtimeStatus.appAuthenticated) {
      toast.error("App login required before broker login");
      return;
    }
    setLoginPending(true);
    try {
      const response = await loginToBroker();
      setRuntimeStatus(await fetchRuntimeStatus());
      toast.success(response.authenticated ? "Broker session refreshed" : "Broker login request sent");
    } catch {
      toast.error("Broker login failed");
    } finally {
      setLoginPending(false);
    }
  }

  return (
    <div className="flex h-[calc(100vh-5.5rem)] min-h-[36rem] flex-col gap-2 pb-2">
      <OptionsHeader status={runtimeStatus} onBrokerLogin={handleBrokerLogin} loginPending={loginPending} />

      {/* Underlying selector + compact spot ticker */}
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
        <span className="mx-2 h-[18px] w-px bg-[var(--border-soft)]" />
        <SpotTicker label="NIFTY" price={primaryPrice} forwardPrice={primaryForward} />
        <span className="mx-1 h-[18px] w-px bg-[var(--border-soft)]" />
        <SpotTicker label="BNF" price={secondaryPrice} forwardPrice={secondaryForward} />
        <span className="ml-auto text-[10px] text-[var(--dim)]">auto-managed sessions</span>
      </div>

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
