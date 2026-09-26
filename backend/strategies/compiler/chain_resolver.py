"""Production ``ChainResolver``: the live option chain, never a guess.

A RELATIVE leg (``ATM+N`` / ``ITMn`` / ``OTMn`` or ``delta_target``) names a
*policy*, not a contract. The policy only becomes a frozen leg by asking the
same canonical option-chain source the freeze-evidence check reads
(:mod:`backend.options.market.freshness`) -- never by re-deriving a strike from
data the compiler holds itself. A structure's ``expiry`` may itself be a
selector (``current_week`` / ``next_week`` / ``current_month``); it is resolved
to one concrete date here, once, before any leg is asked to match against it,
so every leg in the structure resolves against the same expiry and the frozen
plan never carries the selector text as if it were a date.

Anything that stops a resolution from being trustworthy -- no active session,
an unmatched selector, an unmatched contract -- fails closed: a
:class:`~backend.strategies.compiler.base.ValidationRefusal` for the
structure-level expiry, ``None`` for a single leg (which
``OptionStructureCompiler`` turns into ``SELECTION_POLICY_UNRESOLVABLE``).
Guessing would silently freeze a different structure than the one requested.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from backend.options.market.expiry_selectors import ExpirySelectorError, resolve_expiry_selector
from backend.strategies.compiler.base import ValidationRefusal
from backend.strategies.compiler.option_structure import ChainResolver


def _is_plain_date(value: str) -> bool:
    """``YYYY-MM-DD`` shaped, as opposed to a semantic selector like ``next_week``."""
    return (
        len(value) == 10
        and value[4] == "-"
        and value[7] == "-"
        and value[:4].isdigit()
        and value[5:7].isdigit()
        and value[8:10].isdigit()
    )


def resolve_structure_expiry(market_service: Any, *, underlying: str, expiry: Optional[str]) -> str:
    """One concrete ISO expiry date for the whole structure.

    An explicit ``YYYY-MM-DD`` passes through untouched (it names one date
    already). Anything else is a selector, resolved against the same live
    session the freeze-evidence check reads; no session, or a selector that
    matches nothing, refuses by name rather than freezing a guess.
    """
    text = str(expiry or "").strip()
    if not text:
        raise ValidationRefusal(
            "OPTION_EXPIRY_UNRESOLVABLE",
            {"underlying": underlying, "message": "A structure requires an expiry or expiry selector"},
        )
    if _is_plain_date(text):
        return text
    try:
        session = market_service.get_session(underlying)
    except Exception as exc:  # noqa: BLE001 - no/stale session source fails closed
        raise ValidationRefusal(
            "OPTION_CHAIN_SNAPSHOT_UNAVAILABLE",
            {"underlying": underlying, "expiry_selector": text, "reason": str(exc)},
        ) from exc
    expiries = (session or {}).get("expiries") or []
    try:
        resolved = resolve_expiry_selector(text, expiries)
    except ExpirySelectorError as exc:
        raise ValidationRefusal(
            "OPTION_EXPIRY_UNRESOLVABLE",
            {"underlying": underlying, "expiry_selector": text, "reason": str(exc)},
        ) from exc
    return resolved.isoformat()


def build_production_chain_resolver(market_service: Any) -> ChainResolver:
    """A :data:`ChainResolver` backed by :class:`OptionsMarketService.resolve_selection`.

    Delegated, never duplicated: the strike math (ATM/ITM/OTM offsets, delta
    targeting) already lives in ``backend.options.market.selection``. This
    adapter only translates the compiler's per-leg call into the service's
    payload shape and reads back one resolved contract. Any failure -- no
    session, an offset or delta that matches nothing -- returns ``None``,
    which the compiler turns into ``SELECTION_POLICY_UNRESOLVABLE``.
    """

    def resolve(
        *,
        underlying: str,
        expiry: Optional[str],
        option_type: str,
        moneyness: str = "ATM",
        offset: Optional[int] = None,
        delta_target: Optional[float] = None,
    ) -> Optional[Mapping[str, Any]]:
        leg_payload: Dict[str, Any] = {"option_type": option_type}
        if delta_target is not None:
            leg_payload["delta_target"] = delta_target
        else:
            kind = str(moneyness or "ATM").upper()
            leg_payload["offset"] = "ATM" if kind == "ATM" else f"{kind}{int(offset or 0)}"
        try:
            result = market_service.resolve_selection(
                underlying, {"expiry": expiry, "legs": [leg_payload]}
            )
        except Exception:  # noqa: BLE001 - any resolver failure is "no match"
            return None
        resolved = result.get("resolved") if isinstance(result, Mapping) else None
        if not resolved:
            return None
        contract = dict(resolved[0])
        contract.setdefault("exchange", "NFO")
        return contract

    return resolve
