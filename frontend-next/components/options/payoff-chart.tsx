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
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--bg)]/60 p-3">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <p className="text-[10px] uppercase tracking-[0.24em] text-[var(--dim)]">payoff</p>
          <h3 className="mt-1 text-sm font-semibold text-[var(--text)]">Payoff chart with day and scenario sliders</h3>
        </div>
        <label className="ml-auto flex items-center gap-2 text-[11px] text-[var(--muted)]">
          day {daysOffset}/{maxDaysToExpiry}
          <input aria-label="payoff day slider" type="range" min={0} max={maxDaysToExpiry} step={1} value={daysOffset} onChange={(event) => onDaysOffsetChange(Number(event.currentTarget.value))} className="accent-[var(--accent)]" />
        </label>
        <label className="flex items-center gap-2 text-[11px] text-[var(--muted)]">
          scenario {sliderPercent >= 0 ? "+" : ""}
          {sliderPercent}%
          <input aria-label="payoff slider" type="range" min={-10} max={10} step={1} value={sliderPercent} onChange={(event) => onSliderPercentChange(Number(event.currentTarget.value))} className="accent-[var(--accent)]" />
        </label>
      </div>
      <div className="mt-3 grid gap-3 lg:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--panel)] p-3">
          <svg viewBox="0 0 100 100" className="h-52 w-full" preserveAspectRatio="none" aria-label="payoff chart">
            <line x1="0" y1="50" x2="100" y2="50" stroke="rgba(255,255,255,0.15)" strokeWidth="0.4" />
            <polyline fill="none" stroke="#f97316" strokeWidth="1.2" points={polyline} />
            <line x1={scenarioX} y1="0" x2={scenarioX} y2="100" stroke="#60a5fa" strokeWidth="0.8" strokeDasharray="2 2" />
          </svg>
        </div>
        <div className="grid gap-2">
          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--panel)] p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">max profit</p>
            <p className="mt-2 text-lg font-semibold text-[var(--green)]">₹{summary.maxProfit.toFixed(2)}</p>
          </div>
          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--panel)] p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">max loss</p>
            <p className="mt-2 text-lg font-semibold text-[var(--red)]">₹{summary.maxLoss.toFixed(2)}</p>
          </div>
          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--panel)] p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">break-evens</p>
            <p className="mt-2 text-sm text-[var(--text)]">{summary.breakEvenSpots.length ? summary.breakEvenSpots.join(" / ") : "Pending"}</p>
          </div>
          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--panel)] p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">scenario pnl</p>
            <p className={`mt-2 text-lg font-semibold ${summary.currentSpotProfitLoss >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>
              ₹{summary.currentSpotProfitLoss.toFixed(2)}
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
