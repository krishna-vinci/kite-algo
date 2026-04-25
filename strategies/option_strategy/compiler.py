from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .models import (
    CanonicalOptionStrategyPreview,
    InputGroup,
    MetricKind,
    NormalizedRule,
    RuleInputDescriptor,
    RuleRole,
    SelectedOptionLeg,
    StrategyFamily,
    StrategyProtectionPreferences,
)


_TEMPLATE_HINTS: Dict[str, Tuple[str, StrategyFamily, str, str]] = {
    "buy_call": ("buy_call", StrategyFamily.DIRECTIONAL, "bullish", "Single long call directional structure."),
    "sell_put": ("sell_put", StrategyFamily.DIRECTIONAL, "bullish", "Short put directional structure with index-led risk management."),
    "bull_call_spread": ("bull_call_spread", StrategyFamily.DIRECTIONAL, "bullish", "Directional debit spread with capped upside."),
    "bear_put_spread": ("bear_put_spread", StrategyFamily.DIRECTIONAL, "bearish", "Directional downside debit spread."),
    "short_straddle": ("short_straddle", StrategyFamily.NEUTRAL_SHORT_PREMIUM, "neutral", "Neutral short premium structure managed with combined premium and index emergency guards."),
    "short_strangle": ("short_strangle", StrategyFamily.NEUTRAL_SHORT_PREMIUM, "neutral", "Neutral short premium structure with wider short strikes."),
    "long_straddle": ("long_straddle", StrategyFamily.LONG_VOL, "long_volatility", "Long volatility structure managed with combined premium expansion."),
    "long_strangle": ("long_strangle", StrategyFamily.LONG_VOL, "long_volatility", "Long volatility structure with lower entry premium than a straddle."),
    "iron_condor": ("iron_condor", StrategyFamily.NEUTRAL_SHORT_PREMIUM, "neutral", "Defined-risk neutral short premium structure."),
}


def compile_option_strategy_preview(
    *,
    underlying: str,
    template_id: Optional[str],
    strategy_type: str,
    current_spot: Optional[float],
    legs: Iterable[SelectedOptionLeg],
    protection_preferences: Optional[StrategyProtectionPreferences] = None,
) -> CanonicalOptionStrategyPreview:
    normalized_legs = [leg if isinstance(leg, SelectedOptionLeg) else SelectedOptionLeg.model_validate(leg) for leg in legs]
    prefs = protection_preferences or StrategyProtectionPreferences()

    entry_points = round(sum(leg.ltp * max(leg.lots, 1) for leg in normalized_legs), 2)
    net_entry_rupees = round(
        sum((leg.ltp * leg.effective_quantity) * (1 if leg.transaction_type == "SELL" else -1) for leg in normalized_legs),
        2,
    )
    entry_cost_rupees = abs(net_entry_rupees)
    combined_entry_type = "credit" if net_entry_rupees >= 0 else "debit"

    inferred_structure, family, bias, description, confidence, reason = _infer_strategy_shape(
        template_id=template_id,
        strategy_type=strategy_type,
        legs=normalized_legs,
    )

    primary_metric = _primary_metric_for_family(family)
    emergency_metric = MetricKind.INDEX_PRICE if family == StrategyFamily.NEUTRAL_SHORT_PREMIUM else None

    inputs: Dict[str, RuleInputDescriptor] = {}
    rules: List[NormalizedRule] = []
    warnings: List[str] = []

    index_lower_default, index_upper_default = _default_index_boundaries(
        underlying=underlying,
        current_spot=current_spot,
        bias=bias,
        family=family,
    )

    combined_target_default, combined_stop_default = _default_combined_premium_values(family, entry_points)
    mtm_target_default, mtm_stop_default = _default_mtm_values(family, entry_cost_rupees)

    def register_input(
        key: str,
        *,
        label: str,
        unit: str,
        group: InputGroup,
        requested_value: Optional[float],
        default_value: Optional[float],
        required: bool,
        recommended: bool,
        help_text: str,
    ) -> Optional[float]:
        value = requested_value if requested_value is not None else default_value
        if value is None and not recommended and not required:
            source = "empty_optional"
        elif requested_value is not None:
            source = "user_input"
        else:
            source = "backend_required" if required else "backend_default"
        inputs[key] = RuleInputDescriptor(
            key=key,
            label=label,
            unit=unit,
            group=group,
            required=required,
            recommended=recommended,
            value=value,
            source=source,
            help_text=help_text,
        )
        return value

    if family == StrategyFamily.DIRECTIONAL:
        lower_label, upper_label = (
            ("index stoploss", "index target") if bias == "bullish" else ("index target", "index stoploss")
        )
        lower_value = register_input(
            "index_lower_boundary",
            label=lower_label,
            unit="index points",
            group=InputGroup.PRIMARY,
            requested_value=prefs.index_lower_boundary,
            default_value=index_lower_default,
            required=True,
            recommended=True,
            help_text="Backend-classified directional lower boundary.",
        )
        upper_value = register_input(
            "index_upper_boundary",
            label=upper_label,
            unit="index points",
            group=InputGroup.PRIMARY,
            requested_value=prefs.index_upper_boundary,
            default_value=index_upper_default,
            required=True,
            recommended=True,
            help_text="Backend-classified directional upper boundary.",
        )
        mtm_stop_value = register_input(
            "basket_mtm_stoploss",
            label="basket MTM stoploss",
            unit="₹",
            group=InputGroup.SECONDARY,
            requested_value=prefs.basket_mtm_stoploss,
            default_value=mtm_stop_default,
            required=False,
            recommended=False,
            help_text="Optional rupee-denominated basket loss guard.",
        )
        mtm_target_value = register_input(
            "basket_mtm_target",
            label="basket MTM target",
            unit="₹",
            group=InputGroup.SECONDARY,
            requested_value=prefs.basket_mtm_target,
            default_value=mtm_target_default,
            required=False,
            recommended=False,
            help_text="Optional rupee-denominated basket profit target.",
        )
        if lower_value is not None:
            rules.append(
                NormalizedRule(
                    key="directional-index-lower",
                    metric=MetricKind.INDEX_PRICE,
                    role=RuleRole.HARD_STOP if bias == "bullish" else RuleRole.PROFIT_TARGET,
                    label=lower_label,
                    operator="lte",
                    threshold=float(lower_value),
                    required=True,
                    source=inputs["index_lower_boundary"].source,
                )
            )
        if upper_value is not None:
            rules.append(
                NormalizedRule(
                    key="directional-index-upper",
                    metric=MetricKind.INDEX_PRICE,
                    role=RuleRole.PROFIT_TARGET if bias == "bullish" else RuleRole.HARD_STOP,
                    label=upper_label,
                    operator="gte",
                    threshold=float(upper_value),
                    required=True,
                    source=inputs["index_upper_boundary"].source,
                )
            )
        if mtm_stop_value is not None:
            rules.append(
                NormalizedRule(
                    key="directional-mtm-stop",
                    metric=MetricKind.BASKET_MTM_RUPEES,
                    role=RuleRole.HARD_STOP,
                    label="basket MTM stoploss",
                    operator="lte",
                    threshold=-abs(float(mtm_stop_value)),
                    source=inputs["basket_mtm_stoploss"].source,
                )
            )
        if mtm_target_value is not None:
            rules.append(
                NormalizedRule(
                    key="directional-mtm-target",
                    metric=MetricKind.BASKET_MTM_RUPEES,
                    role=RuleRole.PROFIT_TARGET,
                    label="basket MTM target",
                    operator="gte",
                    threshold=float(mtm_target_value),
                    source=inputs["basket_mtm_target"].source,
                )
            )
    elif family == StrategyFamily.NEUTRAL_SHORT_PREMIUM:
        lower_value = register_input(
            "index_lower_boundary",
            label="index lower emergency",
            unit="index points",
            group=InputGroup.EMERGENCY,
            requested_value=prefs.index_lower_boundary,
            default_value=index_lower_default,
            required=True,
            recommended=True,
            help_text="Mandatory downside emergency guard for neutral short premium.",
        )
        upper_value = register_input(
            "index_upper_boundary",
            label="index upper emergency",
            unit="index points",
            group=InputGroup.EMERGENCY,
            requested_value=prefs.index_upper_boundary,
            default_value=index_upper_default,
            required=True,
            recommended=True,
            help_text="Mandatory upside emergency guard for neutral short premium.",
        )
        combined_target_value = register_input(
            "combined_premium_target",
            label="combined premium target",
            unit="premium points",
            group=InputGroup.PRIMARY,
            requested_value=prefs.combined_premium_target,
            default_value=combined_target_default,
            required=True,
            recommended=True,
            help_text="Primary book-profit rule in premium points.",
        )
        combined_stop_value = register_input(
            "combined_premium_stoploss",
            label="combined premium stoploss",
            unit="premium points",
            group=InputGroup.PRIMARY,
            requested_value=prefs.combined_premium_stoploss,
            default_value=combined_stop_default,
            required=False,
            recommended=False,
            help_text="Optional premium-expansion stoploss.",
        )
        mtm_stop_value = register_input(
            "basket_mtm_stoploss",
            label="basket MTM stoploss",
            unit="₹",
            group=InputGroup.SECONDARY,
            requested_value=prefs.basket_mtm_stoploss,
            default_value=mtm_stop_default,
            required=False,
            recommended=False,
            help_text="Optional rupee-denominated basket loss guard.",
        )
        mtm_target_value = register_input(
            "basket_mtm_target",
            label="basket MTM target",
            unit="₹",
            group=InputGroup.SECONDARY,
            requested_value=prefs.basket_mtm_target,
            default_value=mtm_target_default,
            required=False,
            recommended=False,
            help_text="Optional rupee-denominated basket profit target.",
        )
        if lower_value is not None:
            rules.append(_rule("neutral-lower-emergency", MetricKind.INDEX_PRICE, RuleRole.EMERGENCY_GUARD, "index lower emergency", "lte", lower_value, True, inputs["index_lower_boundary"].source))
        if upper_value is not None:
            rules.append(_rule("neutral-upper-emergency", MetricKind.INDEX_PRICE, RuleRole.EMERGENCY_GUARD, "index upper emergency", "gte", upper_value, True, inputs["index_upper_boundary"].source))
        if combined_target_value is not None:
            rules.append(_rule("neutral-combined-target", MetricKind.COMBINED_PREMIUM_POINTS, RuleRole.PROFIT_TARGET, "combined premium target", "lte", max(entry_points - combined_target_value, 0), True, inputs["combined_premium_target"].source))
        if combined_stop_value is not None:
            rules.append(_rule("neutral-combined-stop", MetricKind.COMBINED_PREMIUM_POINTS, RuleRole.HARD_STOP, "combined premium stoploss", "gte", entry_points + abs(float(combined_stop_value)), False, inputs["combined_premium_stoploss"].source))
        if mtm_stop_value is not None:
            rules.append(_rule("neutral-mtm-stop", MetricKind.BASKET_MTM_RUPEES, RuleRole.HARD_STOP, "basket MTM stoploss", "lte", -abs(float(mtm_stop_value)), False, inputs["basket_mtm_stoploss"].source))
        if mtm_target_value is not None:
            rules.append(_rule("neutral-mtm-target", MetricKind.BASKET_MTM_RUPEES, RuleRole.PROFIT_TARGET, "basket MTM target", "gte", float(mtm_target_value), False, inputs["basket_mtm_target"].source))
    elif family == StrategyFamily.LONG_VOL:
        combined_target_value = register_input(
            "combined_premium_target",
            label="combined premium target",
            unit="premium points",
            group=InputGroup.PRIMARY,
            requested_value=prefs.combined_premium_target,
            default_value=combined_target_default,
            required=True,
            recommended=True,
            help_text="Primary premium expansion target.",
        )
        combined_stop_value = register_input(
            "combined_premium_stoploss",
            label="combined premium stoploss",
            unit="premium points",
            group=InputGroup.PRIMARY,
            requested_value=prefs.combined_premium_stoploss,
            default_value=combined_stop_default,
            required=True,
            recommended=True,
            help_text="Primary premium contraction stoploss.",
        )
        mtm_stop_value = register_input(
            "basket_mtm_stoploss",
            label="basket MTM stoploss",
            unit="₹",
            group=InputGroup.SECONDARY,
            requested_value=prefs.basket_mtm_stoploss,
            default_value=mtm_stop_default,
            required=False,
            recommended=False,
            help_text="Optional rupee-denominated loss guard.",
        )
        mtm_target_value = register_input(
            "basket_mtm_target",
            label="basket MTM target",
            unit="₹",
            group=InputGroup.SECONDARY,
            requested_value=prefs.basket_mtm_target,
            default_value=mtm_target_default,
            required=False,
            recommended=False,
            help_text="Optional rupee-denominated profit target.",
        )
        if combined_target_value is not None:
            rules.append(_rule("long-vol-combined-target", MetricKind.COMBINED_PREMIUM_POINTS, RuleRole.PROFIT_TARGET, "combined premium target", "gte", entry_points + abs(float(combined_target_value)), True, inputs["combined_premium_target"].source))
        if combined_stop_value is not None:
            rules.append(_rule("long-vol-combined-stop", MetricKind.COMBINED_PREMIUM_POINTS, RuleRole.HARD_STOP, "combined premium stoploss", "lte", max(entry_points - abs(float(combined_stop_value)), 0), True, inputs["combined_premium_stoploss"].source))
        if mtm_stop_value is not None:
            rules.append(_rule("long-vol-mtm-stop", MetricKind.BASKET_MTM_RUPEES, RuleRole.HARD_STOP, "basket MTM stoploss", "lte", -abs(float(mtm_stop_value)), False, inputs["basket_mtm_stoploss"].source))
        if mtm_target_value is not None:
            rules.append(_rule("long-vol-mtm-target", MetricKind.BASKET_MTM_RUPEES, RuleRole.PROFIT_TARGET, "basket MTM target", "gte", float(mtm_target_value), False, inputs["basket_mtm_target"].source))
    else:
        lower_value = register_input(
            "index_lower_boundary",
            label="index lower emergency",
            unit="index points",
            group=InputGroup.EMERGENCY,
            requested_value=prefs.index_lower_boundary,
            default_value=index_lower_default,
            required=False,
            recommended=False,
            help_text="Optional downside emergency guard.",
        )
        upper_value = register_input(
            "index_upper_boundary",
            label="index upper emergency",
            unit="index points",
            group=InputGroup.EMERGENCY,
            requested_value=prefs.index_upper_boundary,
            default_value=index_upper_default,
            required=False,
            recommended=False,
            help_text="Optional upside emergency guard.",
        )
        mtm_stop_value = register_input(
            "basket_mtm_stoploss",
            label="basket MTM stoploss",
            unit="₹",
            group=InputGroup.PRIMARY,
            requested_value=prefs.basket_mtm_stoploss,
            default_value=mtm_stop_default,
            required=True,
            recommended=True,
            help_text="Primary rupee-denominated loss guard for structure-managed strategies.",
        )
        mtm_target_value = register_input(
            "basket_mtm_target",
            label="basket MTM target",
            unit="₹",
            group=InputGroup.PRIMARY,
            requested_value=prefs.basket_mtm_target,
            default_value=mtm_target_default,
            required=True,
            recommended=True,
            help_text="Primary rupee-denominated profit target for structure-managed strategies.",
        )
        if lower_value is not None:
            rules.append(_rule("structure-lower-emergency", MetricKind.INDEX_PRICE, RuleRole.EMERGENCY_GUARD, "index lower emergency", "lte", lower_value, False, inputs["index_lower_boundary"].source))
        if upper_value is not None:
            rules.append(_rule("structure-upper-emergency", MetricKind.INDEX_PRICE, RuleRole.EMERGENCY_GUARD, "index upper emergency", "gte", upper_value, False, inputs["index_upper_boundary"].source))
        if mtm_stop_value is not None:
            rules.append(_rule("structure-mtm-stop", MetricKind.BASKET_MTM_RUPEES, RuleRole.HARD_STOP, "basket MTM stoploss", "lte", -abs(float(mtm_stop_value)), True, inputs["basket_mtm_stoploss"].source))
        if mtm_target_value is not None:
            rules.append(_rule("structure-mtm-target", MetricKind.BASKET_MTM_RUPEES, RuleRole.PROFIT_TARGET, "basket MTM target", "gte", float(mtm_target_value), True, inputs["basket_mtm_target"].source))
        warnings.append("Backend marked this structure as MTM-led because raw combined premium can be misleading for mixed long/short baskets.")

    if current_spot is None:
        warnings.append("Current spot was unavailable, so any index defaults were left empty or derived conservatively.")
    if family == StrategyFamily.NEUTRAL_SHORT_PREMIUM and prefs.combined_premium_stoploss is None:
        warnings.append("Combined premium stoploss is optional here; index emergency bracket remains the mandatory loss guard.")

    return CanonicalOptionStrategyPreview(
        user_intent=template_id or strategy_type,
        inferred_structure=inferred_structure,
        inferred_family=family,
        direction_bias=bias,  # type: ignore[arg-type]
        classification_confidence=confidence,
        classification_reason=reason,
        description=description,
        primary_metric=primary_metric,
        emergency_metric=emergency_metric,
        combined_premium_entry_type=combined_entry_type if family != StrategyFamily.PREMIUM_MANAGED_STRUCTURE else None,
        entry_combined_premium_points=entry_points,
        estimated_entry_cost_rupees=entry_cost_rupees,
        warnings=warnings,
        inputs=inputs,
        rules=rules,
    )


def _rule(key: str, metric: MetricKind, role: RuleRole, label: str, operator: str, threshold: float, required: bool, source: str) -> NormalizedRule:
    return NormalizedRule(
        key=key,
        metric=metric,
        role=role,
        label=label,
        operator=operator,  # type: ignore[arg-type]
        threshold=round(float(threshold), 2),
        required=required,
        source=source,  # type: ignore[arg-type]
    )


def _infer_strategy_shape(*, template_id: Optional[str], strategy_type: str, legs: List[SelectedOptionLeg]) -> Tuple[str, StrategyFamily, str, str, float, str]:
    normalized_template = str(template_id or "").strip().lower()
    if normalized_template in _TEMPLATE_HINTS:
        structure, family, bias, description = _TEMPLATE_HINTS[normalized_template]
        return structure, family, bias, description, 0.98, f"Template hint '{normalized_template}' matched a known backend strategy shape."

    expiry_keys = {leg.expiry_key for leg in legs if leg.expiry_key}
    if len(expiry_keys) > 1:
        return (
            str(strategy_type or "manual"),
            StrategyFamily.PREMIUM_MANAGED_STRUCTURE,
            "structure",
            "Mixed-expiry or structure-managed option basket.",
            0.8,
            "Legs span multiple expiries, so backend selected structure-managed family.",
        )

    short_call = any(leg.transaction_type == "SELL" and leg.option_type == "CE" for leg in legs)
    short_put = any(leg.transaction_type == "SELL" and leg.option_type == "PE" for leg in legs)
    long_call = any(leg.transaction_type == "BUY" and leg.option_type == "CE" for leg in legs)
    long_put = any(leg.transaction_type == "BUY" and leg.option_type == "PE" for leg in legs)
    any_shorts = any(leg.transaction_type == "SELL" for leg in legs)

    if len(legs) >= 4 and short_call and short_put and long_call and long_put:
        return "iron_condor", StrategyFamily.NEUTRAL_SHORT_PREMIUM, "neutral", "Defined-risk neutral premium structure inferred from paired short and long wings.", 0.82, "Backend detected paired short and long option wings."

    if short_call and short_put:
        same_strikes = {leg.strike for leg in legs if leg.transaction_type == "SELL"}
        structure = "short_straddle" if len(same_strikes) == 1 else "short_strangle"
        return structure, StrategyFamily.NEUTRAL_SHORT_PREMIUM, "neutral", "Neutral short premium structure inferred from short call and short put legs.", 0.9, "Backend detected both short call and short put legs."

    if long_call and long_put and not any_shorts:
        same_strikes = {leg.strike for leg in legs}
        structure = "long_straddle" if len(same_strikes) == 1 else "long_strangle"
        return structure, StrategyFamily.LONG_VOL, "long_volatility", "Long volatility structure inferred from long call and long put legs.", 0.9, "Backend detected both long call and long put legs without shorts."

    bias = "bullish"
    if long_put and not long_call:
        bias = "bearish"
    elif short_call and not short_put:
        bias = "bearish"
    return str(strategy_type or template_id or "manual"), StrategyFamily.DIRECTIONAL, bias, "Directional option structure inferred from the selected legs.", 0.75, "Backend fell back to directional classification based on remaining leg mix."


def _primary_metric_for_family(family: StrategyFamily) -> MetricKind:
    if family == StrategyFamily.DIRECTIONAL:
        return MetricKind.INDEX_PRICE
    if family == StrategyFamily.PREMIUM_MANAGED_STRUCTURE:
        return MetricKind.BASKET_MTM_RUPEES
    return MetricKind.COMBINED_PREMIUM_POINTS


def _default_index_boundaries(*, underlying: str, current_spot: Optional[float], bias: str, family: StrategyFamily) -> Tuple[Optional[float], Optional[float]]:
    if current_spot is None:
        return None, None
    gap = 100 if str(underlying).upper() == "BANKNIFTY" else 50
    lower = _round_to_gap(current_spot - gap * 2, gap)
    upper = _round_to_gap(current_spot + gap * 2, gap)
    if family == StrategyFamily.DIRECTIONAL:
        if bias == "bullish":
            return _round_to_gap(current_spot - gap * 2, gap), _round_to_gap(current_spot + gap * 3, gap)
        if bias == "bearish":
            return _round_to_gap(current_spot - gap * 3, gap), _round_to_gap(current_spot + gap * 2, gap)
    return lower, upper


def _default_combined_premium_values(family: StrategyFamily, entry_points: float) -> Tuple[Optional[float], Optional[float]]:
    if entry_points <= 0:
        return None, None
    if family == StrategyFamily.NEUTRAL_SHORT_PREMIUM:
        return max(40.0, round(entry_points * 0.35)), None
    if family == StrategyFamily.LONG_VOL:
        return max(40.0, round(entry_points * 0.4)), max(30.0, round(entry_points * 0.25))
    return None, None


def _default_mtm_values(family: StrategyFamily, entry_cost_rupees: float) -> Tuple[Optional[float], Optional[float]]:
    if entry_cost_rupees <= 0:
        return None, None
    if family == StrategyFamily.PREMIUM_MANAGED_STRUCTURE:
        return round(entry_cost_rupees * 0.4, 2), round(entry_cost_rupees * 0.25, 2)
    return None, None


def _round_to_gap(value: float, gap: int) -> float:
    return float(int(round(value / gap)) * gap)
