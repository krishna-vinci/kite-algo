"use client";

import type { CandlePoint, ChartTimeframe, Underlying } from "@/components/options/types";
import { LightweightChartPanel } from "@/components/options/lightweight-chart-panel";

type ChartStripProps = Readonly<{
  chartHeight: number;
  splitPercent: number;
  timeframe: ChartTimeframe;
  onChartHeightChange: (next: number) => void;
  onSplitPercentChange: (next: number) => void;
  onTimeframeChange: (next: ChartTimeframe) => void;
  primary: { label: Underlying; price: number | null; changePercent: number | null; forwardPrice: number | null; candles: CandlePoint[]; liveCandle?: CandlePoint | null; loading?: boolean };
  secondary: { label: Underlying; price: number | null; changePercent: number | null; forwardPrice: number | null; candles: CandlePoint[]; liveCandle?: CandlePoint | null; loading?: boolean };
}>;

export function ChartStrip({
  chartHeight,
  splitPercent,
  timeframe,
  onChartHeightChange,
  onSplitPercentChange,
  onTimeframeChange,
  primary,
  secondary,
}: ChartStripProps) {
  return (
    <section className="flex-none px-1 pt-1">
      <div className="flex gap-2" style={{ height: chartHeight }}>
        <div style={{ width: `${splitPercent}%` }} className="min-w-0">
          <LightweightChartPanel label={primary.label} price={primary.price} changePercent={primary.changePercent} forwardPrice={primary.forwardPrice} timeframe={timeframe} candles={primary.candles} liveCandle={primary.liveCandle} loading={primary.loading} onTimeframeChange={onTimeframeChange} />
        </div>
        <input
          aria-label="chart split"
          type="range"
          min={30}
          max={70}
          value={splitPercent}
          onChange={(event) => onSplitPercentChange(Number(event.currentTarget.value))}
          className="w-2 cursor-col-resize accent-[var(--accent)] [writing-mode:vertical-lr]"
        />
        <div className="min-w-0 flex-1">
          <LightweightChartPanel label={secondary.label} price={secondary.price} changePercent={secondary.changePercent} forwardPrice={secondary.forwardPrice} timeframe={timeframe} candles={secondary.candles} liveCandle={secondary.liveCandle} loading={secondary.loading} onTimeframeChange={onTimeframeChange} />
        </div>
      </div>
      <div className="flex items-center justify-center py-1">
        <input
          aria-label="chart height"
          type="range"
          min={180}
          max={420}
          value={chartHeight}
          onChange={(event) => onChartHeightChange(Number(event.currentTarget.value))}
          className="h-2 w-28 cursor-row-resize accent-[var(--accent)]"
        />
        <span className="ml-2 font-mono text-[10px] text-[var(--dim)]">{chartHeight}px</span>
      </div>
    </section>
  );
}
