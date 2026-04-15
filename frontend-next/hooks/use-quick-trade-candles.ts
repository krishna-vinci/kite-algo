"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import type { CandlePoint, ChartTimeframe } from "@/components/options/types";
import { fetchCandles } from "@/lib/options/api";

type QuickTradeSymbol = "NIFTY" | "GOLDM_FUT";

type DebugCounts = { history: number; live: number; error: number };

export type QuickTradeCandlesState = {
  chartCandles: Record<QuickTradeSymbol, CandlePoint[]>;
  liveCandles: Record<QuickTradeSymbol, CandlePoint | null>;
  latestPrices: Record<QuickTradeSymbol, number | null>;
  referenceCloses: Record<QuickTradeSymbol, number | null>;
  debugCounts: Record<QuickTradeSymbol, DebugCounts>;
  chartLoading: boolean;
  timeframe: ChartTimeframe;
  setTimeframe: (value: ChartTimeframe) => void;
  liveConnected: boolean;
  lastUpdateAt: number | null;
  historyGeneration: number;
};

function emptyCounts(tokens: Record<QuickTradeSymbol, string>) {
  return Object.fromEntries(Object.keys(tokens).map((key) => [key, { history: 0, live: 0, error: 0 }])) as Record<QuickTradeSymbol, DebugCounts>;
}

export function useQuickTradeCandles(appAuthenticated: boolean, instrumentTokens: Record<QuickTradeSymbol, string>): QuickTradeCandlesState {
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("15m");
  const [chartLoading, setChartLoading] = useState(false);
  const emptyCandleMap = useMemo(
    () => Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, []])) as unknown as Record<QuickTradeSymbol, CandlePoint[]>,
    [instrumentTokens],
  );
  const emptyLiveMap = useMemo(
    () => Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as unknown as Record<QuickTradeSymbol, CandlePoint | null>,
    [instrumentTokens],
  );

  const [chartCandles, setChartCandles] = useState<Record<QuickTradeSymbol, CandlePoint[]>>(emptyCandleMap);
  const [liveCandles, setLiveCandles] = useState<Record<QuickTradeSymbol, CandlePoint | null>>(emptyLiveMap);
  const [latestPrices, setLatestPrices] = useState<Record<QuickTradeSymbol, number | null>>(
    Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<QuickTradeSymbol, number | null>,
  );
  const [referenceCloses, setReferenceCloses] = useState<Record<QuickTradeSymbol, number | null>>(
    Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<QuickTradeSymbol, number | null>,
  );
  const [debugCounts, setDebugCounts] = useState<Record<QuickTradeSymbol, DebugCounts>>(emptyCounts(instrumentTokens));
  const [liveConnected, setLiveConnected] = useState(false);
  const [lastUpdateAt, setLastUpdateAt] = useState<number | null>(null);
  const [historyGeneration, setHistoryGeneration] = useState(0);

  useEffect(() => {
    if (!appAuthenticated) {
      setChartLoading(false);
      setChartCandles(emptyCandleMap);
      setLiveCandles(emptyLiveMap);
      setLatestPrices(Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<QuickTradeSymbol, number | null>);
      setReferenceCloses(Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<QuickTradeSymbol, number | null>);
      setDebugCounts(emptyCounts(instrumentTokens));
      setLiveConnected(false);
      setLastUpdateAt(null);
      return;
    }

    let disposed = false;
    let pollHandle: number | null = null;

    setChartCandles(emptyCandleMap);
    setLiveCandles(emptyLiveMap);
    setDebugCounts(emptyCounts(instrumentTokens));
    setLiveConnected(false);
    setLastUpdateAt(null);

    const fetchWindowStart = (forDaily = false) => {
      const now = new Date();
      const start = new Date(now);
      if (forDaily) {
        start.setDate(start.getDate() - 14);
      } else if (timeframe === "1d") {
        start.setDate(start.getDate() - 30);
      } else {
        start.setHours(start.getHours() - 8);
      }
      return start.toISOString();
    };

    async function loadHistory() {
      setChartLoading(true);
      try {
        const entries = Object.entries(instrumentTokens) as Array<[QuickTradeSymbol, string]>;
        const toIso = new Date().toISOString();
        const results = await Promise.all(entries.map(async ([symbol, identifier]) => [symbol, await fetchCandles({ identifier, timeframe, fromIso: fetchWindowStart(false), toIso })] as const));
        const dailyResults = await Promise.all(entries.map(async ([symbol, identifier]) => [symbol, await fetchCandles({ identifier, timeframe: "1d", fromIso: fetchWindowStart(true), toIso })] as const));

        if (disposed) {
          return;
        }

        setChartCandles(Object.fromEntries(results) as Record<QuickTradeSymbol, CandlePoint[]>);
        setReferenceCloses(
          Object.fromEntries(
            dailyResults.map(([symbol, candles]) => {
              const previousClose = candles.length >= 2 ? candles[candles.length - 2]?.close ?? null : candles[0]?.close ?? null;
              return [symbol, previousClose];
            }),
          ) as Record<QuickTradeSymbol, number | null>,
        );
        setDebugCounts((current) => {
          const next = { ...current };
          for (const [symbol] of results) {
            next[symbol] = { ...next[symbol], history: next[symbol].history + 1 };
          }
          return next;
        });
        setHistoryGeneration((value) => value + 1);
      } catch {
        if (!disposed) {
          toast.error("Unable to load Quick Trade candles.");
        }
      } finally {
        if (!disposed) {
          setChartLoading(false);
        }
      }
    }

    async function pollLatest() {
      try {
        const entries = Object.entries(instrumentTokens) as Array<[QuickTradeSymbol, string]>;
        const toIso = new Date().toISOString();
        const results = await Promise.all(entries.map(async ([symbol, identifier]) => [symbol, await fetchCandles({ identifier, timeframe, fromIso: fetchWindowStart(false), toIso })] as const));

        if (disposed) {
          return;
        }

        let sawLive = false;
        setLiveCandles((current) => {
          const next = { ...current };
          for (const [symbol, candles] of results) {
            const latest = candles[candles.length - 1] ?? null;
            if (!latest) {
              continue;
            }
            sawLive = true;
            next[symbol] = latest;
          }
          return next;
        });
        setLatestPrices((current) => {
          const next = { ...current };
          for (const [symbol, candles] of results) {
            const latest = candles[candles.length - 1] ?? null;
            if (latest) {
              next[symbol] = latest.close;
            }
          }
          return next;
        });
        setDebugCounts((current) => {
          const next = { ...current };
          for (const [symbol, candles] of results) {
            if (candles[candles.length - 1]) {
              next[symbol] = { ...next[symbol], live: next[symbol].live + 1 };
            }
          }
          return next;
        });
        if (sawLive) {
          setLiveConnected(true);
          setLastUpdateAt(Date.now());
        }
      } catch {
        if (!disposed) {
          setLiveConnected(false);
          setDebugCounts((current) => {
            const next = { ...current };
            for (const symbol of Object.keys(instrumentTokens) as QuickTradeSymbol[]) {
              next[symbol] = { ...next[symbol], error: next[symbol].error + 1 };
            }
            return next;
          });
        }
      }
    }

    void loadHistory();
    void pollLatest();
    pollHandle = window.setInterval(() => {
      void pollLatest();
    }, 2000);

    return () => {
      disposed = true;
      if (pollHandle !== null) {
        window.clearInterval(pollHandle);
      }
    };
  }, [appAuthenticated, timeframe, instrumentTokens, emptyCandleMap, emptyLiveMap]);

  return { chartCandles, liveCandles, latestPrices, referenceCloses, debugCounts, chartLoading, timeframe, setTimeframe, liveConnected, lastUpdateAt, historyGeneration };
}
