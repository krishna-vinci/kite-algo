"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { QuickTradeChartStrip } from "@/components/quick-trade/quick-trade-chart-strip";
import type { Underlying, OptionSessionSnapshot, RuntimeStatus } from "@/components/options/types";
import { useQuickTradeCandles } from "@/hooks/use-quick-trade-candles";
import {
  buildOptionsSessionSseUrl,
  ensureOptionsSessions,
  fetchOptionSession,
  fetchRuntimeStatus,
  loginToBroker,
  mergeOptionSessionSnapshot,
  normalizeOptionSessionSnapshot,
} from "@/lib/options/api";

const INDEX_TOKENS: Record<Underlying, string> = {
  NIFTY: "256265",
  BANKNIFTY: "260105",
};

const QUICK_TRADE_TOKENS = {
  NIFTY: "256265",
  GOLDM_FUT: "124881671",
} as const;

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

function createFallbackSession(underlying: Underlying): OptionSessionSnapshot {
  const spotPrice = underlying === "BANKNIFTY" ? 48240 : 23460;
  const gap = underlying === "BANKNIFTY" ? 100 : 50;
  const atmStrike = Math.round(spotPrice / gap) * gap;
  return {
    underlying,
    spotLtp: spotPrice,
    atmStrike,
    expiries: ["2026-04-30"],
    perExpiry: {
      "2026-04-30": {
        forward: spotPrice + 12,
        sigmaExpiry: null,
        atmStrike,
        strikes: [],
        rows: [],
      },
    },
    rows: [],
    updatedAt: null,
  };
}

function StatusDot({ label, ok }: Readonly<{ label: string; ok: boolean }>) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-[var(--green)]" : "bg-[var(--dim)]"}`} />
      <span className="text-[10px] uppercase tracking-[0.12em] text-[var(--muted)]">{label}</span>
    </div>
  );
}

function DebugValue({ label, value }: Readonly<{ label: string; value: string }>) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[9px] uppercase tracking-[0.12em] text-[var(--dim)]">{label}</span>
      <span className="font-mono text-[10px] text-[var(--text)]">{value}</span>
    </div>
  );
}

export function QuickTradeWorkspace() {
  const [chartHeight, setChartHeight] = useState(360);
  const [splitPercent, setSplitPercent] = useState(50);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>(createFallbackRuntimeStatus());
  const [loginPending, setLoginPending] = useState(false);
  const [sessions, setSessions] = useState<Record<Underlying, OptionSessionSnapshot>>({
    NIFTY: createFallbackSession("NIFTY"),
    BANKNIFTY: createFallbackSession("BANKNIFTY"),
  });

  const { chartCandles, liveCandles, latestPrices, referenceCloses, debugCounts, chartLoading, timeframe, setTimeframe, liveConnected, lastUpdateAt, historyGeneration } = useQuickTradeCandles(runtimeStatus.appAuthenticated, QUICK_TRADE_TOKENS);

  // --- runtime status polling ---
  useEffect(() => {
    let disposed = false;
    async function poll() {
      try {
        const status = await fetchRuntimeStatus();
        if (!disposed) setRuntimeStatus(status);
      } catch { /* ignore */ }
    }
    void poll();
    const interval = window.setInterval(poll, 15000);
    return () => { disposed = true; window.clearInterval(interval); };
  }, []);

  // --- SSE for option session (forward prices) ---
  useEffect(() => {
    if (!runtimeStatus.appAuthenticated) return;

    let disposed = false;
    const streams: EventSource[] = [];

    async function primeSessions() {
      try {
        await ensureOptionsSessions();
        const results = await Promise.allSettled(
          (Object.keys(INDEX_TOKENS) as Underlying[]).map((item) => fetchOptionSession(item)),
        );
        if (disposed) return;
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
        if (!disposed) toast.error("Unable to load option session for forward prices.");
      }
    }

    void primeSessions();

    for (const item of Object.keys(INDEX_TOKENS) as Underlying[]) {
      const source = new EventSource(buildOptionsSessionSseUrl(item), { withCredentials: true });
      source.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as { type?: string } & Record<string, unknown>;
          if (payload.type === "error") return;
          const snapshot = normalizeOptionSessionSnapshot(payload as never);
          if (!disposed) {
            setSessions((current) => ({ ...current, [item]: mergeOptionSessionSnapshot(current[item], snapshot) }));
          }
        } catch { /* ignore keep-alive */ }
      };
      streams.push(source);
    }

    return () => {
      disposed = true;
      streams.forEach((source) => source.close());
    };
  }, [runtimeStatus.appAuthenticated]);

  // --- derived prices ---
  const readForward = (session: OptionSessionSnapshot | null | undefined) => {
    if (!session) return null;
    const firstExpiry = session.expiries[0];
    return firstExpiry ? session.perExpiry[firstExpiry]?.forward ?? null : null;
  };

  const primaryForward = useMemo(() => readForward(sessions.NIFTY), [sessions.NIFTY]);
  const primaryPrice = useMemo(() => {
    return liveCandles.NIFTY?.close ?? latestPrices.NIFTY ?? sessions.NIFTY?.spotLtp ?? null;
  }, [latestPrices.NIFTY, liveCandles.NIFTY?.close, sessions.NIFTY?.spotLtp]);

  const secondaryPrice = useMemo(() => {
    return liveCandles.GOLDM_FUT?.close ?? latestPrices.GOLDM_FUT ?? null;
  }, [latestPrices.GOLDM_FUT, liveCandles.GOLDM_FUT?.close]);

  const primaryChange = useMemo(() => {
    const previousClose = referenceCloses.NIFTY;
    const latest = liveCandles.NIFTY?.close ?? primaryPrice;
    return previousClose && latest ? ((latest - previousClose) / previousClose) * 100 : null;
  }, [liveCandles.NIFTY?.close, primaryPrice, referenceCloses.NIFTY]);

  const secondaryChange = useMemo(() => {
    const previousClose = referenceCloses.GOLDM_FUT;
    const latest = liveCandles.GOLDM_FUT?.close ?? secondaryPrice;
    return previousClose && latest ? ((latest - previousClose) / previousClose) * 100 : null;
  }, [liveCandles.GOLDM_FUT?.close, referenceCloses.GOLDM_FUT, secondaryPrice]);

  const chartStreamFresh = useMemo(() => {
    if (!liveConnected || lastUpdateAt === null) {
      return false;
    }
    return Date.now() - lastUpdateAt < 30_000;
  }, [lastUpdateAt, liveConnected]);

  const lastUpdateLabel = useMemo(() => {
    const effective = lastUpdateAt ?? (liveCandles.GOLDM_FUT?.time ? liveCandles.GOLDM_FUT.time * 1000 : null);
    if (effective === null) {
      return "none";
    }
    return new Date(effective).toLocaleTimeString("en-IN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }, [lastUpdateAt, liveCandles.GOLDM_FUT?.time]);

  const niftyLiveTime = useMemo(() => {
    if (!liveCandles.NIFTY?.time) {
      return "none";
    }
    return new Date(liveCandles.NIFTY.time * 1000).toLocaleTimeString("en-IN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }, [liveCandles.NIFTY?.time]);

  const goldLiveTime = useMemo(() => {
    if (!liveCandles.GOLDM_FUT?.time) {
      return "none";
    }
    return new Date(liveCandles.GOLDM_FUT.time * 1000).toLocaleTimeString("en-IN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }, [liveCandles.GOLDM_FUT?.time]);

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
      {/* Compact header bar */}
      <header className="flex items-center gap-3 rounded-2xl border border-[var(--border)] bg-[var(--panel)] px-3 py-2">
        <h1 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-[var(--text)]">Quick Trade</h1>
        <div className="ml-auto flex items-center gap-3">
          <StatusDot label="broker" ok={runtimeStatus.brokerStatus === "connected"} />
          <StatusDot label="ws" ok={runtimeStatus.websocketStatus === "connected"} />
          <StatusDot label="charts" ok={chartStreamFresh} />
          <StatusDot label="paper" ok={runtimeStatus.paperAvailable} />
          {runtimeStatus.brokerStatus !== "connected" && (
            <button
              type="button"
              onClick={handleBrokerLogin}
              disabled={loginPending}
              className="cursor-pointer rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-2.5 py-1 text-[10px] font-semibold text-[var(--accent)] transition-colors duration-200 hover:bg-[var(--accent)]/15 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {loginPending ? "Refreshing…" : "Connect"}
            </button>
          )}
        </div>
      </header>

      <section className="grid grid-cols-2 gap-x-3 gap-y-1 rounded-xl border border-[var(--border)] bg-[var(--panel)]/70 px-3 py-2 md:grid-cols-4 xl:grid-cols-8">
        <DebugValue label="tf" value={timeframe} />
        <DebugValue label="stream" value={liveConnected ? "yes" : "no"} />
        <DebugValue label="fresh" value={chartStreamFresh ? "yes" : "no"} />
        <DebugValue label="update" value={lastUpdateLabel} />
        <DebugValue label="nifty live" value={liveCandles.NIFTY?.close?.toFixed(2) ?? "none"} />
        <DebugValue label="gold live" value={liveCandles.GOLDM_FUT?.close?.toFixed(2) ?? "none"} />
        <DebugValue label="nifty t" value={niftyLiveTime} />
        <DebugValue label="gold t" value={goldLiveTime} />
        <DebugValue label="n h/l/e" value={`${debugCounts.NIFTY?.history ?? 0}/${debugCounts.NIFTY?.live ?? 0}/${debugCounts.NIFTY?.error ?? 0}`} />
        <DebugValue label="g h/l/e" value={`${debugCounts.GOLDM_FUT?.history ?? 0}/${debugCounts.GOLDM_FUT?.live ?? 0}/${debugCounts.GOLDM_FUT?.error ?? 0}`} />
      </section>

      {/* Full-height chart area */}
      <div className="flex-1 min-h-0">
        <QuickTradeChartStrip
          chartHeight={chartHeight}
          splitPercent={splitPercent}
          timeframe={timeframe}
          onChartHeightChange={setChartHeight}
          onSplitPercentChange={setSplitPercent}
          onTimeframeChange={setTimeframe}
          historyGeneration={historyGeneration}
          primary={{
            label: "NIFTY",
            price: primaryPrice,
            changePercent: primaryChange,
            forwardPrice: primaryForward,
            candles: chartCandles.NIFTY ?? [],
            liveCandle: liveCandles.NIFTY,
            loading: chartLoading,
          }}
          secondary={{
            label: "GOLDM FUT",
            price: secondaryPrice,
            changePercent: secondaryChange,
            forwardPrice: null,
            candles: chartCandles.GOLDM_FUT ?? [],
            liveCandle: liveCandles.GOLDM_FUT,
            loading: chartLoading,
          }}
          fillHeight
        />
      </div>
    </div>
  );
}
