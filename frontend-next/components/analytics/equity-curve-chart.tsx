"use client";

import { useEffect, useRef } from "react";
import {
  createChart,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  ColorType,
  LineStyle,
} from "lightweight-charts";

import type { EquityCurvePoint } from "@/lib/analytics/types";

type EquityCurveChartProps = {
  points: EquityCurvePoint[];
  /** Height in px — defaults to 320 */
  height?: number;
};

function toNum(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

/**
 * EquityCurveChart — renders net P&L, benchmark, and excess return lines
 * using lightweight-charts with project colour tokens resolved from CSS.
 */
export function EquityCurveChart({ points, height = 320 }: EquityCurveChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const netRef = useRef<ISeriesApi<"Line"> | null>(null);
  const benchRef = useRef<ISeriesApi<"Line"> | null>(null);
  const excessRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Resolve CSS colour tokens at mount — works in both light and dark modes
  useEffect(() => {
    if (!containerRef.current) return;

    const style = getComputedStyle(containerRef.current);
    const green = style.getPropertyValue("--green").trim() || "#22c55e";
    const red = style.getPropertyValue("--red").trim() || "#ef4444";
    const muted = style.getPropertyValue("--muted-foreground").trim() || "#71717a";
    const border = style.getPropertyValue("--border").trim() || "#27272a";
    const bg = style.getPropertyValue("--background").trim() || "#09090b";
    const fg = style.getPropertyValue("--foreground").trim() || "#fafafa";

    const chart = createChart(containerRef.current, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: muted,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: border, style: LineStyle.Dotted },
        horzLines: { color: border, style: LineStyle.Dotted },
      },
      crosshair: { vertLine: { labelBackgroundColor: bg }, horzLine: { labelBackgroundColor: bg } },
      rightPriceScale: { borderColor: border },
      timeScale: { borderColor: border, fixLeftEdge: true, fixRightEdge: true },
    });

    const netSeries = chart.addSeries(LineSeries, {
      color: green,
      lineWidth: 2,
      title: "Net P&L",
      priceFormat: { type: "custom", formatter: (v: number) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}` },
    });
    const benchSeries = chart.addSeries(LineSeries, {
      color: muted,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: "Benchmark",
    });
    const excessSeries = chart.addSeries(LineSeries, {
      color: fg,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "Excess Return",
    });

    chartRef.current = chart;
    netRef.current = netSeries;
    benchRef.current = benchSeries;
    excessRef.current = excessSeries;

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height]);

  // Push data whenever points change
  useEffect(() => {
    if (!netRef.current || !benchRef.current || !excessRef.current) return;

    const sorted = [...points].sort((a, b) =>
      a.trading_date.localeCompare(b.trading_date),
    );

    const netData: LineData[] = [];
    const benchData: LineData[] = [];
    const excessData: LineData[] = [];
    let runningNet = 0;

    for (const p of sorted) {
      const pnl = toNum(p.realized_pnl);
      if (pnl !== null) runningNet += pnl;

      netData.push({ time: p.trading_date, value: runningNet });

      const bench = toNum(p.benchmark_return_pct);
      if (bench !== null) benchData.push({ time: p.trading_date, value: Number(bench) });

      const excess = toNum(p.excess_return_pct);
      if (excess !== null) excessData.push({ time: p.trading_date, value: Number(excess) });
    }

    netRef.current.setData(netData);
    benchRef.current.setData(benchData);
    excessRef.current.setData(excessData);

    chartRef.current?.timeScale().fitContent();
  }, [points]);

  if (points.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-xl border border-border/60 bg-muted/10 text-sm text-muted-foreground"
        style={{ height }}
      >
        No equity curve data for the selected period.
      </div>
    );
  }

  return (
    <div className="w-full">
      <div ref={containerRef} className="w-full" style={{ height }} />
      <div className="mt-2 flex flex-wrap items-center gap-3 text-[10px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-[var(--green)]" /> Net P&amp;L
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-muted-foreground" /> Benchmark
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-foreground" /> Excess
        </span>
      </div>
    </div>
  );
}
