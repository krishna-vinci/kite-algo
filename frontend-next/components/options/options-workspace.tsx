"use client";

import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FloatingOrderTicket } from "@/components/options/floating-order-ticket";
import { NiftyImpactPanel } from "@/components/options/nifty-impact-panel";
import { OptionChainPanel } from "@/components/options/option-chain-panel";
import { OptionsHeader } from "@/components/options/options-header";
import { StrategyBuilderPanel } from "@/components/options/strategy-builder-panel";
import type { MiniChainSnapshot, NiftyImpactRow, OptionSessionSnapshot, Underlying } from "@/components/options/types";
import { WorkspaceTabs } from "@/components/options/workspace-tabs";
import { CompactTradingDock } from "@/features/trading/components/compact-trading-dock";
import { MarketQuoteStrip } from "@/features/trading/components/market-quote-strip";
import { useTradingConsoleData } from "@/features/trading/hooks/use-trading-console-data";
import {
  buildOptionsSessionSseUrl,
  ensureOptionsSessions,
  fetchNifty50Impact,
  fetchOptionSession,
  loginToBroker,
  mergeOptionSessionSnapshot,
  normalizeOptionSessionSnapshot,
} from "@/lib/options/api";

const INDEX_TOKENS: Record<Underlying, string> = {
  NIFTY: "256265",
  BANKNIFTY: "260105",
};

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
  const queryClient = useQueryClient();
  const tradingSnapshot = useTradingConsoleData();
  const runtimeStatus = tradingSnapshot.runtime;
  const [activeTab, setActiveTab] = useState<"chain" | "builder" | "impact">("builder");
  const [underlying, setUnderlying] = useState<Underlying>("NIFTY");
  const [deltaFilter, setDeltaFilter] = useState(0.3);
  const [loginPending, setLoginPending] = useState(false);
  const [expiries, setExpiries] = useState<string[]>([]);
  const [selectedExpiry, setSelectedExpiry] = useState("");
  const [sessions, setSessions] = useState<Record<Underlying, OptionSessionSnapshot | null>>({
    NIFTY: null,
    BANKNIFTY: null,
  });
  const [impactRows, setImpactRows] = useState<NiftyImpactRow[]>([]);
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
    if (!runtimeStatus.appAuthenticated) {
      setImpactRows([]);
      return;
    }

    let disposed = false;

    async function loadImpact() {
      try {
        const rows = await fetchNifty50Impact();
        if (!disposed) {
          setImpactRows(rows);
        }
      } catch {
        if (!disposed) {
          setImpactRows([]);
        }
      }
    }

    void loadImpact();
    const interval = window.setInterval(loadImpact, 15000);
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
    const nextExpiries = session?.expiries?.length ? session.expiries : [];
    setExpiries(nextExpiries);
    setSelectedExpiry((current) => (nextExpiries.includes(current) ? current : nextExpiries[0] ?? ""));
  }, [sessions, underlying]);

  const primarySession = sessions.NIFTY;
  const secondarySession = sessions.BANKNIFTY;
  const quoteBySymbol = Object.fromEntries(tradingSnapshot.quotes.map((quote) => [quote.symbol, quote])) as Record<string, typeof tradingSnapshot.quotes[number]>;
  const chain = useMemo(() => {
    const liveChain = toMiniChainSnapshot(sessions[underlying], selectedExpiry);
    return liveChain;
  }, [selectedExpiry, sessions, underlying]);
  const chainLoading = runtimeStatus.appAuthenticated && !toMiniChainSnapshot(sessions[underlying], selectedExpiry);
  const primaryForward = useMemo(() => (runtimeStatus.appAuthenticated ? readForwardPrice(primarySession) : null), [primarySession, runtimeStatus.appAuthenticated]);
  const secondaryForward = useMemo(() => (runtimeStatus.appAuthenticated ? readForwardPrice(secondarySession) : null), [secondarySession, runtimeStatus.appAuthenticated]);

  const primaryPrice = useMemo(
    () => (runtimeStatus.appAuthenticated ? primarySession?.spotLtp ?? quoteBySymbol.NIFTY?.lastPrice ?? null : null),
    [primarySession?.spotLtp, quoteBySymbol.NIFTY?.lastPrice, runtimeStatus.appAuthenticated],
  );
  const secondaryPrice = useMemo(
    () => (runtimeStatus.appAuthenticated ? secondarySession?.spotLtp ?? quoteBySymbol.BANKNIFTY?.lastPrice ?? null : null),
    [quoteBySymbol.BANKNIFTY?.lastPrice, runtimeStatus.appAuthenticated, secondarySession?.spotLtp],
  );

  async function handleBrokerLogin() {
    if (!runtimeStatus.appAuthenticated) {
      toast.error("App login required before broker login");
      return;
    }
    setLoginPending(true);
    try {
      const response = await loginToBroker();
      await queryClient.invalidateQueries({ queryKey: ["trading", "runtime-status"] });
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
        <span className="ml-auto hidden text-[10px] text-[var(--dim)] lg:inline">canonical paper + broker state</span>
        <MarketQuoteStrip quotes={tradingSnapshot.quotes} compact className="hidden xl:flex" />
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

      <CompactTradingDock workspace="/options" paper={tradingSnapshot.paper} broker={tradingSnapshot.broker} />
    </div>
  );
}
