// features/alerts/components/price-ladder.tsx
/**
 * The price-vs-target track: pure decoration on numbers the page already has.
 * It computes nothing authoritative — the markers are positioned inside a
 * padded window that always contains both values.
 */

import {
  formatPrice,
  type TargetDistance,
} from "@/features/alerts/lib/plain-language";

export type LadderRange = { min: number; max: number };

export function ladderRange(price: number | null, target: number | null): LadderRange | null {
  if (price === null || !Number.isFinite(price)) return null;
  if (target === null || !Number.isFinite(target)) {
    return { min: price * 0.95, max: price * 1.05 };
  }
  const span = Math.abs(target - price);
  const pad = Math.max(span * 0.25, Math.abs(price) * 0.01);
  return { min: Math.min(price, target) - pad, max: Math.max(price, target) + pad };
}

function positionOf(value: number, range: LadderRange): number {
  const pct = ((value - range.min) / (range.max - range.min)) * 100;
  return Math.min(100, Math.max(0, pct));
}

export function PriceLadder({
  price,
  target,
  distance,
}: {
  price: number | null;
  target: number | null;
  distance: TargetDistance | null;
}) {
  const range = ladderRange(price, target);
  if (!range) {
    return <p className="text-xs text-muted-foreground">Waiting for the first price…</p>;
  }
  return (
    <div className="flex flex-col gap-1.5">
      <div className="relative h-2 rounded-full bg-border/60" role="presentation">
        <span
          data-testid="ladder-price-marker"
          className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary"
          style={{ left: `${positionOf(price ?? range.min, range)}%` }}
        />
        {target !== null ? (
          <span
            data-testid="ladder-target-marker"
            className="absolute top-1/2 h-4 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded bg-amber-400"
            style={{ left: `${positionOf(target, range)}%` }}
          />
        ) : null}
      </div>
      {distance ? (
        <p className="text-xs text-muted-foreground">{distance.sentence}</p>
      ) : (
        <p className="text-xs text-muted-foreground">Set a level to see the distance.</p>
      )}
      <p className="text-xs tabular-nums text-muted-foreground/70">
        {formatPrice(range.min)} — {formatPrice(range.max)}
      </p>
    </div>
  );
}
