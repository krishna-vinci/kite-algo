import { describe, expect, it } from "vitest";

import { computePayoffProfile, type PayoffLeg } from "./payoff";

function leg(overrides: Partial<PayoffLeg>): PayoffLeg {
  return {
    id: overrides.id ?? Math.random().toString(36),
    optionType: "CE",
    side: "BUY",
    strike: 25000,
    premium: 100,
    lots: 1,
    lotSize: 1,
    ...overrides,
  };
}

describe("computePayoffProfile", () => {
  it("returns a flat zero profile for no legs", () => {
    const profile = computePayoffProfile([]);
    expect(profile.breakevens).toEqual([]);
    expect(profile.maxProfit).toBe(0);
    expect(profile.maxLoss).toBe(0);
  });

  it("computes long-straddle breakevens at strike ± total premium", () => {
    const strike = 25000;
    const callPremium = 120;
    const putPremium = 130;
    const legs = [
      leg({ id: "ce", optionType: "CE", side: "BUY", strike, premium: callPremium }),
      leg({ id: "pe", optionType: "PE", side: "BUY", strike, premium: putPremium }),
    ];

    const profile = computePayoffProfile(legs);

    expect(profile.breakevens).toEqual([strike - (callPremium + putPremium), strike + (callPremium + putPremium)]);
    // Unbounded profit on the upside (naked long call leg).
    expect(profile.maxProfit).toBeNull();
    // Max loss is the combined premium paid, at expiry exactly at the strike.
    expect(profile.maxLoss).toBe(-(callPremium + putPremium));
  });

  it("computes iron-condor breakevens and bounded max profit/loss", () => {
    // Long put wing / short put / short call / long call wing, 100-wide wings.
    const legs = [
      leg({ id: "long-put", optionType: "PE", side: "BUY", strike: 24700, premium: 20 }),
      leg({ id: "short-put", optionType: "PE", side: "SELL", strike: 24800, premium: 50 }),
      leg({ id: "short-call", optionType: "CE", side: "SELL", strike: 25200, premium: 55 }),
      leg({ id: "long-call", optionType: "CE", side: "BUY", strike: 25300, premium: 25 }),
    ];

    const profile = computePayoffProfile(legs);

    const netCredit = 50 + 55 - (20 + 25); // 60
    expect(profile.breakevens).toEqual([24800 - netCredit, 25200 + netCredit]);
    expect(profile.maxProfit).toBe(netCredit);
    expect(profile.maxLoss).toBe(-(100 - netCredit));
  });

  it("scales payoff by lots and lot size", () => {
    const legs = [leg({ optionType: "CE", side: "SELL", strike: 25000, premium: 100, lots: 2, lotSize: 75 })];
    const profile = computePayoffProfile(legs);
    // Max profit for a naked short call is the premium collected, at or below the strike.
    expect(profile.maxProfit).toBe(100 * 2 * 75);
    expect(profile.maxLoss).toBeNull();
  });

  it("aggregates net Greeks with BUY/SELL sign", () => {
    const legs = [
      leg({ id: "buy", side: "BUY", lots: 1, lotSize: 75, delta: 0.5, theta: -2 }),
      leg({ id: "sell", side: "SELL", lots: 1, lotSize: 75, delta: 0.3, theta: -1 }),
    ];
    const profile = computePayoffProfile(legs);
    expect(profile.netGreeks.delta).toBeCloseTo((0.5 - 0.3) * 75);
    expect(profile.netGreeks.theta).toBeCloseTo((-2 - -1) * 75);
  });
});
