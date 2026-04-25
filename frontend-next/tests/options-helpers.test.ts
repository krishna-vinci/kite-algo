import { describe, expect, it } from "vitest";
import type { OptionLeg } from "@/components/options/types";
import { generatePayoffPoints, generatePayoffSummary, getStrategyPayoffAtExpiry } from "@/lib/options/payoff";
import {
  classifyStrategyFamily,
  getDefaultProtectionProfile,
  getStrategyProtectionProfile,
} from "@/lib/options/strategy-profile";

describe("strategy profile helpers", () => {
  it("classifies directional structures and maps index controls", () => {
    const legs: OptionLeg[] = [{ optionType: "call", side: "long", strike: 25000, premium: 100 }];

    expect(classifyStrategyFamily(legs)).toBe("directional");
    expect(getStrategyProtectionProfile(legs)).toEqual({
      family: "directional",
      triggers: ["index-stoploss", "index-target"],
      mandatoryIndexBracket: false,
      description: "Directional profile with index stoploss and index target.",
    });
  });

  it("classifies neutral short premium and marks index bracket mandatory", () => {
    const legs: OptionLeg[] = [
      { optionType: "call", side: "short", strike: 25300, premium: 120 },
      { optionType: "put", side: "short", strike: 25300, premium: 115 },
    ];

    expect(classifyStrategyFamily(legs)).toBe("neutral-short-premium");
    expect(getDefaultProtectionProfile("neutral-short-premium")).toEqual({
      family: "neutral-short-premium",
      triggers: ["index-stoploss", "combined-premium-target"],
      mandatoryIndexBracket: true,
      description: "Mandatory index bracket protection with combined premium profit target.",
    });
  });

  it("classifies long vol structures", () => {
    const legs: OptionLeg[] = [
      { optionType: "call", side: "long", strike: 25300, premium: 100 },
      { optionType: "put", side: "long", strike: 25300, premium: 90 },
    ];

    expect(classifyStrategyFamily(legs)).toBe("long-vol");
    expect(getStrategyProtectionProfile(legs)).toEqual({
      family: "long-vol",
      triggers: ["combined-premium-target", "combined-premium-stoploss"],
      mandatoryIndexBracket: false,
      description: "Premium-managed long volatility profile with premium target and premium stoploss.",
    });
  });

  it("classifies mixed-expiry structures as premium-managed", () => {
    const legs: OptionLeg[] = [
      { optionType: "call", side: "short", strike: 25300, premium: 120, expiryKey: "APR" },
      { optionType: "call", side: "long", strike: 25300, premium: 160, expiryKey: "MAY" },
    ];

    expect(classifyStrategyFamily(legs)).toBe("premium-managed-structure");
    expect(getStrategyProtectionProfile(legs)).toEqual({
      family: "premium-managed-structure",
      triggers: ["combined-premium-target", "combined-premium-stoploss"],
      mandatoryIndexBracket: false,
      description: "Premium-managed structure profile for calendars and mixed-expiry spreads.",
    });
  });
});

describe("payoff helpers", () => {
  it("calculates multi-leg payoff at expiry", () => {
    const legs: OptionLeg[] = [
      { optionType: "call", side: "short", strike: 100, premium: 10 },
      { optionType: "call", side: "long", strike: 110, premium: 4 },
    ];

    expect(getStrategyPayoffAtExpiry(legs, 90)).toBe(6);
    expect(getStrategyPayoffAtExpiry(legs, 105)).toBe(1);
    expect(getStrategyPayoffAtExpiry(legs, 120)).toBe(-4);
  });

  it("generates payoff points and summary metrics", () => {
    const legs: OptionLeg[] = [{ optionType: "call", side: "long", strike: 100, premium: 10 }];

    const points = generatePayoffPoints(legs, 80, 130, 10);
    expect(points).toEqual([
      { spot: 80, profitLoss: -10 },
      { spot: 90, profitLoss: -10 },
      { spot: 100, profitLoss: -10 },
      { spot: 110, profitLoss: 0 },
      { spot: 120, profitLoss: 10 },
      { spot: 130, profitLoss: 20 },
    ]);

    expect(generatePayoffSummary(legs, 80, 130, 10, 118)).toEqual({
      maxProfit: 20,
      maxLoss: -10,
      breakEvenSpots: [110],
      currentSpotProfitLoss: 10,
    });
  });

  it("interpolates break-even spots across sampled ranges", () => {
    const legs: OptionLeg[] = [{ optionType: "call", side: "long", strike: 100, premium: 7 }];

    expect(generatePayoffSummary(legs, 100, 120, 5, 111)).toEqual({
      maxProfit: 13,
      maxLoss: -7,
      breakEvenSpots: [107],
      currentSpotProfitLoss: 3,
    });
  });
});
