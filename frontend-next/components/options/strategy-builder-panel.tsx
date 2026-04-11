"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import type {
  BuilderLeg,
  CanonicalStrategyPreview,
  DryRunPlan,
  MiniChainSnapshot,
  StrategyRuleInputDescriptor,
  StrategyTemplateConfig,
  Underlying,
} from "@/components/options/types";
import { PayoffChart } from "@/components/options/payoff-chart";
import { buildPositionDryRun, executePaperOptionStrategy, previewOptionStrategy } from "@/lib/options/api";

type StrategyBuilderPanelProps = Readonly<{
  underlying: Underlying;
  expiry: string;
  currentSpot: number;
  chain: MiniChainSnapshot | null;
  appAuthenticated: boolean;
  paperAvailable: boolean;
}>;

type RuleFieldKey =
  | "index_lower_boundary"
  | "index_upper_boundary"
  | "combined_premium_target"
  | "combined_premium_stoploss"
  | "basket_mtm_target"
  | "basket_mtm_stoploss";

const FIELD_ORDER: RuleFieldKey[] = [
  "index_lower_boundary",
  "index_upper_boundary",
  "combined_premium_target",
  "combined_premium_stoploss",
  "basket_mtm_target",
  "basket_mtm_stoploss",
];

const GROUP_ORDER = {
  primary: 0,
  emergency: 1,
  secondary: 2,
} as const;

const templates: StrategyTemplateConfig[] = [
  {
    id: "short_straddle",
    label: "Short Straddle",
    description: "ATM short premium neutral structure.",
    strategyType: "straddle",
    legBlueprints: [
      { optionType: "call", side: "short", strikeOffset: 0 },
      { optionType: "put", side: "short", strikeOffset: 0 },
    ],
  },
  {
    id: "short_strangle",
    label: "Short Strangle",
    description: "OTM short premium with wider neutral range.",
    strategyType: "strangle",
    legBlueprints: [
      { optionType: "call", side: "short", strikeOffset: 4 },
      { optionType: "put", side: "short", strikeOffset: -4 },
    ],
  },
  {
    id: "long_straddle",
    label: "Long Straddle",
    description: "Long vol around ATM.",
    strategyType: "straddle",
    legBlueprints: [
      { optionType: "call", side: "long", strikeOffset: 0 },
      { optionType: "put", side: "long", strikeOffset: 0 },
    ],
  },
  {
    id: "long_strangle",
    label: "Long Strangle",
    description: "Long volatility with lower entry premium than a straddle.",
    strategyType: "strangle",
    legBlueprints: [
      { optionType: "call", side: "long", strikeOffset: 4 },
      { optionType: "put", side: "long", strikeOffset: -4 },
    ],
  },
  {
    id: "buy_call",
    label: "Buy Call",
    description: "Directional bullish single-leg structure.",
    strategyType: "single_leg",
    legBlueprints: [{ optionType: "call", side: "long", strikeOffset: 0 }],
  },
  {
    id: "sell_put",
    label: "Sell Put",
    description: "Directional bullish short premium single-leg structure.",
    strategyType: "single_leg",
    legBlueprints: [{ optionType: "put", side: "short", strikeOffset: 0 }],
  },
  {
    id: "bull_call_spread",
    label: "Bull Call Spread",
    description: "Directional debit spread with capped upside.",
    strategyType: "single_leg",
    legBlueprints: [
      { optionType: "call", side: "long", strikeOffset: 0 },
      { optionType: "call", side: "short", strikeOffset: 2 },
    ],
  },
  {
    id: "bear_put_spread",
    label: "Bear Put Spread",
    description: "Directional debit spread for downside view.",
    strategyType: "single_leg",
    legBlueprints: [
      { optionType: "put", side: "long", strikeOffset: 0 },
      { optionType: "put", side: "short", strikeOffset: -2 },
    ],
  },
  {
    id: "iron_condor",
    label: "Iron Condor",
    description: "Defined-risk neutral short premium structure.",
    strategyType: "iron_condor",
    legBlueprints: [
      { optionType: "call", side: "short", strikeOffset: 2 },
      { optionType: "call", side: "long", strikeOffset: 4 },
      { optionType: "put", side: "short", strikeOffset: -2 },
      { optionType: "put", side: "long", strikeOffset: -4 },
    ],
  },
];

function getStrikeGap(underlying: Underlying) {
  return underlying === "BANKNIFTY" ? 100 : 50;
}

function toBuilderLeg(chain: MiniChainSnapshot, optionType: "call" | "put", side: "long" | "short", strike: number): BuilderLeg | null {
  const row = chain.strikes.find((item) => item.strike === strike);
  const source = optionType === "call" ? row?.ce : row?.pe;
  if (!row || !source) {
    return null;
  }
  return {
    optionType,
    side,
    strike,
    premium: source.ltp,
    quantity: 1,
    contractSize: source.lotSize,
    instrumentToken: source.instrumentToken,
    tradingSymbol: source.tradingSymbol,
    lotSize: source.lotSize,
    delta: source.delta,
    gamma: source.gamma,
    theta: source.theta,
    vega: source.vega,
    iv: source.iv,
    oi: source.oi,
  };
}

function greekCell(label: string, value: number, color?: string) {
  const cls = color ?? "text-[var(--text)]";
  return (
    <span className="text-[var(--dim)]">
      {label} <span className={cls}>{value.toFixed(2)}</span>
    </span>
  );
}

export function StrategyBuilderPanel({ underlying, expiry, currentSpot, chain, appAuthenticated, paperAvailable }: StrategyBuilderPanelProps) {
  const [templateId, setTemplateId] = useState(templates[0].id);
  const [legs, setLegs] = useState<BuilderLeg[]>([]);
  const [lotMultiplier, setLotMultiplier] = useState(1);
  const [sliderPercent, setSliderPercent] = useState(0);
  const [daysOffset, setDaysOffset] = useState(0);
  const [indexLowerBoundary, setIndexLowerBoundary] = useState<number | "">("");
  const [indexUpperBoundary, setIndexUpperBoundary] = useState<number | "">("");
  const [combinedPremiumTarget, setCombinedPremiumTarget] = useState<number | "">("");
  const [combinedPremiumStoploss, setCombinedPremiumStoploss] = useState<number | "">("");
  const [basketMtmTarget, setBasketMtmTarget] = useState<number | "">("");
  const [basketMtmStoploss, setBasketMtmStoploss] = useState<number | "">("");
  const [touchedInputs, setTouchedInputs] = useState<Record<string, boolean>>({});
  const touchedInputsRef = useRef<Record<string, boolean>>({});
  touchedInputsRef.current = touchedInputs;
  const [strategyPreview, setStrategyPreview] = useState<CanonicalStrategyPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [plan, setPlan] = useState<DryRunPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [paperSubmitting, setPaperSubmitting] = useState(false);
  const [profileExpanded, setProfileExpanded] = useState(false);
  const [maxDaysToExpiry, setMaxDaysToExpiry] = useState(1);
  const previewPayloadRef = useRef<{ selectedLegs: Array<Record<string, unknown>>; currentSpot: number }>({ selectedLegs: [], currentSpot });
  const structureChainRef = useRef<MiniChainSnapshot | null>(null);

  const template = templates.find((item) => item.id === templateId) ?? templates[0];
  structureChainRef.current = chain;

  // Track the structural identity for full-reset detection
  const chainStructureKey = chain ? `${chain.underlying}|${chain.expiry}|${chain.atmStrike}` : "";
  const prevChainStructureKeyRef = useRef(chainStructureKey);

  // Full structural reset: only when template, underlying, expiry, or ATM strike actually changes
  useEffect(() => {
    const structureChain = structureChainRef.current;
    if (!structureChain) {
      return;
    }
    const gap = getStrikeGap(underlying);
    const nextLegs = template.legBlueprints
      .map((blueprint) => toBuilderLeg(structureChain, blueprint.optionType, blueprint.side, structureChain.atmStrike + blueprint.strikeOffset * gap))
      .filter((value): value is BuilderLeg => Boolean(value));
    setLegs(nextLegs);
    setPlan(null);
    setStrategyPreview(null);
    setPreviewError(null);
    setTouchedInputs({});
    setIndexLowerBoundary("");
    setIndexUpperBoundary("");
    setCombinedPremiumTarget("");
    setCombinedPremiumStoploss("");
    setBasketMtmTarget("");
    setBasketMtmStoploss("");
    setProfileExpanded(false);
    prevChainStructureKeyRef.current = chainStructureKey;
  // eslint-disable-next-line react-hooks/exhaustive-deps -- only reset on structural identity changes, not chain reference
  }, [chainStructureKey, template.id, underlying]);

  // Live greeks/premium refresh: update existing legs in-place when chain ticks arrive.
  useEffect(() => {
    if (!chain) {
      return;
    }
    // Skip if a structural reset just happened in the same render cycle
    if (prevChainStructureKeyRef.current !== chainStructureKey) {
      return;
    }
    setLegs((current) => {
      let changed = false;
      const updated = current.map((leg) => {
        const refreshed = toBuilderLeg(chain, leg.optionType, leg.side, leg.strike);
        if (!refreshed) {
          return leg;
        }
        if (
          refreshed.premium === leg.premium &&
          refreshed.delta === leg.delta &&
          refreshed.gamma === leg.gamma &&
          refreshed.theta === leg.theta &&
          refreshed.vega === leg.vega &&
          refreshed.iv === leg.iv &&
          refreshed.oi === leg.oi
        ) {
          return leg;
        }
        changed = true;
        return {
          ...leg,
          premium: refreshed.premium,
          instrumentToken: refreshed.instrumentToken,
          tradingSymbol: refreshed.tradingSymbol,
          lotSize: refreshed.lotSize,
          delta: refreshed.delta,
          gamma: refreshed.gamma,
          theta: refreshed.theta,
          vega: refreshed.vega,
          iv: refreshed.iv,
          oi: refreshed.oi,
        };
      });
      return changed ? updated : current;
    });
  }, [chain, chainStructureKey]);

  useEffect(() => {
    const expiryCloseTs = new Date(`${expiry}T15:30:00+05:30`).getTime();
    if (!Number.isFinite(expiryCloseTs)) {
      setMaxDaysToExpiry(1);
      return;
    }
    const remainingDays = Math.ceil((expiryCloseTs - Date.now()) / (1000 * 60 * 60 * 24));
    setMaxDaysToExpiry(Math.max(1, remainingDays));
  }, [expiry]);

  const aggregateGreeks = useMemo(
    () =>
      legs.reduce(
        (accumulator, leg) => {
          const sign = leg.side === "long" ? 1 : -1;
          const quantity = lotMultiplier * leg.lotSize;
          return {
            delta: accumulator.delta + (leg.delta ?? 0) * sign * quantity,
            gamma: accumulator.gamma + (leg.gamma ?? 0) * sign * quantity,
            theta: accumulator.theta + (leg.theta ?? 0) * sign * quantity,
            vega: accumulator.vega + (leg.vega ?? 0) * sign * quantity,
          };
        },
        { delta: 0, gamma: 0, theta: 0, vega: 0 },
      ),
    [legs, lotMultiplier],
  );

  const backendLegs = useMemo(
    () =>
      legs.map((leg) => ({
        instrument_token: leg.instrumentToken,
        tradingsymbol: leg.tradingSymbol,
        strike: leg.strike,
        option_type: leg.optionType === "call" ? "CE" : "PE",
        ltp: leg.premium,
        lot_size: leg.lotSize,
        delta: leg.delta ?? 0,
        lots: lotMultiplier,
        transaction_type: leg.side === "long" ? "BUY" : "SELL",
      })),
    [legs, lotMultiplier],
  );

  useEffect(() => {
    previewPayloadRef.current = { selectedLegs: backendLegs, currentSpot };
  }, [backendLegs, currentSpot]);

  const protectionConfig = useMemo(
    () => ({
      enabled: true,
      index_lower_boundary: indexLowerBoundary === "" ? undefined : Number(indexLowerBoundary),
      index_upper_boundary: indexUpperBoundary === "" ? undefined : Number(indexUpperBoundary),
      combined_premium_target: combinedPremiumTarget === "" ? undefined : Number(combinedPremiumTarget),
      combined_premium_stoploss: combinedPremiumStoploss === "" ? undefined : Number(combinedPremiumStoploss),
      basket_mtm_target: basketMtmTarget === "" ? undefined : Number(basketMtmTarget),
      basket_mtm_stoploss: basketMtmStoploss === "" ? undefined : Number(basketMtmStoploss),
    }),
    [indexLowerBoundary, indexUpperBoundary, combinedPremiumTarget, combinedPremiumStoploss, basketMtmTarget, basketMtmStoploss],
  );

  const previewRequestKey = useMemo(
    () => JSON.stringify({
      underlying,
      expiry,
      templateId: template.id,
      strategyType: template.strategyType,
      lotMultiplier,
      legs: legs.map((leg) => ({ strike: leg.strike, optionType: leg.optionType, side: leg.side, token: leg.instrumentToken })),
      protectionConfig,
    }),
    [underlying, expiry, template.id, template.strategyType, lotMultiplier, legs, protectionConfig],
  );

  const applyPreviewDefaults = useCallback((preview: CanonicalStrategyPreview) => {
    const touched = touchedInputsRef.current;
    const applyValue = (key: RuleFieldKey, setter: (value: number | "") => void) => {
      if (touched[key]) {
        return;
      }
      const nextValue = preview.inputs[key]?.value;
      setter(nextValue == null ? "" : Number(nextValue));
    };

    applyValue("index_lower_boundary", setIndexLowerBoundary);
    applyValue("index_upper_boundary", setIndexUpperBoundary);
    applyValue("combined_premium_target", setCombinedPremiumTarget);
    applyValue("combined_premium_stoploss", setCombinedPremiumStoploss);
    applyValue("basket_mtm_target", setBasketMtmTarget);
    applyValue("basket_mtm_stoploss", setBasketMtmStoploss);
  }, []);

  useEffect(() => {
    if (!expiry || previewPayloadRef.current.selectedLegs.length === 0) {
      setStrategyPreview(null);
      setPreviewError(null);
      return;
    }

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setPreviewLoading(true);
      try {
        const previewPayload = previewPayloadRef.current;
        const preview = await previewOptionStrategy({
          underlying,
          expiry,
          strategyType: template.strategyType,
          templateId: template.id,
          selectedLegs: previewPayload.selectedLegs,
          protectionConfig,
          currentSpot: previewPayload.currentSpot,
        });
        if (cancelled) {
          return;
        }
        setStrategyPreview(preview);
        setPreviewError(null);
        applyPreviewDefaults(preview);
      } catch (error) {
        if (cancelled) {
          return;
        }
        setPreviewError(error instanceof Error ? error.message : "Backend preview unavailable");
      } finally {
        if (!cancelled) {
          setPreviewLoading(false);
        }
      }
    }, 250);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [underlying, expiry, template.id, template.strategyType, protectionConfig, applyPreviewDefaults, previewRequestKey]);

  const visibleInputs = useMemo(() => {
    if (!strategyPreview) {
      return [] as StrategyRuleInputDescriptor[];
    }
    return Object.values(strategyPreview.inputs)
      .filter((input) => input.visible)
      .sort((left, right) => {
        const groupDelta = GROUP_ORDER[left.group] - GROUP_ORDER[right.group];
        if (groupDelta !== 0) {
          return groupDelta;
        }
        return FIELD_ORDER.indexOf(left.key as RuleFieldKey) - FIELD_ORDER.indexOf(right.key as RuleFieldKey);
      });
  }, [strategyPreview]);

  const groupedInputs = useMemo(
    () => ({
      primary: visibleInputs.filter((input) => input.group === "primary"),
      emergency: visibleInputs.filter((input) => input.group === "emergency"),
      secondary: visibleInputs.filter((input) => input.group === "secondary"),
    }),
    [visibleInputs],
  );

  function inputValue(key: RuleFieldKey) {
    switch (key) {
      case "index_lower_boundary":
        return indexLowerBoundary;
      case "index_upper_boundary":
        return indexUpperBoundary;
      case "combined_premium_target":
        return combinedPremiumTarget;
      case "combined_premium_stoploss":
        return combinedPremiumStoploss;
      case "basket_mtm_target":
        return basketMtmTarget;
      case "basket_mtm_stoploss":
        return basketMtmStoploss;
    }
  }

  function setInputValue(key: RuleFieldKey, rawValue: string) {
    const nextValue = rawValue ? Number(rawValue) : "";
    setTouchedInputs((current) => ({ ...current, [key]: true }));
    setPlan(null);
    switch (key) {
      case "index_lower_boundary":
        setIndexLowerBoundary(nextValue);
        return;
      case "index_upper_boundary":
        setIndexUpperBoundary(nextValue);
        return;
      case "combined_premium_target":
        setCombinedPremiumTarget(nextValue);
        return;
      case "combined_premium_stoploss":
        setCombinedPremiumStoploss(nextValue);
        return;
      case "basket_mtm_target":
        setBasketMtmTarget(nextValue);
        return;
      case "basket_mtm_stoploss":
        setBasketMtmStoploss(nextValue);
    }
  }

  async function handleDryRun() {
    if (!expiry || backendLegs.length === 0) {
      return;
    }
    setPlanning(true);
    try {
      const nextPlan = await buildPositionDryRun({
        underlying,
        expiry,
        strategyType: template.strategyType,
        templateId: template.id,
        selectedLegs: backendLegs,
        protectionConfig,
        currentSpot,
      });
      setPlan(nextPlan);
      if (nextPlan.strategy) {
        setStrategyPreview(nextPlan.strategy);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Dry-run planning failed");
    } finally {
      setPlanning(false);
    }
  }

  async function handlePaperExecute() {
    if (!plan?.orders?.length) {
      toast.error("Build a dry-run plan before paper execution");
      return;
    }
    setPaperSubmitting(true);
    try {
      const result = await executePaperOptionStrategy({
        accountScope: "default",
        underlying,
        expiry,
        strategyType: template.strategyType,
        templateId: template.id,
        selectedLegs: backendLegs,
        protectionConfig,
        currentSpot,
      });
      if (result.strategy) {
        setStrategyPreview(result.strategy);
      }
      toast.success(result.strategyId ? `Paper strategy ${result.status} · ${result.strategyId}` : `Paper strategy ${result.status}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Paper execution failed");
    } finally {
      setPaperSubmitting(false);
    }
  }

  function adjustStrike(index: number, direction: 1 | -1) {
    if (!chain) {
      return;
    }
    setLegs((current) =>
      current.map((leg, legIndex) => {
        if (legIndex !== index) {
          return leg;
        }
        const nextStrike = leg.strike + getStrikeGap(underlying) * direction;
        return toBuilderLeg(chain, leg.optionType, leg.side, nextStrike) ?? leg;
      }),
    );
  }

  /* ── helpers for compact inline info (no overlay tooltips) ── */

  function fieldHint(input: StrategyRuleInputDescriptor): string {
    const parts: string[] = [];
    if (input.help_text) parts.push(input.help_text);
    if (input.required) parts.push("required");
    else if (input.recommended) parts.push("recommended");
    else parts.push("optional");
    return parts.join(" · ");
  }

  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--panel)] p-3">
      {/* ── Header row: template select + spot + lots ── */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={templateId}
          onChange={(event) => setTemplateId(event.currentTarget.value)}
          className="min-w-44 cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 text-[11px] text-[var(--text)]"
        >
          {templates.map((item) => (
            <option key={item.id} value={item.id}>{item.label}</option>
          ))}
        </select>
        <span className="rounded border border-[var(--border)] px-2 py-1 font-mono text-[11px] text-[var(--text)]" title={template.description}>
          spot {currentSpot.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-[9px] uppercase tracking-[0.12em] text-[var(--dim)]">lots</span>
          <button type="button" onClick={() => setLotMultiplier((v) => Math.max(1, v - 1))} className="cursor-pointer rounded border border-[var(--border)] px-2 py-0.5 text-xs text-[var(--text)] transition-colors duration-150 hover:bg-[var(--panel-hover)]">−</button>
          <span className="min-w-6 text-center text-sm font-semibold text-[var(--accent)]">{lotMultiplier}</span>
          <button type="button" onClick={() => setLotMultiplier((v) => v + 1)} className="cursor-pointer rounded border border-[var(--border)] px-2 py-0.5 text-xs text-[var(--text)] transition-colors duration-150 hover:bg-[var(--panel-hover)]">+</button>
        </div>
      </div>

      {/* ── Main two-column grid: LHS = builder, RHS = analysis ── */}
      <div className="grid min-h-0 flex-1 gap-2 lg:grid-cols-[1.15fr_0.85fr]">

        {/* ════════ LHS: Legs, Risk Controls, Actions ════════ */}
        <div className="flex flex-col gap-2">

          {/* ── Legs ── */}
          <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/70 p-2">
            <div className="space-y-1">
              {legs.map((leg, index) => (
                <div
                  key={`${leg.tradingSymbol ?? leg.strike}-${index}`}
                  className="flex items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--panel)] px-2 py-1.5 text-[11px]"
                >
                  <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-medium ${leg.side === "long" ? "border-[var(--green)]/30 text-[var(--green)]" : "border-[var(--red)]/30 text-[var(--red)]"}`}>
                    {leg.side === "long" ? "B" : "S"}
                  </span>
                  <span className="font-medium text-[var(--text)]">{leg.optionType === "call" ? "CE" : "PE"} {leg.strike}</span>
                  <span className="text-[var(--muted)]">₹{leg.premium.toFixed(2)} × {(lotMultiplier * leg.lotSize).toLocaleString("en-IN")}</span>
                  <span className="ml-auto text-[9px] text-[var(--dim)]" title={`Δ${leg.delta?.toFixed(3)} Γ${leg.gamma?.toFixed(4)} Θ${leg.theta?.toFixed(2)} V${leg.vega?.toFixed(2)} IV${leg.iv?.toFixed(1)}%`}>
                    Δ{leg.delta?.toFixed(2)}
                  </span>
                  <button type="button" onClick={() => adjustStrike(index, -1)} className="cursor-pointer rounded border border-[var(--border)] px-1.5 py-0.5 text-[var(--muted)] transition-colors duration-150 hover:bg-[var(--panel-hover)]">−</button>
                  <span className="min-w-10 text-center text-[10px] text-[var(--accent)]">{leg.strike}</span>
                  <button type="button" onClick={() => adjustStrike(index, 1)} className="cursor-pointer rounded border border-[var(--border)] px-1.5 py-0.5 text-[var(--muted)] transition-colors duration-150 hover:bg-[var(--panel-hover)]">+</button>
                </div>
              ))}
              {legs.length === 0 && (
                <p className="py-3 text-center text-[11px] text-[var(--dim)]">select a template</p>
              )}
            </div>
          </div>

          {/* ── Risk Controls ── */}
          {visibleInputs.length > 0 && (
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/70 p-2">
              <p className="mb-1.5 text-[9px] uppercase tracking-[0.16em] text-[var(--dim)]">risk controls</p>
              <div className="space-y-1.5 text-[11px]">
                {(["primary", "emergency", "secondary"] as const).map((group) => {
                  const inputs = groupedInputs[group];
                  if (inputs.length === 0) return null;
                  return (
                    <div key={group} className={`border-l-2 pl-2 ${group === "primary" ? "border-l-[var(--accent)]" : group === "emergency" ? "border-l-[var(--red)]" : "border-l-[var(--yellow)]"}`}>
                      <div className="grid gap-1 lg:grid-cols-2">
                        {inputs.map((input) => (
                          <label
                            key={input.key}
                            className="flex items-center gap-1.5 rounded border border-[var(--border)] bg-[var(--panel)] px-2 py-1 text-[var(--muted)]"
                            title={fieldHint(input)}
                          >
                            <span className="min-w-0 flex-1 truncate text-[10px]">
                              {input.label}
                              {input.unit ? <span className="ml-0.5 text-[var(--dim)]">{input.unit}</span> : null}
                            </span>
                            <input
                              type="number"
                              value={inputValue(input.key as RuleFieldKey)}
                              onChange={(event) => setInputValue(input.key as RuleFieldKey, event.currentTarget.value)}
                              className="w-20 rounded border border-[var(--border)] bg-[var(--bg)] px-1.5 py-0.5 text-right text-[var(--text)]"
                            />
                          </label>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* ── Action buttons ── */}
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/70 p-2">
            <button
              type="button"
              onClick={handleDryRun}
              disabled={!appAuthenticated || planning || !expiry || backendLegs.length === 0}
              className="cursor-pointer rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-3 py-1.5 text-[11px] font-semibold text-[var(--accent)] transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {planning ? "Planning…" : "Dry-run"}
            </button>
            <button
              type="button"
              onClick={handlePaperExecute}
              disabled={!appAuthenticated || !paperAvailable || paperSubmitting || !plan?.orders?.length}
              className="cursor-pointer rounded-md border border-[var(--blue-border)] bg-[var(--blue-soft)] px-3 py-1.5 text-[11px] font-semibold text-[var(--blue)] transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {paperSubmitting ? "Sending…" : "Paper execute"}
            </button>
            {previewLoading && (
              <span className="flex items-center gap-1 text-[10px] text-[var(--muted)]">
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--accent)]" />
                syncing
              </span>
            )}
            {previewError && <span className="text-[10px] text-[var(--red)]">{previewError}</span>}
          </div>
        </div>

        {/* ════════ RHS: Payoff, Greeks, Summary, Dry-run result, Profile ════════ */}
        <div className="flex flex-col gap-2">

          {/* ── Payoff Chart ── */}
          {legs.length > 0 && (
            <PayoffChart
              legs={legs.map((leg) => ({ ...leg, quantity: lotMultiplier }))}
              currentSpot={currentSpot}
              sliderPercent={sliderPercent}
              onSliderPercentChange={setSliderPercent}
              daysOffset={daysOffset}
              onDaysOffsetChange={setDaysOffset}
              maxDaysToExpiry={maxDaysToExpiry}
            />
          )}

          {/* ── Greeks + Cost row ── */}
          <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/70 p-2 text-[11px]">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
              {greekCell("Δ", aggregateGreeks.delta, "text-[var(--green)]")}
              {greekCell("Γ", aggregateGreeks.gamma)}
              {greekCell("Θ", aggregateGreeks.theta)}
              {greekCell("V", aggregateGreeks.vega)}
              <span className="mx-1 h-3 w-px bg-[var(--border-soft)]" />
              <span className="text-[var(--muted)]">margin <span className="text-[var(--accent)]">₹{(plan?.estimatedMargin ?? strategyPreview?.estimated_entry_cost_rupees ?? 0).toFixed(0)}</span></span>
              <span className="text-[var(--muted)]">cost <span className="text-[var(--accent)]">₹{(plan?.estimatedCost ?? strategyPreview?.estimated_entry_cost_rupees ?? 0).toFixed(0)}</span></span>
              {strategyPreview && (
                <span className="text-[var(--muted)]">prem <span className="text-[var(--accent)]">{strategyPreview.entry_combined_premium_points.toFixed(0)}pts</span></span>
              )}
            </div>
          </div>

          {/* ── Dry-run result ── */}
          {plan && (
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/70 p-2 text-[11px]">
              <p className="text-[var(--text)]">{plan.message}</p>
              {plan.strategyId && <p className="mt-0.5 text-[10px] text-[var(--muted)]">ID: {plan.strategyId}</p>}
              {plan.orders && plan.orders.length > 0 && (
                <div className="mt-1.5 rounded border border-[var(--border)] bg-[var(--panel)] p-1.5">
                  <ul className="space-y-0.5">
                    {plan.orders.map((order) => (
                      <li key={`${order.tradingsymbol}-${order.transaction_type}`} className="text-[var(--text)]">
                        <span className={order.transaction_type === "BUY" ? "text-[var(--green)]" : "text-[var(--red)]"}>{order.transaction_type}</span> {order.tradingsymbol} × {order.quantity}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}

          {/* ── Strategy profile (compact) ── */}
          <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/70 p-2 text-[11px]">
            {strategyPreview ? (
              <>
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-semibold text-[var(--accent)]">{strategyPreview.inferred_family}</span>
                  <span className="rounded border border-[var(--border)] px-1 py-0.5 text-[9px] uppercase tracking-[0.06em] text-[var(--text)]">
                    {strategyPreview.inferred_structure.replaceAll("_", " ")}
                  </span>
                  {strategyPreview.warnings.length > 0 && (
                    <span className="rounded border border-[var(--yellow)]/30 px-1 py-0.5 text-[9px] text-[var(--yellow)]">{strategyPreview.warnings.length} warn</span>
                  )}
                  {(strategyPreview.description || strategyPreview.warnings.length > 0) && (
                    <button
                      type="button"
                      onClick={() => setProfileExpanded((v) => !v)}
                      className="ml-auto cursor-pointer rounded border border-[var(--border)] px-1.5 py-0.5 text-[9px] text-[var(--dim)] transition-colors duration-150 hover:bg-[var(--panel-hover)]"
                    >
                      {profileExpanded ? "−" : "+"}
                    </button>
                  )}
                </div>
                {profileExpanded && (
                  <div className="mt-1.5 space-y-0.5 text-[10px] leading-snug text-[var(--muted)]">
                    {strategyPreview.description && <p>{strategyPreview.description}</p>}
                    {strategyPreview.warnings.length > 0 && (
                      <p className="text-[var(--yellow)]">{strategyPreview.warnings.join(" · ")}</p>
                    )}
                  </div>
                )}
              </>
            ) : (
              <span className="text-[var(--dim)]">awaiting preview…</span>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
