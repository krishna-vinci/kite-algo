"""Per-strategy risk policy: the declaration, its ceiling, and its arithmetic (B2.5).

A hosted strategy VERSION declares what it is allowed to risk. The declaration is
frozen with the immutable version and enforced at admission against two further
levels that can only ever *tighten* it:

* the **operator policy** (``StrategyAdmissionPolicy``) - the owner-recorded
  allocation, per-instrument and gross notional axes, and daily loss budget;
* the **platform ceiling** - configuration the platform operator sets once, read
  from the environment exactly as ``admission.margin_max_age_seconds`` is. An
  absent ceiling is not a ceiling of zero.

The effective policy is therefore ``min(declared, operator, ceiling)`` for every
numeric limit, the intersection of every allow-list, and a boolean that only
ever tightens:

* ``naked_permitted`` is an explicit deny switch: it is false when any level
  states false, true when all stated levels grant it, and ``None`` when no level
  states it. Naked exposure still requires a frozen option protection exit rule;
* ``protection.stop_required`` is a *requirement*: it applies when ANY level
  demands it (OR). Applying AND here would let a floor that names no stop
  requirement silently cancel a declaration that does, which is the opposite of
  a ceiling.

Nothing here places an order. The functions are pure: the same declaration, the
same policy and the same ceiling always produce the same effective policy.

The two arithmetic helpers - :func:`classify_structure_family` and
:func:`worst_case_loss_inr` - read only the FROZEN legs (option type, side,
strike, expiry, signed quantity and reference price). They are deliberately
small: a classifier that a moving chain could redefine is not a control, and
the worst case includes the frozen entry premium rather than guessing when it
is absent.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

__all__ = [
    "ALLOWED_EXPIRY_POLICIES",
    "STRUCTURE_FAMILIES",
    "RiskPolicyError",
    "classify_structure_family",
    "effective_risk_policy",
    "frozen_protection_stop",
    "operator_risk_policy",
    "platform_risk_ceiling",
    "unhedged_target",
    "validate_risk_policy",
    "worst_case_loss_inr",
]

#: The structure families the classifier can name, in the vocabulary an
#: ``allowed_structure_families`` declaration is validated against. ``custom``
#: is the honest answer for any shape the small classifier does not recognise.
STRUCTURE_FAMILIES = (
    "long_single",
    "short_single",
    "vertical_spread",
    "ratio_spread",
    "calendar",
    "diagonal",
    "butterfly",
    "straddle",
    "strangle",
    "iron_condor",
    "iron_butterfly",
    "custom",
)

#: The frozen expiry policies an ``expiry_policy`` allow-list may name (mirrors
#: the compiler's own ``EXPIRY_POLICIES`` vocabulary).
ALLOWED_EXPIRY_POLICIES = (
    "exit_before_cutoff",
    "allow_cash_settlement",
    "allow_physical_settlement",
)

#: The recognised fields of one declaration. Unknown fields are refused so a
#: typo cannot silently disable a limit.
DECLARED_FIELDS = (
    "max_loss_inr",
    "notional_limit_inr",
    "margin_limit_inr",
    "protection",
    "expiry_policy",
    "allowed_structure_families",
    "naked_permitted",
)

#: Environment variables that express the platform ceiling.
_CEILING_ENV = {
    "max_loss_inr": "ADMISSION_RISK_MAX_LOSS_INR",
    "notional_limit_inr": "ADMISSION_RISK_NOTIONAL_LIMIT_INR",
    "margin_limit_inr": "ADMISSION_RISK_MARGIN_LIMIT_INR",
}
_CEILING_FAMILIES_ENV = "ADMISSION_RISK_ALLOWED_STRUCTURE_FAMILIES"
_CEILING_EXPIRY_ENV = "ADMISSION_RISK_EXPIRY_POLICIES"
_CEILING_NAKED_ENV = "ADMISSION_RISK_NAKED_PERMITTED"
_CEILING_STOP_ENV = "ADMISSION_RISK_STOP_REQUIRED"

class RiskPolicyError(ValueError):
    """An invalid ``risk_policy`` declaration, naming the offending field."""


# ---------------------------------------------------------------------------
# declaration validation (used at version creation)
# ---------------------------------------------------------------------------


def _positive_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise RiskPolicyError(f"risk_policy.{field} must be a positive number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskPolicyError(f"risk_policy.{field} must be a positive number") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise RiskPolicyError(f"risk_policy.{field} must be a positive number")
    return numeric


def _choice_list(value: Any, *, field: str, allowed: Sequence[str]) -> List[str]:
    if isinstance(value, str):
        items: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        raise RiskPolicyError(
            f"risk_policy.{field} must be a list of allowed values"
        )
    chosen: List[str] = []
    for item in items:
        text = str(item)
        if text not in allowed:
            raise RiskPolicyError(
                f"risk_policy.{field} names an unknown value: {text!r}"
            )
        if text not in chosen:
            chosen.append(text)
    if not chosen:
        raise RiskPolicyError(
            f"risk_policy.{field} must name at least one allowed value"
        )
    return chosen


def _validate_protection(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RiskPolicyError("risk_policy.protection must be a JSON object")
    protection = dict(value)
    if "stop_required" in protection and not isinstance(
        protection["stop_required"], bool
    ):
        raise RiskPolicyError("risk_policy.protection.stop_required must be a boolean")
    return protection


def validate_risk_policy(payload: Any) -> Optional[Dict[str, Any]]:
    """Validate and normalise an author-supplied ``risk_policy``.

    ``None`` means the version declares no policy at all (the options lane then
    refuses by name). An empty object is a real, if permissive, declaration:
    every field is optional, so an author who declares ``{}`` has said "no
    numeric limits and no naked permission". Unknown fields and malformed
    values are refused, each naming the offending field.
    """
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise RiskPolicyError("risk_policy must be a JSON object")
    unknown = sorted(set(payload) - set(DECLARED_FIELDS))
    if unknown:
        raise RiskPolicyError(
            "unsupported risk_policy fields: " + ", ".join(unknown)
        )
    declared: Dict[str, Any] = {}
    for field in ("max_loss_inr", "notional_limit_inr", "margin_limit_inr"):
        if field in payload and payload[field] is not None:
            declared[field] = _positive_number(payload[field], field=field)
    if "protection" in payload and payload["protection"] is not None:
        declared["protection"] = _validate_protection(payload["protection"])
    if "expiry_policy" in payload and payload["expiry_policy"] is not None:
        declared["expiry_policy"] = _choice_list(
            payload["expiry_policy"], field="expiry_policy", allowed=ALLOWED_EXPIRY_POLICIES
        )
    if (
        "allowed_structure_families" in payload
        and payload["allowed_structure_families"] is not None
    ):
        declared["allowed_structure_families"] = _choice_list(
            payload["allowed_structure_families"],
            field="allowed_structure_families",
            allowed=STRUCTURE_FAMILIES,
        )
    if "naked_permitted" in payload and payload["naked_permitted"] is not None:
        if not isinstance(payload["naked_permitted"], bool):
            raise RiskPolicyError("risk_policy.naked_permitted must be a boolean")
        declared["naked_permitted"] = bool(payload["naked_permitted"])
    return declared


# ---------------------------------------------------------------------------
# platform ceiling + operator policy
# ---------------------------------------------------------------------------


def _ceiling_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _ceiling_list(raw: Optional[str]) -> List[str]:
    if raw is None:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def platform_risk_ceiling(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The platform's own ceilings, from configuration. Absent means no ceiling.

    A malformed numeric ceiling is ignored rather than read as zero: a typo in
    an operator's configuration must not ground every strategy. A malformed
    allow-list, by contrast, is narrowed to the values the platform recognises,
    so an unknown family can never be *permitted* by that list.
    """
    env = os.environ if environ is None else environ
    ceiling: Dict[str, Any] = {}
    for field, name in _CEILING_ENV.items():
        try:
            value = _positive_number(env.get(name), field=field)
        except RiskPolicyError:
            continue
        ceiling[field] = value
    families = _ceiling_list(env.get(_CEILING_FAMILIES_ENV))
    if families:
        ceiling["allowed_structure_families"] = [
            family for family in families if family in STRUCTURE_FAMILIES
        ]
    expiry = _ceiling_list(env.get(_CEILING_EXPIRY_ENV))
    if expiry:
        ceiling["expiry_policy"] = [
            policy for policy in expiry if policy in ALLOWED_EXPIRY_POLICIES
        ]
    naked = _ceiling_bool(env.get(_CEILING_NAKED_ENV))
    if naked is not None:
        ceiling["naked_permitted"] = naked
    stop = _ceiling_bool(env.get(_CEILING_STOP_ENV))
    if stop is not None:
        ceiling["protection"] = {"stop_required": stop}
    return ceiling


def operator_risk_policy(policy: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The operator's own axes, re-expressed as additional ceilings.

    Only the axes that genuinely bound a *structure* are carried across. The
    allocation and per-instrument/gross notional axes keep their own dedicated
    admission checks (``ALLOCATION_EXCEEDED`` and friends); repeating them here
    under a second name would make one violation look like two different
    controls. The daily-loss budget bounds a single structure's worst case, and
    the gross notional budget bounds a structure's notional, so both are real
    ceilings that only tighten.
    """
    if not isinstance(policy, Mapping):
        return {}
    operator: Dict[str, Any] = {}
    gross = policy.get("gross_notional_inr")
    if gross is not None:
        operator["notional_limit_inr"] = float(gross)
    budget = policy.get("daily_loss_budget_inr")
    if budget is not None:
        operator["max_loss_inr"] = float(budget)
    return operator


# ---------------------------------------------------------------------------
# the effective policy
# ---------------------------------------------------------------------------


def _optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _intersect_sets(values: Sequence[Any]) -> Optional[List[str]]:
    present: List[Set[str]] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            present.append({value})
        elif isinstance(value, (list, tuple, set, frozenset)):
            present.append({str(item) for item in value})
    if not present:
        return None
    intersection = set(present[0])
    for item in present[1:]:
        intersection &= item
    return sorted(intersection)


def _merge_protection(parts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    stop_required = False
    for part in parts:
        protection = part.get("protection")
        if not isinstance(protection, Mapping):
            continue
        for key, value in protection.items():
            if key == "stop_required":
                # A requirement tightens when ANY level demands it.
                stop_required = stop_required or bool(value)
                continue
            merged.setdefault(key, value)
    if stop_required:
        merged["stop_required"] = True
    return merged


def effective_risk_policy(
    declared: Optional[Mapping[str, Any]],
    operator: Optional[Mapping[str, Any]] = None,
    ceiling: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``min(declared, operator, ceiling)`` - the policy admission enforces.

    Numeric limits take the minimum of every level that states one, allow-lists
    intersect, ``naked_permitted`` is AND (granted only where every level that
    states it grants it; when no level states it, the result is ``None``), and
    ``protection.stop_required`` is OR (required as soon as any level requires
    it). Absent values never widen an explicitly stated one.
    """
    parts: List[Mapping[str, Any]] = [
        part for part in (declared, operator, ceiling) if isinstance(part, Mapping)
    ]

    def minimum(field: str) -> Optional[float]:
        values = [
            value
            for value in (_optional_float(part.get(field)) for part in parts)
            if value is not None
        ]
        return min(values) if values else None

    naked_values = [
        bool(part["naked_permitted"])
        for part in parts
        if "naked_permitted" in part
    ]
    naked_permitted = all(naked_values) if naked_values else None

    return {
        "max_loss_inr": minimum("max_loss_inr"),
        "notional_limit_inr": minimum("notional_limit_inr"),
        "margin_limit_inr": minimum("margin_limit_inr"),
        "protection": _merge_protection(parts),
        "expiry_policy": _intersect_sets([part.get("expiry_policy") for part in parts]),
        "allowed_structure_families": _intersect_sets(
            [part.get("allowed_structure_families") for part in parts]
        ),
        "naked_permitted": naked_permitted,
    }


# ---------------------------------------------------------------------------
# structure family + worst-case loss (frozen legs only)
# ---------------------------------------------------------------------------


def _strike(leg: Mapping[str, Any]) -> Optional[float]:
    return _optional_float(leg.get("strike"))


def _signed_quantity(leg: Mapping[str, Any]) -> Optional[int]:
    """One frozen leg's SIGNED magnitude, or ``None`` when it cannot be read."""
    for key in ("signed_quantity", "quantity"):
        try:
            value = int(leg.get(key))
        except (TypeError, ValueError):
            continue
        if key == "quantity":
            return abs(value) if str(leg.get("side") or "BUY").upper() != "SELL" else -abs(value)
        return value
    return None


def _option_type(leg: Mapping[str, Any]) -> str:
    return str(leg.get("option_type") or leg.get("instrument_type") or "").strip().upper()


def _expiry(leg: Mapping[str, Any]) -> Optional[str]:
    value = leg.get("expiry")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _side(leg: Mapping[str, Any]) -> str:
    return str(leg.get("side") or "BUY").strip().upper()


def _same_expiry(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Treat two omitted expiries as the same frozen expiry.

    Older frozen test fixtures omitted expiry because all legs were implicitly
    one expiry. A calendar/diagonal, on the other hand, must name both expiry
    values so it cannot be inferred from an absent field.
    """
    return _expiry(first) == _expiry(second)


def _same_type(legs: Sequence[Mapping[str, Any]]) -> bool:
    types = {_option_type(leg) for leg in legs}
    return len(types) == 1 and "" not in types


def _butterfly(legs: Sequence[Mapping[str, Any]]) -> bool:
    if len(legs) != 3 or not _same_type(legs):
        return False
    expiries = {_expiry(leg) for leg in legs}
    if len(expiries) != 1:
        return False
    ordered = sorted(legs, key=lambda leg: (_strike(leg) is None, _strike(leg)))
    strikes = [_strike(leg) for leg in ordered]
    if None in strikes or len(set(strikes)) != 3:
        return False
    quantities = [_signed_quantity(leg) for leg in ordered]
    if any(quantity is None for quantity in quantities):
        return False
    magnitudes = [abs(int(quantity)) for quantity in quantities]
    if not (magnitudes[0] == magnitudes[2] and magnitudes[1] == 2 * magnitudes[0]):
        return False
    return (
        quantities[0] != 0
        and quantities[0] == quantities[2]
        and quantities[1] == -quantities[0] * 2
    )


def classify_structure_family(legs: Sequence[Mapping[str, Any]]) -> str:
    """The frozen structure's family, from its option geometry and quantities.

    Anything the classifier cannot name is ``custom`` - never silently the
    nearest shape, because an allow-list that admits by accident is not an
    allow-list.
    """
    option_legs = [leg for leg in legs if isinstance(leg, Mapping)]
    if not option_legs:
        return "custom"
    if len(option_legs) == 1:
        side = _side(option_legs[0])
        return "short_single" if side == "SELL" else "long_single"
    if len(option_legs) == 2:
        first, second = option_legs
        first_type, second_type = _option_type(first), _option_type(second)
        first_strike, second_strike = _strike(first), _strike(second)
        first_side, second_side = _side(first), _side(second)
        first_quantity, second_quantity = _signed_quantity(first), _signed_quantity(second)
        same_strike = (
            first_strike is not None
            and second_strike is not None
            and first_strike == second_strike
        )
        if first_type != second_type:
            return "straddle" if same_strike else "strangle"
        if (
            not same_strike
            and first_quantity is not None
            and second_quantity is not None
            and abs(first_quantity) != abs(second_quantity)
        ):
            return "ratio_spread"
        if first_side != second_side:
            if (
                not same_strike
                and _expiry(first) is not None
                and _expiry(second) is not None
                and not _same_expiry(first, second)
            ):
                return "diagonal"
            if same_strike and _expiry(first) is not None and _expiry(second) is not None:
                if not _same_expiry(first, second):
                    return "calendar"
            # Opposite sides on one option type: same strike closes itself out,
            # a different strike is a spread.
            return "custom" if same_strike else "vertical_spread"
        return "custom"
    if len(option_legs) == 3 and _butterfly(option_legs):
        return "butterfly"
    if len(option_legs) == 4:
        shorts = [leg for leg in option_legs if str(leg.get("side") or "").strip().upper() == "SELL"]
        longs = [leg for leg in option_legs if str(leg.get("side") or "").strip().upper() == "BUY"]
        if len(shorts) == 2 and len(longs) == 2:
            short_ce = _find(shorts, "CE")
            short_pe = _find(shorts, "PE")
            long_ce = _find(longs, "CE")
            long_pe = _find(longs, "PE")
            if None not in (short_ce, short_pe, long_ce, long_pe):
                short_ce_strike, short_pe_strike = _strike(short_ce), _strike(short_pe)
                long_ce_strike, long_pe_strike = _strike(long_ce), _strike(long_pe)
                if None not in (short_ce_strike, short_pe_strike, long_ce_strike, long_pe_strike):
                    if short_ce_strike == short_pe_strike:
                        if long_ce_strike > short_ce_strike and long_pe_strike < short_pe_strike:
                            return "iron_butterfly"
                    elif (
                        long_ce_strike > short_ce_strike
                        and long_pe_strike < short_pe_strike
                    ):
                        return "iron_condor"
        return "custom"
    return "custom"


def _find(legs: Sequence[Mapping[str, Any]], option_type: str) -> Optional[Mapping[str, Any]]:
    for leg in legs:
        if _option_type(leg) == option_type:
            return leg
    return None


def _expiry_payoff(rows: Sequence[tuple], spot: float) -> float:
    total = 0.0
    for option_type, strike, quantity, _reference_price in rows:
        intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
        total += quantity * intrinsic
    return total


def _reference_price(leg: Mapping[str, Any]) -> Optional[float]:
    try:
        value = float(leg.get("reference_price"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def worst_case_loss_inr(legs: Sequence[Mapping[str, Any]]) -> Optional[float]:
    """The frozen target's worst-case LOSS at expiry, including entry premium.

    The premium is the signed cash flow at entry: a BUY pays ``quantity ×
    reference_price`` while a SELL receives it. The expiry P&L is therefore the
    intrinsic payoff minus the signed premium. The piecewise-linear payoff is
    minimised at zero, every strike, and the upper tail after the last strike.

    ``None`` means the loss is UNBOUNDED, or the frozen legs cannot be read well
    enough to bound it - a net short call has no upper bound, and an unreadable
    leg is never proof of coverage. Admission distinguishes missing premiums by
    name and requires a frozen option protection exit rule for an unbounded
    structure rather than inventing a numeric ceiling.
    """
    rows: List[tuple] = []
    strikes: List[float] = []
    for leg in legs:
        if not isinstance(leg, Mapping):
            return None
        option_type = _option_type(leg)
        strike = _strike(leg)
        quantity = _signed_quantity(leg)
        reference_price = _reference_price(leg)
        if (
            option_type not in ("CE", "PE")
            or strike is None
            or quantity is None
            or reference_price is None
        ):
            return None
        rows.append((option_type, strike, quantity, reference_price))
        strikes.append(strike)
    if not rows:
        return 0.0
    net_calls = sum(
        quantity for option_type, _strike_value, quantity, _reference_price in rows
        if option_type == "CE"
    )
    if net_calls < 0:
        # A net short call loses without bound as the underlying rises. A long
        # call above it would have cancelled this, so there is nothing to sample.
        return None
    entry_premium = sum(
        quantity * reference_price
        for _option_type_value, _strike_value, quantity, reference_price in rows
    )
    points: Set[float] = {0.0, *strikes, max(strikes) * 10.0 + 1000.0}
    worst = min(_expiry_payoff(rows, spot) - entry_premium for spot in points)
    return float(-worst) if worst < 0 else 0.0


def frozen_protection_stop(protection_policy: Any) -> bool:
    """Whether a frozen policy carries an option-metric exit rule.

    These are the metrics the option protection engine can evaluate and use to
    submit an exit. Generic legacy keys (for example ``stop_loss_pct``) are not
    enough evidence for an option structure's naked-loss safety gate.
    """
    if not isinstance(protection_policy, Mapping):
        return False
    protected_metrics = {
        "index_ltp",
        "combined_premium",
        "combined_premium_change_pct",
        "strategy_mtm",
        "net_delta",
    }
    rules = protection_policy.get("rules")
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        metric = str(rule.get("metric") or "").strip()
        action = str(rule.get("action") or "exit").strip().lower()
        if metric in protected_metrics and action == "exit":
            return True
    return False


def unhedged_target(plan: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The naked-coverage violation of a frozen target, ignoring its own policy.

    Reuses the adjustment gate's own coverage rule
    (``option_adjust_would_unhedge``) against the TARGET state, but with the
    frozen ``protection_policy.naked`` declaration removed from the probe. The
    caller then applies explicit policy denial and the option protection exit
    rule as the separate admission controls.
    """
    from backend.options.execution.plan_binding import option_adjust_would_unhedge

    resolved = dict(plan.get("resolved_plan") or {})
    policy = resolved.get("protection_policy")
    if isinstance(policy, Mapping):
        probe_policy = {key: value for key, value in policy.items() if key != "naked"}
        resolved["protection_policy"] = probe_policy
    probe = dict(plan)
    probe["resolved_plan"] = resolved
    return option_adjust_would_unhedge(probe)
