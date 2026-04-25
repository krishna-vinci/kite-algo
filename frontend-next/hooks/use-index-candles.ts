"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import type { CandlePoint, ChartTimeframe } from "@/components/options/types";
import { buildCandlesStreamUrl, fetchCandles } from "@/lib/options/api";

type ChartSymbol = string;

type CandleStreamSnapshotEvent = {
  instrument_token: number;
  interval: string;
  candles: unknown[][];
};

type CandleStreamUpdateEvent = {
  event: "tick" | "candle";
  instrument_token: number;
  interval: string;
  candle: unknown[];
};

function parseStreamCandle(candle: unknown[]): CandlePoint | null {
  if (!Array.isArray(candle) || candle.length < 6) {
    return null;
  }
  const timeValue = candle[0];
  const isoMillis = typeof timeValue === "string" ? Date.parse(timeValue) : Number(timeValue) * 1000;
  if (!Number.isFinite(isoMillis)) {
    return null;
  }
  const open = Number(candle[1]);
  const high = Number(candle[2]);
  const low = Number(candle[3]);
  const close = Number(candle[4]);
  const volume = Number(candle[5] ?? 0);
  if (![open, high, low, close, volume].every(Number.isFinite)) {
    return null;
  }
  return {
    time: Math.floor(isoMillis / 1000),
    open,
    high,
    low,
    close,
    volume,
  };
}

function mergeCandleSeries(current: CandlePoint[], incoming: CandlePoint[]): CandlePoint[] {
  if (incoming.length === 0) {
    return current;
  }
  const merged = new Map<number, CandlePoint>();
  for (const candle of current) {
    merged.set(candle.time, candle);
  }
  for (const candle of incoming) {
    merged.set(candle.time, candle);
  }
  return Array.from(merged.values()).sort((left, right) => left.time - right.time);
}

export type IndexCandlesState = {
  chartCandles: Record<ChartSymbol, CandlePoint[]>;
  liveCandles: Record<ChartSymbol, CandlePoint | null>;
  latestPrices: Record<ChartSymbol, number | null>;
  referenceCloses: Record<ChartSymbol, number | null>;
  debugCounts: Record<ChartSymbol, { snapshot: number; tick: number; candle: number; error: number }>;
  chartLoading: boolean;
  timeframe: ChartTimeframe;
  setTimeframe: (tf: ChartTimeframe) => void;
  streamConnected: boolean;
  lastUpdateAt: number | null;
  /** Bumps each time full history data is loaded (REST fetch or snapshot). Used to distinguish setData vs update in the chart. */
  historyGeneration: number;
};

/**
 * Shared hook for NIFTY/BANKNIFTY candle fetching + SSE streaming.
 * Used by both Quick Trade and (previously) Options workspace.
 */
export function useIndexCandles(appAuthenticated: boolean, instrumentTokens: Record<ChartSymbol, string>): IndexCandlesState {
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("15m");
  const [chartLoading, setChartLoading] = useState(false);
  const emptyCandleMap = useMemo(
    () => Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, []])) as Record<ChartSymbol, CandlePoint[]>,
    [instrumentTokens],
  );
  const emptyLiveMap = useMemo(
    () => Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<ChartSymbol, CandlePoint | null>,
    [instrumentTokens],
  );
  const [chartCandles, setChartCandles] = useState<Record<ChartSymbol, CandlePoint[]>>(emptyCandleMap);
  const [liveCandles, setLiveCandles] = useState<Record<ChartSymbol, CandlePoint | null>>(emptyLiveMap);
  const [latestPrices, setLatestPrices] = useState<Record<ChartSymbol, number | null>>(
    Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<ChartSymbol, number | null>,
  );
  const [referenceCloses, setReferenceCloses] = useState<Record<ChartSymbol, number | null>>(
    Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<ChartSymbol, number | null>,
  );
  const [debugCounts, setDebugCounts] = useState<Record<ChartSymbol, { snapshot: number; tick: number; candle: number; error: number }>>(
    Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, { snapshot: 0, tick: 0, candle: 0, error: 0 }])) as Record<ChartSymbol, { snapshot: number; tick: number; candle: number; error: number }>,
  );
  const [streamConnected, setStreamConnected] = useState(false);
  const [lastUpdateAt, setLastUpdateAt] = useState<number | null>(null);

  // Track a monotonic "generation" counter so the chart component can
  // distinguish a full history replacement (timeframe change / initial load)
  // from incremental updates coming from tick/candle SSE events.
  const [historyGeneration, setHistoryGeneration] = useState(0);

  useEffect(() => {
    if (!appAuthenticated) {
      setChartLoading(false);
      setChartCandles(emptyCandleMap);
      setLiveCandles(emptyLiveMap);
      setLatestPrices(Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<ChartSymbol, number | null>);
      setReferenceCloses(Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, null])) as Record<ChartSymbol, number | null>);
      setDebugCounts(Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, { snapshot: 0, tick: 0, candle: 0, error: 0 }])) as Record<ChartSymbol, { snapshot: number; tick: number; candle: number; error: number }>);
      setStreamConnected(false);
      setLastUpdateAt(null);
      return;
    }

    let disposed = false;
    const controllers: AbortController[] = [];

    // Clear stale candle data when timeframe changes, but preserve latestPrices
    // so the displayed current price doesn't flicker during timeframe switches.
    setChartCandles(emptyCandleMap);
    setLiveCandles(emptyLiveMap);
    setDebugCounts(Object.fromEntries(Object.keys(instrumentTokens).map((key) => [key, { snapshot: 0, tick: 0, candle: 0, error: 0 }])) as Record<ChartSymbol, { snapshot: number; tick: number; candle: number; error: number }>);
    setStreamConnected(false);
    setLastUpdateAt(null);

    async function loadCharts() {
      setChartLoading(true);
      try {
        const entries = Object.entries(instrumentTokens) as Array<[ChartSymbol, string]>;
        const toIso = new Date().toISOString();
        const fromDate = new Date();
        fromDate.setMonth(fromDate.getMonth() - 3);
        const fromIso = fromDate.toISOString();
        const results = await Promise.all(entries.map(async ([symbol, identifier]) => [symbol, await fetchCandles({ identifier, timeframe, fromIso, toIso })] as const));
        const dailyResults = await Promise.all(
          entries.map(async ([symbol, identifier]) => [symbol, await fetchCandles({ identifier, timeframe: "1d", fromIso, toIso })] as const),
        );
        if (!disposed) {
          setChartCandles(Object.fromEntries(results) as Record<ChartSymbol, CandlePoint[]>);
          setHistoryGeneration((g) => g + 1);
          setReferenceCloses(
            Object.fromEntries(
              dailyResults.map(([symbol, candles]) => {
                const previousClose = candles.length >= 2 ? candles[candles.length - 2]?.close ?? null : candles[0]?.open ?? null;
                return [symbol, previousClose];
              }),
            ) as Record<ChartSymbol, number | null>,
          );
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

    const applySnapshot = (item: ChartSymbol, payload: CandleStreamSnapshotEvent) => {
      const incoming = payload.candles.map(parseStreamCandle).filter((value): value is CandlePoint => Boolean(value));
      if (disposed) {
        return;
      }
      setDebugCounts((current) => ({ ...current, [item]: { ...current[item], snapshot: current[item].snapshot + 1 } }));
      setChartCandles((current) => ({ ...current, [item]: mergeCandleSeries(current[item], incoming) }));
      setHistoryGeneration((g) => g + 1);
      setLiveCandles((current) => ({ ...current, [item]: incoming[incoming.length - 1] ?? current[item] }));
      setLatestPrices((current) => ({ ...current, [item]: incoming[incoming.length - 1]?.close ?? current[item] ?? null }));
      setLastUpdateAt(Date.now());
      setStreamConnected(true);
    };

    const applyUpdate = (item: ChartSymbol, payload: CandleStreamUpdateEvent) => {
      const nextCandle = parseStreamCandle(payload.candle);
      if (!nextCandle || disposed) {
        return;
      }
      setDebugCounts((current) => ({
        ...current,
        [item]: {
          ...current[item],
          [payload.event]: current[item][payload.event] + 1,
        },
      }));
      setLiveCandles((current) => ({ ...current, [item]: nextCandle }));
      setLatestPrices((current) => ({ ...current, [item]: nextCandle.close }));
      setLastUpdateAt(Date.now());
      setStreamConnected(true);
    };

    const startStream = async (item: ChartSymbol, token: string) => {
      while (!disposed) {
        const controller = new AbortController();
        controllers.push(controller);
        try {
          const response = await fetch(buildCandlesStreamUrl(token, timeframe), {
            method: "GET",
            credentials: "include",
            headers: { Accept: "text/event-stream" },
            signal: controller.signal,
          });

          if (!response.ok || !response.body) {
            throw new Error(`SSE request failed with status ${response.status}`);
          }

          if (disposed) {
            return;
          }

          setStreamConnected(true);

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          let eventName = "message";
          let dataLines: string[] = [];

          const flushEvent = () => {
            if (dataLines.length === 0) {
              eventName = "message";
              return;
            }
            const rawData = dataLines.join("\n");
            dataLines = [];
            try {
              if (eventName === "snapshot") {
                applySnapshot(item, JSON.parse(rawData) as CandleStreamSnapshotEvent);
              } else if (eventName === "tick" || eventName === "candle") {
                applyUpdate(item, JSON.parse(rawData) as CandleStreamUpdateEvent);
              }
            } catch {
              // ignore malformed payloads
            } finally {
              eventName = "message";
            }
          };

          while (!disposed) {
            const { value, done } = await reader.read();
            if (done) {
              flushEvent();
              break;
            }

            buffer += decoder.decode(value, { stream: true });

            while (true) {
              const newlineIndex = buffer.indexOf("\n");
              if (newlineIndex === -1) {
                break;
              }
              let line = buffer.slice(0, newlineIndex);
              buffer = buffer.slice(newlineIndex + 1);
              if (line.endsWith("\r")) {
                line = line.slice(0, -1);
              }

              if (line === "") {
                flushEvent();
              } else if (line.startsWith("event:")) {
                eventName = line.slice(6).trim();
              } else if (line.startsWith("data:")) {
                dataLines.push(line.slice(5).trimStart());
              }
            }
          }
        } catch {
          if (!disposed && !controller.signal.aborted) {
            setDebugCounts((current) => ({ ...current, [item]: { ...current[item], error: current[item].error + 1 } }));
            setStreamConnected(false);
            await new Promise((resolve) => setTimeout(resolve, 1500));
          }
        }
      }
    };

    for (const [item, token] of Object.entries(instrumentTokens) as Array<[ChartSymbol, string]>) {
      void startStream(item, token);
    }

    return () => {
      disposed = true;
      controllers.forEach((controller) => controller.abort());
    };
  }, [appAuthenticated, timeframe, instrumentTokens, emptyCandleMap, emptyLiveMap]);

  return { chartCandles, liveCandles, latestPrices, referenceCloses, debugCounts, chartLoading, timeframe, setTimeframe, streamConnected, lastUpdateAt, historyGeneration };
}
