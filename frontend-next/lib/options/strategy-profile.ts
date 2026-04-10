import type { OptionLeg, ProtectionProfile, StrategyFamily } from "@/components/options/types";

function getExpiryCount(legs: OptionLeg[]): number {
  return new Set(legs.map((leg) => leg.expiryKey ?? "same-expiry")).size;
}

export function classifyStrategyFamily(legs: OptionLeg[]): StrategyFamily {
  if (legs.length === 0) {
    return "directional";
  }

  if (getExpiryCount(legs) > 1) {
    return "premium-managed-structure";
  }

  const hasShortCall = legs.some((leg) => leg.side === "short" && leg.optionType === "call");
  const hasShortPut = legs.some((leg) => leg.side === "short" && leg.optionType === "put");
  if (hasShortCall && hasShortPut) {
    return "neutral-short-premium";
  }

  const hasLongCall = legs.some((leg) => leg.side === "long" && leg.optionType === "call");
  const hasLongPut = legs.some((leg) => leg.side === "long" && leg.optionType === "put");
  const hasShorts = legs.some((leg) => leg.side === "short");
  if (hasLongCall && hasLongPut && !hasShorts) {
    return "long-vol";
  }

  return "directional";
}

export function getDefaultProtectionProfile(family: StrategyFamily): ProtectionProfile {
  switch (family) {
    case "neutral-short-premium":
      return {
        family,
        triggers: ["index-stoploss", "combined-premium-target"],
        mandatoryIndexBracket: true,
        description: "Mandatory index bracket protection with combined premium profit target.",
      };
    case "long-vol":
      return {
        family,
        triggers: ["combined-premium-target", "combined-premium-stoploss"],
        mandatoryIndexBracket: false,
        description: "Premium-managed long volatility profile with premium target and premium stoploss.",
      };
    case "premium-managed-structure":
      return {
        family,
        triggers: ["combined-premium-target", "combined-premium-stoploss"],
        mandatoryIndexBracket: false,
        description: "Premium-managed structure profile for calendars and mixed-expiry spreads.",
      };
    case "directional":
    default:
      return {
        family: "directional",
        triggers: ["index-stoploss", "index-target"],
        mandatoryIndexBracket: false,
        description: "Directional profile with index stoploss and index target.",
      };
  }
}

export function getStrategyProtectionProfile(legs: OptionLeg[]): ProtectionProfile {
  return getDefaultProtectionProfile(classifyStrategyFamily(legs));
}
