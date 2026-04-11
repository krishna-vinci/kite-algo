"use client";

import { useEffect, useMemo, useRef } from "react";
import { CandlestickSeries, createChart, type IChartApi, type ISeriesApi, type Logical, type Time } from "lightweight-charts";
import type { CandlePoint, ChartTimeframe } from "@/components/options/types";

type LightweightChartPanelProps = Readonly<{
  label: string;
  price: number | null;
  changePercent: number | null;
  forwardPrice: number | null;
  timeframe: ChartTimeframe;
  candles: CandlePoint[];
  liveCandle?: CandlePoint | null;
  loading?: boolean;
  onTimeframeChange: (value: ChartTimeframe) => void;
}>;

const timeframes: ChartTimeframe[] = ["5m", "15m", "60m", "1d"];

function formatSeriesData(candles: CandlePoint[]) {
  return candles.map((candle) => ({
    time: Math.floor(candle.time) as Time,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
  }));
}

export function LightweightChartPanel({ label, price, changePercent, forwardPrice, timeframe, candles, liveCandle = null, loading = false, onTimeframeChange }: LightweightChartPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const initializedRef = useRef(false);
  const lastTimeframeRef = useRef<ChartTimeframe>(timeframe);
  const formattedCandles = useMemo(() => formatSeriesData(candles), [candles]);
  const formattedLiveCandle = useMemo(() => (liveCandle ? formatSeriesData([liveCandle])[0] ?? null : null), [liveCandle]);
  const futureBasis = price !== null && forwardPrice !== null ? forwardPrice - price : null;

  useEffect(() => {
    if (!containerRef.current || typeof ResizeObserver === "undefined") {
      return undefined;
    }

    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { color: "#161820" },
        textColor: "#8b8fa4",
      },
      grid: {
        horzLines: { color: "rgba(255,255,255,0.05)" },
        vertLines: { color: "rgba(255,255,255,0.03)" },
      },
      rightPriceScale: { borderColor: "#232636" },
      timeScale: { borderColor: "#232636", timeVisible: true },
      crosshair: {
        horzLine: { color: "rgba(249,115,22,0.5)" },
        vertLine: { color: "rgba(249,115,22,0.3)" },
      },
    });
    chartRef.current = chart;

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#34d399",
      downColor: "#f87171",
      wickUpColor: "#34d399",
      wickDownColor: "#f87171",
      borderVisible: false,
    });
    seriesRef.current = candleSeries;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      initializedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current || !seriesRef.current) {
      return;
    }

    chartRef.current.applyOptions({
      timeScale: { borderColor: "#232636", timeVisible: timeframe !== "1d" },
    });

    const timeframeChanged = lastTimeframeRef.current !== timeframe;
    lastTimeframeRef.current = timeframe;

    if (formattedCandles.length === 0) {
      seriesRef.current.setData([]);
      initializedRef.current = false;
      return;
    }

    seriesRef.current.setData(formattedCandles);
    if (!initializedRef.current || timeframeChanged) {
      const lastIndex = formattedCandles.length - 1;
      const barsToShow = Math.min(15, formattedCandles.length);
      const range = { from: Math.max(0, lastIndex - barsToShow + 1) as Logical, to: (lastIndex + 0.5) as Logical };
      chartRef.current.timeScale().setVisibleLogicalRange(range);
    }
    initializedRef.current = true;
  }, [formattedCandles, timeframe]);

  useEffect(() => {
    if (!seriesRef.current || !chartRef.current || !initializedRef.current || !formattedLiveCandle) {
      return;
    }

    seriesRef.current.update(formattedLiveCandle);
  }, [formattedLiveCandle, timeframe]);

  return (
    <section className="relative h-full rounded-2xl border border-[var(--border)] bg-[var(--panel)] p-3">
      <div className="mb-2 flex items-center gap-2 border-b border-[var(--border-soft)] pb-2">
        <span className="text-xs font-medium uppercase tracking-[0.18em] text-[var(--dim)]">{label}</span>
        {candles.length > 0 && !loading && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--green)]" title="live" />}
        <span className="font-mono text-sm text-[var(--text)]">{price === null ? "—" : price.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</span>
        <span className={`font-mono text-[11px] ${changePercent === null ? "text-[var(--dim)]" : changePercent >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>
          {changePercent === null ? "—" : `${changePercent >= 0 ? "+" : ""}${changePercent.toFixed(2)}%`}
        </span>
        <span className="mx-1 h-[18px] w-px bg-[var(--border-soft)]" />
        <span className="text-[10px] uppercase tracking-[0.16em] text-[var(--dim)]">fut</span>
        <span className="font-mono text-[11px] text-[var(--blue)]">{forwardPrice === null ? "—" : forwardPrice.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</span>
        {futureBasis !== null ? <span className="font-mono text-[10px] text-[var(--muted)]">{futureBasis >= 0 ? "+" : ""}{futureBasis.toFixed(2)}</span> : null}
        <div className="ml-auto flex items-center gap-1 overflow-x-auto">
          {timeframes.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => onTimeframeChange(item)}
              className={`rounded px-1.5 py-1 text-[10px] font-mono transition-colors ${timeframe === item ? "bg-[var(--accent)] text-black" : "text-[var(--muted)] hover:text-[var(--text)]"}`}
            >
              {item}
            </button>
          ))}
        </div>
      </div>
      <div ref={containerRef} className="h-[calc(100%-2.7rem)] min-h-24" />
      {loading && (
        <div className="absolute inset-x-3 bottom-3 flex items-center gap-2 rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-2 py-1 text-[10px] text-[var(--accent)]">
          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--accent)]" />
          loading…
        </div>
      )}
      {!loading && candles.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="rounded-md border border-[var(--border)] bg-[var(--bg)]/90 px-3 py-2 text-[10px] text-[var(--muted)]">no candle data</span>
        </div>
      )}
    </section>
  );
}
