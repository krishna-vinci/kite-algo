/**
 * Pure client-side payoff-at-expiry math for the option-chain payoff builder.
 * No network calls, no framework imports — safe to unit test in isolation.
 *
 * The payoff curve is piecewise linear in spot with kinks only at each leg's
 * strike, so every quantity below (breakevens, max profit/loss) can be
 * computed exactly from the finite set of strikes plus the two boundary rays
 * (spot = 0, and spot beyond the highest strike), rather than by sampling.
 */

export type OptionType = "CE" | "PE";
export type OptionSide = "BUY" | "SELL";

export type PayoffLeg = {
  id: string;
  optionType: OptionType;
  side: OptionSide;
  strike: number;
  premium: number;
  lots: number;
  lotSize: number;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
};

export type PayoffPoint = { spot: number; pnl: number };

export type NetGreeks = { delta: number; gamma: number; theta: number; vega: number };

export type PayoffProfile = {
  points: PayoffPoint[];
  breakevens: number[];
  /** Highest value of the payoff curve; `null` means unbounded (profit runs to infinity). */
  maxProfit: number | null;
  /** Lowest value of the payoff curve (typically negative); `null` means unbounded loss. */
  maxLoss: number | null;
  netGreeks: NetGreeks;
};

const EPSILON = 1e-6;

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

export function intrinsicValue(optionType: OptionType, strike: number, spot: number): number {
  return optionType === "CE" ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0);
}

/** Net P&L of a single leg at expiry, at a given spot price. */
export function legPayoff(leg: PayoffLeg, spot: number): number {
  const intrinsic = intrinsicValue(leg.optionType, leg.strike, spot);
  const perUnit = leg.side === "BUY" ? intrinsic - leg.premium : leg.premium - intrinsic;
  return perUnit * leg.lots * leg.lotSize;
}

/** Total net P&L of all legs at expiry, at a given spot price. */
export function totalPayoff(legs: PayoffLeg[], spot: number): number {
  return legs.reduce((total, leg) => total + legPayoff(leg, spot), 0);
}

function netGreeksOf(legs: PayoffLeg[]): NetGreeks {
  return legs.reduce<NetGreeks>(
    (acc, leg) => {
      const sign = leg.side === "BUY" ? 1 : -1;
      const qty = leg.lots * leg.lotSize * sign;
      return {
        delta: acc.delta + (leg.delta ?? 0) * qty,
        gamma: acc.gamma + (leg.gamma ?? 0) * qty,
        theta: acc.theta + (leg.theta ?? 0) * qty,
        vega: acc.vega + (leg.vega ?? 0) * qty,
      };
    },
    { delta: 0, gamma: 0, theta: 0, vega: 0 },
  );
}

/**
 * Computes the full payoff-at-expiry profile: chart points, breakevens, max
 * profit/loss and net Greeks for the selected legs.
 *
 * `chartUpperSpot` optionally bounds the chart's right edge (e.g. for
 * rendering); it never affects breakeven/max-profit correctness, which is
 * derived analytically from the tail slope beyond the highest strike.
 */
export function computePayoffProfile(legs: PayoffLeg[], chartUpperSpot?: number): PayoffProfile {
  if (legs.length === 0) {
    return { points: [], breakevens: [], maxProfit: 0, maxLoss: 0, netGreeks: netGreeksOf(legs) };
  }

  const strikes = Array.from(new Set(legs.map((leg) => leg.strike))).sort((a, b) => a - b);
  const minStrike = strikes[0];
  const maxStrike = strikes[strikes.length - 1];
  const spread = maxStrike - minStrike || maxStrike || 100;
  const chartHi = chartUpperSpot ?? maxStrike + spread + 100;

  const yAt = (x: number) => totalPayoff(legs, x);

  // Tail slope beyond the highest strike: the curve is linear there (no more
  // kinks), so two points on the ray fully determine it.
  const rightSlope = yAt(maxStrike + 1) - yAt(maxStrike);
  const rightUnbounded = Math.abs(rightSlope) > 1e-9;

  // Breakpoints for the *bounded* domain: spot can't go below 0, and every
  // strike is a kink. The segment between consecutive breakpoints is exact
  // (no hidden kinks), so linear interpolation on each segment is exact too.
  const breakpoints = Array.from(new Set([0, ...strikes.filter((s) => s > 0), chartHi])).sort(
    (a, b) => a - b,
  );

  const breakevens: number[] = [];
  for (let i = 0; i < breakpoints.length - 1; i += 1) {
    const x1 = breakpoints[i];
    const x2 = breakpoints[i + 1];
    const y1 = yAt(x1);
    const y2 = yAt(x2);
    if (Math.abs(y1) < EPSILON) breakevens.push(round2(x1));
    if (y1 * y2 < 0) {
      const x = x1 + ((0 - y1) * (x2 - x1)) / (y2 - y1);
      breakevens.push(round2(x));
    }
  }
  const yHi = yAt(chartHi);
  if (Math.abs(yHi) < EPSILON) breakevens.push(round2(chartHi));

  // Breakeven strictly beyond the last strike, in the unbounded tail.
  if (rightUnbounded) {
    const yMax = yAt(maxStrike);
    const x = maxStrike + (0 - yMax) / rightSlope;
    if (x >= maxStrike - EPSILON) breakevens.push(round2(x));
  }

  const dedupedBreakevens = Array.from(
    new Set(breakevens.filter((b) => b >= 0).map((b) => round2(b))),
  ).sort((a, b) => a - b);

  const candidateXs = [0, ...strikes, chartHi];
  const candidateYs = candidateXs.map(yAt);
  const maxProfit = rightSlope > 1e-9 ? null : round2(Math.max(...candidateYs));
  const maxLoss = rightSlope < -1e-9 ? null : round2(Math.min(...candidateYs));

  const points: PayoffPoint[] = breakpoints.map((spot) => ({ spot, pnl: round2(yAt(spot)) }));

  return {
    points,
    breakevens: dedupedBreakevens,
    maxProfit,
    maxLoss,
    netGreeks: netGreeksOf(legs),
  };
}
