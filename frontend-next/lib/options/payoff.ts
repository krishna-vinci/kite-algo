import type { OptionLeg, PayoffPoint, PayoffSummary } from "@/components/options/types";

function getLegUnits(leg: OptionLeg): number {
  return (leg.quantity ?? 1) * (leg.contractSize ?? 1);
}

export function getLegPayoffAtExpiry(leg: OptionLeg, spot: number): number {
  const intrinsic =
    leg.optionType === "call"
      ? Math.max(spot - leg.strike, 0)
      : Math.max(leg.strike - spot, 0);

  const unitPayoff = leg.side === "long" ? intrinsic - leg.premium : leg.premium - intrinsic;
  return unitPayoff * getLegUnits(leg);
}

export function getStrategyPayoffAtExpiry(legs: OptionLeg[], spot: number): number {
  return legs.reduce((total, leg) => total + getLegPayoffAtExpiry(leg, spot), 0);
}

export function generatePayoffPoints(
  legs: OptionLeg[],
  spotMin: number,
  spotMax: number,
  step: number,
): PayoffPoint[] {
  if (step <= 0) {
    throw new Error("step must be greater than 0");
  }

  if (spotMax < spotMin) {
    throw new Error("spotMax must be greater than or equal to spotMin");
  }

  const points: PayoffPoint[] = [];
  for (let spot = spotMin; spot <= spotMax; spot += step) {
    points.push({ spot, profitLoss: getStrategyPayoffAtExpiry(legs, spot) });
  }

  if (points.at(-1)?.spot !== spotMax) {
    points.push({ spot: spotMax, profitLoss: getStrategyPayoffAtExpiry(legs, spotMax) });
  }

  return points;
}

function roundToTwo(value: number): number {
  return Number(value.toFixed(2));
}

function getBreakEvenSpots(points: PayoffPoint[]): number[] {
  const breakEvens: number[] = [];

  for (let index = 0; index < points.length; index += 1) {
    const point = points[index];
    if (point.profitLoss === 0) {
      breakEvens.push(roundToTwo(point.spot));
      continue;
    }

    const nextPoint = points[index + 1];
    if (!nextPoint) {
      continue;
    }

    if (point.profitLoss * nextPoint.profitLoss < 0) {
      const slope = nextPoint.profitLoss - point.profitLoss;
      const ratio = (0 - point.profitLoss) / slope;
      const interpolated = point.spot + (nextPoint.spot - point.spot) * ratio;
      breakEvens.push(roundToTwo(interpolated));
    }
  }

  return [...new Set(breakEvens)];
}

export function summarizePayoff(points: PayoffPoint[], currentSpot: number): PayoffSummary {
  if (points.length === 0) {
    return {
      maxProfit: 0,
      maxLoss: 0,
      breakEvenSpots: [],
      currentSpotProfitLoss: 0,
    };
  }

  const profitLossValues = points.map((point) => point.profitLoss);
  const currentSpotPoint = points.reduce((closest, point) => {
    const currentDistance = Math.abs(point.spot - currentSpot);
    const closestDistance = Math.abs(closest.spot - currentSpot);
    return currentDistance < closestDistance ? point : closest;
  }, points[0]);

  return {
    maxProfit: Math.max(...profitLossValues),
    maxLoss: Math.min(...profitLossValues),
    breakEvenSpots: getBreakEvenSpots(points),
    currentSpotProfitLoss: currentSpotPoint.profitLoss,
  };
}

export function generatePayoffSummary(
  legs: OptionLeg[],
  spotMin: number,
  spotMax: number,
  step: number,
  currentSpot: number,
): PayoffSummary {
  return summarizePayoff(generatePayoffPoints(legs, spotMin, spotMax, step), currentSpot);
}
