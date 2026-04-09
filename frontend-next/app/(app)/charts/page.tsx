import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";

const controls = ["1D", "5D", "1M", "1H", "15m"];
const overlays = ["VWAP", "EMA 20", "Volume", "Signals"];

const chartPoints = [
  [10, 65],
  [24, 58],
  [38, 63],
  [52, 49],
  [66, 54],
  [80, 44],
  [94, 47],
  [108, 38],
  [122, 43],
  [136, 31],
  [150, 36],
  [164, 29],
  [178, 34],
  [192, 26],
  [206, 32],
  [220, 23],
  [234, 28],
  [248, 18],
  [262, 25],
  [276, 21],
];

const candles = [
  [16, 30, 54],
  [36, 24, 49],
  [56, 34, 58],
  [76, 22, 44],
  [96, 18, 40],
  [116, 25, 47],
  [136, 20, 43],
  [156, 16, 38],
  [176, 12, 35],
  [196, 18, 39],
];

export default function ChartsPage() {
  const path = chartPoints.map(([x, y]) => `${x},${y}`).join(" ");

  return (
    <div className="space-y-4 pb-4">
      <Panel eyebrow="charts" title="Chart header" action={<StatusBadge tone="positive">lightweight mock</StatusBadge>}>
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <SectionLabel
            title="NIFTY 50 / spot"
            description="A terminal-style market view built from static data so the shell feels like a charting desk without depending on live chart state."
          />
          <div className="flex flex-wrap gap-2">
            {controls.map((control, index) => (
              <button
                key={control}
                type="button"
                className={[
                  "rounded-full border px-3 py-2 text-xs font-medium uppercase tracking-[0.24em] transition-colors",
                  index === 0
                    ? "border-primary/40 bg-primary/10 text-primary"
                    : "border-border/70 bg-background/60 text-foreground/70 hover:border-primary/25 hover:text-foreground",
                ].join(" ")}
              >
                {control}
              </button>
            ))}
          </div>
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(280px,0.6fr)]">
        <Panel eyebrow="price action" title="Lightweight-charts placeholder">
          <div className="rounded-[1.5rem] border border-border/70 bg-background/70 p-4">
            <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
              {overlays.map((overlay, index) => (
                <StatusBadge key={overlay} tone={index === 0 ? "positive" : "neutral"}>
                  {overlay}
                </StatusBadge>
              ))}
            </div>

            <div className="relative h-[340px] overflow-hidden rounded-[1.25rem] border border-border/60 bg-[linear-gradient(180deg,rgba(255,255,255,0.03),transparent_22%),linear-gradient(90deg,rgba(255,255,255,0.02),transparent_22%)]">
              <svg viewBox="0 0 300 140" className="absolute inset-0 h-full w-full" preserveAspectRatio="none" aria-hidden="true">
                <defs>
                  <linearGradient id="chartGradient" x1="0" x2="0" y1="0" y2="1">
                    <stop offset="0%" stopColor="rgb(132 204 22 / 0.35)" />
                    <stop offset="100%" stopColor="rgb(132 204 22 / 0.02)" />
                  </linearGradient>
                </defs>
                <polyline fill="none" stroke="rgb(132 204 22)" strokeWidth="2.5" points={path} />
                <polyline fill="url(#chartGradient)" stroke="none" points={`10,120 ${path} 290,124`} />
                {candles.map(([x, low, high]) => (
                  <g key={x} stroke="rgb(191 219 254 / 0.8)">
                    <line x1={x} y1={low} x2={x} y2={high} strokeWidth="1.5" />
                    <rect x={x - 3} y={Math.min(low, high) + 4} width="6" height="14" rx="1.5" fill="rgb(191 219 254 / 0.28)" />
                  </g>
                ))}
                {[24, 48, 72, 96, 120].map((y) => (
                  <line key={y} x1="0" x2="300" y1={y} y2={y} stroke="rgb(255 255 255 / 0.04)" />
                ))}
              </svg>

              <div className="absolute left-4 top-4 rounded-2xl border border-border/70 bg-background/80 px-3 py-2 font-mono text-xs text-primary">
                25,304.60
              </div>
              <div className="absolute right-4 top-4 rounded-2xl border border-border/70 bg-background/80 px-3 py-2 text-xs text-foreground/70">
                mocked lightweight chart canvas
              </div>
              <div className="absolute bottom-4 left-4 right-4 flex items-center justify-between text-[10px] uppercase tracking-[0.3em] text-foreground/40">
                <span>09:15</span>
                <span>11:00</span>
                <span>13:30</span>
                <span>15:25</span>
              </div>
            </div>
          </div>
        </Panel>

        <Panel eyebrow="readout" title="Chart context">
          <div className="space-y-3">
            {[
              ["Trend", "up but narrowing"],
              ["Session", "morning range break"],
              ["Volume", "above average"],
              ["Trigger", "watch 25,280 support"],
            ].map(([label, value]) => (
              <div key={label} className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
                <p className="mt-2 font-mono text-sm text-primary">{value}</p>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
