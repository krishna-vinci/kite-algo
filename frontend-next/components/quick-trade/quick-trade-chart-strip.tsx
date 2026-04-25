"use client";

import type { CandlePoint, ChartTimeframe } from "@/components/options/types";
import { QuickTradeChartPanel } from "@/components/quick-trade/quick-trade-chart-panel";

type QuickTradeChartStripProps = Readonly<{
  chartHeight: number;
  splitPercent: number;
  timeframe: ChartTimeframe;
  onChartHeightChange: (next: number) => void;
  onSplitPercentChange: (next: number) => void;
  onTimeframeChange: (next: ChartTimeframe) => void;
  historyGeneration: number;
  primary: { label: string; price: number | null; changePercent: number | null; forwardPrice: number | null; candles: CandlePoint[]; liveCandle?: CandlePoint | null; loading?: boolean };
  secondary: { label: string; price: number | null; changePercent: number | null; forwardPrice: number | null; candles: CandlePoint[]; liveCandle?: CandlePoint | null; loading?: boolean };
  fillHeight?: boolean;
}>;

export function QuickTradeChartStrip({
  chartHeight,
  splitPercent,
  timeframe,
  onChartHeightChange,
  onSplitPercentChange,
  onTimeframeChange,
  historyGeneration,
  primary,
  secondary,
  fillHeight = false,
}: QuickTradeChartStripProps) {
  return (
    <section className={fillHeight ? "flex h-full flex-col px-1 pt-1" : "flex-none px-1 pt-1"}>
      <div className={`flex gap-2 ${fillHeight ? "min-h-0 flex-1" : ""}`} style={fillHeight ? undefined : { height: chartHeight }}>
        <div style={{ width: `${splitPercent}%` }} className="min-w-0">
          <QuickTradeChartPanel
            key={`${primary.label}:${timeframe}:${primary.liveCandle?.time ?? 0}:${primary.liveCandle?.close ?? 0}`}
            {...primary}
            timeframe={timeframe}
            historyGeneration={historyGeneration}
            onTimeframeChange={onTimeframeChange}
          />
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
          <QuickTradeChartPanel
            key={`${secondary.label}:${timeframe}:${secondary.liveCandle?.time ?? 0}:${secondary.liveCandle?.close ?? 0}`}
            {...secondary}
            timeframe={timeframe}
            historyGeneration={historyGeneration}
            onTimeframeChange={onTimeframeChange}
          />
        </div>
      </div>
      {!fillHeight && (
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
      )}
    </section>
  );
}
