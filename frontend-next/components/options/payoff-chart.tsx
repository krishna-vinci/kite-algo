import type { BuilderLeg } from "@/components/options/types";
import { generatePayoffPoints, summarizePayoff } from "@/lib/options/payoff";

type PayoffChartProps = Readonly<{
  legs: BuilderLeg[];
  currentSpot: number;
  sliderPercent: number;
  onSliderPercentChange: (value: number) => void;
  daysOffset: number;
  onDaysOffsetChange: (value: number) => void;
  maxDaysToExpiry: number;
}>;

function interpolatePoints(expiryPoints: ReturnType<typeof generatePayoffPoints>, progress: number) {
  return expiryPoints.map((point) => ({
    ...point,
    profitLoss: point.profitLoss * progress,
  }));
}

export function PayoffChart({
  legs,
  currentSpot,
  sliderPercent,
  onSliderPercentChange,
  daysOffset,
  onDaysOffsetChange,
  maxDaysToExpiry,
}: PayoffChartProps) {
  const minSpot = Math.round(currentSpot * 0.85);
  const maxSpot = Math.round(currentSpot * 1.15);
  const expiryPoints = generatePayoffPoints(legs, minSpot, maxSpot, Math.max(25, Math.round(currentSpot * 0.005)));
  const progress = maxDaysToExpiry <= 0 ? 1 : Math.min(1, Math.max(0, daysOffset / maxDaysToExpiry));
  const points = interpolatePoints(expiryPoints, progress);
  const scenarioSpot = currentSpot * (1 + sliderPercent / 100);
  const summary = summarizePayoff(points, scenarioSpot);
  const minPnl = Math.min(...points.map((point) => point.profitLoss), 0);
  const maxPnl = Math.max(...points.map((point) => point.profitLoss), 0);
  const range = maxPnl - minPnl || 1;
  const polyline = points
    .map((point, index) => {
      const x = (index / Math.max(points.length - 1, 1)) * 100;
      const y = 100 - ((point.profitLoss - minPnl) / range) * 100;
      return `${x},${y}`;
    })
    .join(" ");
  const scenarioX = ((scenarioSpot - minSpot) / Math.max(maxSpot - minSpot, 1)) * 100;

  return (
    <section className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/60 p-2">
      {/* Header: label + sliders */}
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[9px] uppercase tracking-[0.18em] text-[var(--dim)]">payoff</p>
        <label className="ml-auto flex items-center gap-1 text-[10px] text-[var(--muted)]">
          d{daysOffset}/{maxDaysToExpiry}
          <input aria-label="payoff day slider" type="range" min={0} max={maxDaysToExpiry} step={1} value={daysOffset} onChange={(event) => onDaysOffsetChange(Number(event.currentTarget.value))} className="w-14 accent-[var(--accent)]" />
        </label>
        <label className="flex items-center gap-1 text-[10px] text-[var(--muted)]">
          {sliderPercent >= 0 ? "+" : ""}{sliderPercent}%
          <input aria-label="payoff slider" type="range" min={-10} max={10} step={1} value={sliderPercent} onChange={(event) => onSliderPercentChange(Number(event.currentTarget.value))} className="w-14 accent-[var(--accent)]" />
        </label>
      </div>

      {/* Chart SVG — full width, compact height */}
      <div className="mt-1.5 rounded border border-[var(--border-soft)] bg-[var(--panel)] p-1.5">
        <svg viewBox="0 0 100 100" className="h-32 w-full" preserveAspectRatio="none" aria-label="payoff chart">
          <line x1="0" y1="50" x2="100" y2="50" stroke="rgba(255,255,255,0.15)" strokeWidth="0.4" />
          <polyline fill="none" stroke="#f97316" strokeWidth="1.2" points={polyline} />
          <line x1={scenarioX} y1="0" x2={scenarioX} y2="100" stroke="#60a5fa" strokeWidth="0.8" strokeDasharray="2 2" />
        </svg>
      </div>

      {/* Summary strip — horizontal, compact */}
      <div className="mt-1.5 grid grid-cols-4 gap-1.5 text-[10px]">
        <div className="rounded border border-[var(--border-soft)] bg-[var(--panel)] px-2 py-1.5">
          <p className="text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">max P</p>
          <p className="font-semibold text-[var(--green)]">₹{summary.maxProfit.toFixed(0)}</p>
        </div>
        <div className="rounded border border-[var(--border-soft)] bg-[var(--panel)] px-2 py-1.5">
          <p className="text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">max L</p>
          <p className="font-semibold text-[var(--red)]">₹{summary.maxLoss.toFixed(0)}</p>
        </div>
        <div className="rounded border border-[var(--border-soft)] bg-[var(--panel)] px-2 py-1.5">
          <p className="text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">BEs</p>
          <p className="text-[var(--text)]">{summary.breakEvenSpots.length ? summary.breakEvenSpots.join("/") : "—"}</p>
        </div>
        <div className="rounded border border-[var(--border-soft)] bg-[var(--panel)] px-2 py-1.5">
          <p className="text-[8px] uppercase tracking-[0.12em] text-[var(--dim)]">scene</p>
          <p className={`font-semibold ${summary.currentSpotProfitLoss >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>
            ₹{summary.currentSpotProfitLoss.toFixed(0)}
          </p>
        </div>
      </div>
    </section>
  );
}
