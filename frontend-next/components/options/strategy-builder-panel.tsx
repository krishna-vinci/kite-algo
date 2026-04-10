"use client";

import { useEffect, useMemo, useState } from "react";
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
  deltaFilter: number;
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

function metricLabel(metric: CanonicalStrategyPreview["primary_metric"] | CanonicalStrategyPreview["emergency_metric"] | null | undefined) {
  if (!metric) {
    return null;
  }
  if (metric === "index_price") {
    return "index price";
  }
  if (metric === "combined_premium_points") {
    return "combined premium";
  }
  return "basket MTM";
}

export function StrategyBuilderPanel({ underlying, expiry, currentSpot, deltaFilter, chain, appAuthenticated, paperAvailable }: StrategyBuilderPanelProps) {
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
  const [strategyPreview, setStrategyPreview] = useState<CanonicalStrategyPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [plan, setPlan] = useState<DryRunPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [paperSubmitting, setPaperSubmitting] = useState(false);

  const template = templates.find((item) => item.id === templateId) ?? templates[0];

  useEffect(() => {
    if (!chain) {
      return;
    }
    const gap = getStrikeGap(underlying);
    const nextLegs = template.legBlueprints
      .map((blueprint) => toBuilderLeg(chain, blueprint.optionType, blueprint.side, chain.atmStrike + blueprint.strikeOffset * gap))
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
  }, [chain, template, underlying]);

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

  function applyPreviewDefaults(preview: CanonicalStrategyPreview) {
    const applyValue = (key: RuleFieldKey, setter: (value: number | "") => void) => {
      if (touchedInputs[key]) {
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
  }

  useEffect(() => {
    if (!expiry || backendLegs.length === 0) {
      setStrategyPreview(null);
      setPreviewError(null);
      return;
    }

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setPreviewLoading(true);
      try {
        const preview = await previewOptionStrategy({
          underlying,
          expiry,
          strategyType: template.strategyType,
          templateId: template.id,
          selectedLegs: backendLegs,
          protectionConfig,
          currentSpot,
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
  }, [underlying, expiry, template.id, template.strategyType, backendLegs, protectionConfig, currentSpot]);

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

  return (
    <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto rounded-2xl border border-[var(--border)] bg-[var(--panel)] p-3">
      <div className="flex flex-wrap items-start gap-3">
        <div>
          <p className="text-[10px] uppercase tracking-[0.24em] text-[var(--dim)]">strategy builder</p>
          <h2 className="mt-1 text-sm font-semibold text-[var(--text)]">Primary structured deployment workflow</h2>
        </div>
        <label className="ml-auto flex min-w-56 flex-col gap-2 text-[11px] text-[var(--muted)]">
          strategy template
          <select value={templateId} onChange={(event) => setTemplateId(event.currentTarget.value)} className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-[var(--text)]">
            {templates.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="grid gap-3 lg:grid-cols-[0.85fr_1.15fr]">
        <div className="space-y-3">
          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">strategy selection</p>
            <p className="mt-2 text-[11px] text-[var(--muted)]">Select structure on the left, review Greeks/margin/payoff on the right.</p>
          </div>

          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">backend profile</p>
            {strategyPreview ? (
              <>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <p className="text-sm font-semibold text-[var(--accent)]">{strategyPreview.inferred_family}</p>
                  <span className="rounded-md border border-[var(--border)] px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-[var(--text)]">{strategyPreview.inferred_structure.replaceAll("_", " ")}</span>
                </div>
                <p className="mt-2 text-[11px] text-[var(--muted)]">{strategyPreview.description}</p>
                <div className="mt-3 grid gap-2 text-[11px] text-[var(--muted)] sm:grid-cols-2">
                  <div>
                    primary metric <span className="ml-1 text-[var(--text)]">{metricLabel(strategyPreview.primary_metric)}</span>
                  </div>
                  {strategyPreview.emergency_metric ? (
                    <div>
                      emergency metric <span className="ml-1 text-[var(--text)]">{metricLabel(strategyPreview.emergency_metric)}</span>
                    </div>
                  ) : null}
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {strategyPreview.rules.map((rule) => (
                    <span key={rule.key} className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-[var(--text)]">
                      {rule.label}
                    </span>
                  ))}
                </div>
                {strategyPreview.warnings.length ? (
                  <div className="mt-3 space-y-1 rounded-lg border border-[var(--amber-border)] bg-[var(--amber-soft)]/60 p-2 text-[11px] text-[var(--muted)]">
                    {strategyPreview.warnings.map((warning) => (
                      <p key={warning}>• {warning}</p>
                    ))}
                  </div>
                ) : null}
              </>
            ) : (
              <p className="mt-2 text-[11px] text-[var(--muted)]">Backend profile will appear after the strategy legs are synced.</p>
            )}
            {previewLoading ? <p className="mt-2 text-[11px] text-[var(--muted)]">Syncing backend strategy profile…</p> : null}
            {previewError ? <p className="mt-2 text-[11px] text-[var(--red)]">{previewError}</p> : null}
          </div>

          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
            <div className="flex items-center justify-between">
              <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">lots</p>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => setLotMultiplier((value) => Math.max(1, value - 1))} className="rounded border border-[var(--border)] px-3 py-1">-</button>
                <span className="min-w-10 text-center text-lg font-semibold text-[var(--accent)]">{lotMultiplier}</span>
                <button type="button" onClick={() => setLotMultiplier((value) => value + 1)} className="rounded border border-[var(--border)] px-3 py-1">+</button>
              </div>
            </div>
            <p className="mt-2 text-[11px] text-[var(--muted)]">Delta search retained at {deltaFilter.toFixed(2)} while legs remain manually adjustable.</p>
          </div>

          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">backend-defined rule inputs</p>
            <p className="mt-2 text-[11px] text-[var(--muted)]">Index, combined premium, and basket MTM inputs are normalized by the backend.</p>
            <div className="mt-3 grid gap-2 sm:grid-cols-2 text-[11px]">
              {visibleInputs.map((input) => (
                <label key={input.key} className="flex flex-col gap-1 text-[var(--muted)]">
                  <span className="flex items-center gap-2">
                    {input.label}
                    <span className="rounded border border-[var(--border)] px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em] text-[var(--dim)]">{input.group}</span>
                    <span className="text-[10px] text-[var(--dim)]">{input.unit}</span>
                  </span>
                  <input
                    type="number"
                    value={inputValue(input.key as RuleFieldKey)}
                    onChange={(event) => setInputValue(input.key as RuleFieldKey, event.currentTarget.value)}
                    className="rounded border border-[var(--border)] bg-[var(--panel)] px-2 py-2 text-[var(--text)]"
                  />
                  <span className="text-[10px] text-[var(--dim)]">
                    {input.required ? "required" : input.recommended ? "recommended" : "optional"}
                    {input.help_text ? ` · ${input.help_text}` : ""}
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" onClick={handleDryRun} disabled={!appAuthenticated || planning || !expiry || backendLegs.length === 0} className="rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-3 py-2 text-[11px] font-semibold text-[var(--accent)] disabled:opacity-60">
                {planning ? "Planning…" : "Build dry-run plan"}
              </button>
              <button type="button" onClick={handlePaperExecute} disabled={!appAuthenticated || !paperAvailable || paperSubmitting || !plan?.orders?.length} className="rounded-md border border-[var(--blue-border)] bg-[var(--blue-soft)] px-3 py-2 text-[11px] font-semibold text-[var(--blue)] disabled:opacity-60">
                {paperSubmitting ? "Sending to paper…" : "Execute on paper"}
              </button>
            </div>
            <p className="mt-2 text-[11px] text-[var(--muted)]">Dry-run and paper execution now route through the backend strategy compiler and store.</p>
          </div>
        </div>

        <div className="space-y-3">
          <div className="grid gap-3 lg:grid-cols-2">
            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
              <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">aggregate greeks</p>
              <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
                <div>Δ <span className="text-[var(--green)]">{aggregateGreeks.delta.toFixed(2)}</span></div>
                <div>Γ <span className="text-[var(--text)]">{aggregateGreeks.gamma.toFixed(2)}</span></div>
                <div>Θ <span className="text-[var(--text)]">{aggregateGreeks.theta.toFixed(2)}</span></div>
                <div>Vega <span className="text-[var(--text)]">{aggregateGreeks.vega.toFixed(2)}</span></div>
              </div>
            </div>
            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
              <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">margin snapshot</p>
              <div className="mt-2 space-y-2 text-[11px] text-[var(--muted)]">
                <div>Estimated margin <span className="ml-2 text-[var(--accent)]">₹{(plan?.estimatedMargin ?? strategyPreview?.estimated_entry_cost_rupees ?? 0).toFixed(0)}</span></div>
                <div>Estimated cost <span className="ml-2 text-[var(--accent)]">₹{(plan?.estimatedCost ?? strategyPreview?.estimated_entry_cost_rupees ?? 0).toFixed(0)}</span></div>
                {strategyPreview ? (
                  <div>Combined premium <span className="ml-2 text-[var(--accent)]">{strategyPreview.entry_combined_premium_points.toFixed(0)} pts</span></div>
                ) : null}
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
            <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">legs</p>
            <div className="mt-3 space-y-2">
              {legs.map((leg, index) => (
                <div key={`${leg.tradingSymbol ?? leg.strike}-${index}`} className="grid grid-cols-[auto_1fr_auto_auto_auto] items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-[11px]">
                  <span className={`rounded border px-2 py-1 ${leg.side === "long" ? "border-[var(--green)]/30 text-[var(--green)]" : "border-[var(--red)]/30 text-[var(--red)]"}`}>{leg.side === "long" ? "BUY" : "SELL"}</span>
                  <div>
                    <div className="font-medium text-[var(--text)]">{leg.optionType === "call" ? "CE" : "PE"} {leg.strike}</div>
                    <div className="text-[var(--muted)]">Δ {leg.delta?.toFixed(2) ?? "—"} · Γ {leg.gamma?.toFixed(2) ?? "—"} · Θ {leg.theta?.toFixed(2) ?? "—"} · Vega {leg.vega?.toFixed(2) ?? "—"}</div>
                  </div>
                  <button type="button" onClick={() => adjustStrike(index, -1)} className="rounded border border-[var(--border)] px-2 py-1">-</button>
                  <span className="text-[var(--accent)]">{leg.strike}</span>
                  <button type="button" onClick={() => adjustStrike(index, 1)} className="rounded border border-[var(--border)] px-2 py-1">+</button>
                </div>
              ))}
            </div>
          </div>

          {legs.length > 0 ? <PayoffChart legs={legs.map((leg) => ({ ...leg, quantity: lotMultiplier }))} currentSpot={currentSpot} sliderPercent={sliderPercent} onSliderPercentChange={setSliderPercent} daysOffset={daysOffset} onDaysOffsetChange={setDaysOffset} maxDaysToExpiry={Math.max(1, Math.ceil((new Date(`${expiry}T15:30:00+05:30`).getTime() - Date.now()) / (1000 * 60 * 60 * 24)))} /> : null}

          <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
            {plan ? (
              <div className="mt-3 space-y-2 text-[11px]">
                <p className="text-[var(--text)]">{plan.message}</p>
                {plan.strategyId ? <p className="text-[var(--muted)]">Strategy ID: {plan.strategyId}</p> : null}
                <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-3">
                  <p className="mb-2 text-[10px] uppercase tracking-[0.16em] text-[var(--dim)]">dry-run orders</p>
                  <ul className="space-y-1">
                    {plan.orders?.map((order) => (
                      <li key={`${order.tradingsymbol}-${order.transaction_type}`} className="text-[var(--text)]">{order.transaction_type} {order.tradingsymbol} × {order.quantity}</li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}
