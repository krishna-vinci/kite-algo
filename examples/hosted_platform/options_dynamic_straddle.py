"""Example 3 - a delta-neutral short straddle with protective wings, managed
dynamically across supervised evaluations.

The structure is always the SAME shape: a short at-the-money call and a short
at-the-money put (the straddle), each covered by a further out-of-the-money long
wing of the same option type. Two shorts plus two hedges means the position is
never naked, and the wings bound its worst case.

Each evaluation reads the option chain and Greeks through the platform's own data
access, reads the option run THIS strategy already owns from
``owned_work()["option_runs"]``, and submits exactly ONE desired state:

* nothing held -> ``entry`` at ``base_units``;
* held and its net delta is beyond ``resize_delta_threshold`` (or the harness
  forces it) -> ``adjust`` (a resize) at ``resize_units``, frozen against the
  generation it just observed;
* held and inside ``roll_days_to_expiry`` of its expiry (or ``roll_to_expiry``
  names the next one) -> ``adjust`` (an expiry roll) with the SAME legs and roles
  on the next expiry;
* ``exit_position`` -> the governed ``exit``.

The strategy never places an order itself: every order is the platform's, from a
frozen plan the platform compiled, validated, admitted and sequenced. The
generation basis on every adjustment is the run generation from the SAME read
that produced the decision, so a stale-basis refusal is the platform's answer to
a run that moved - never a basis this example guessed.

Three evaluation-boundary parameters exist for the whole-platform acceptance
harness, and none changes what the strategy is allowed to do:

* ``force_resize`` fires the declared resize without waiting for the delta
  threshold, so a deterministic evaluation sequence can exercise the resize;
* ``duplicate_entry_probe`` and ``stale_basis_probe`` ASK the platform whether a
  SECOND entry for the held structure, and an adjustment frozen against an OLD
  generation, would be admitted. Both are refused; neither places an order.

Stale or missing quotes/Greeks, an unpublished book, an unreadable option-run
snapshot and a missing lot or delta are named no-action states: the example says
why it did nothing instead of guessing a strike, a price or a size.
"""

from __future__ import annotations

import sys
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_TERMINAL_REQUEST_STATES = {"executed", "refused", "rejected", "dispatch_unresolved"}

#: The durable option-run status vocabulary (``backend.options.execution.models``).
#: A run that is none of these is UNKNOWN: not closed, not flat, no re-entry.
_CLOSED_RUN_STATUSES = {"exited", "settled", "closed", "cancelled", "canceled", "rejected"}
_OPEN_RUN_STATUSES = {
    "created",
    "entry_previewed",
    "entering",
    "entered",
    "adjusting",
    "partial_entry",
    "cleanup_required",
    "exit_previewed",
    "exiting",
    "partial_exit",
}


def _say(ctx, text: str) -> None:  # noqa: ANN001
    """Report progress, capped to the platform's 200-character note bound."""
    print(f"[strategy] {text}", file=sys.stderr, flush=True)
    ctx.progress(str(text)[:200])


def _number(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _freshness(service_payload: Dict[str, Any], *, max_age_seconds: float) -> Tuple[bool, str]:
    """Whether a market read is provably current (the B1 example's own rule).

    A non-empty chain is NOT freshness. The platform stamps these reads with
    ``updated_at`` and surfaces an upstream ``resource_error``; a missing or
    unparsable timestamp is "not proven", never "assume current".
    """
    if not isinstance(service_payload, dict):
        return False, "no payload"
    error = service_payload.get("resource_error")
    if error:
        return False, f"the provider reported resource_error={error}"
    stamp = service_payload.get("updated_at")
    if not stamp:
        return False, "the payload carries no updated_at stamp"
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False, f"updated_at is not an ISO timestamp ({stamp})"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - moment).total_seconds()
    if age > max_age_seconds:
        return False, f"updated_at is {int(age)}s old (max {int(max_age_seconds)}s)"
    return True, ""


def _packet(row: Dict[str, Any], option_type: str) -> Optional[Dict[str, Any]]:
    """One chain row's call or put packet, or ``None`` when it is not usable.

    A leg needs a broker token, a symbol and a premium. Greeks are checked
    separately, because a missing Greek is a no-action state rather than a reason
    to guess a strike.
    """
    packet = row.get(option_type.lower()) or row.get(option_type.upper())
    if not isinstance(packet, dict):
        return None
    token = packet.get("token") or packet.get("instrument_token")
    premium = _number(packet.get("ltp"))
    symbol = str(packet.get("tsym") or packet.get("tradingsymbol") or "")
    if token is None or premium is None or not symbol:
        return None
    return {
        "token": int(token),
        "tsym": symbol,
        "ltp": premium,
        "iv": _number(packet.get("iv")),
        "delta": _number(packet.get("delta")),
        "oi": _number(packet.get("oi")),
    }


def _by_strike(chain_rows: List[Dict[str, Any]]) -> Dict[float, Dict[str, Any]]:
    rows: Dict[float, Dict[str, Any]] = {}
    for row in chain_rows:
        strike = _number(row.get("strike"))
        if strike is not None:
            rows[float(strike)] = row
    return rows


def _by_symbol(chain_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for row in chain_rows:
        for option_type in ("CE", "PE"):
            packet = _packet(row, option_type)
            if packet is not None:
                rows[packet["tsym"].upper()] = packet
    return rows


def _greeks_by_strike(greeks: Dict[str, Any]) -> Dict[float, Dict[str, Any]]:
    rows: Dict[float, Dict[str, Any]] = {}
    for row in list(greeks.get("contracts") or []):
        if not isinstance(row, dict):
            continue
        strike = _number(row.get("strike"))
        if strike is not None:
            rows[float(strike)] = row
    return rows


def _greek_contract(
    greeks_by_strike: Dict[float, Dict[str, Any]], strike: float, option_type: str
) -> Optional[Dict[str, Any]]:
    row = greeks_by_strike.get(float(strike))
    if not isinstance(row, dict):
        return None
    packet = row.get(option_type.lower()) or row.get(option_type.upper())
    return dict(packet) if isinstance(packet, dict) else None


def _nearest(strikes: List[float], target: float) -> Optional[float]:
    if not strikes:
        return None
    return min(strikes, key=lambda strike: abs(float(strike) - float(target)))


# -- the run this strategy owns ---------------------------------------------


def _run_state(run: Dict[str, Any]) -> str:
    """``open`` / ``closed`` / ``unknown`` from the run's OWN durable status."""
    status = str(run.get("status") or "").strip().lower()
    if status in _CLOSED_RUN_STATUSES:
        return "closed"
    if status in _OPEN_RUN_STATUSES:
        return "open"
    return "unknown"


def _held_run(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The run that still holds (or may still hold) the structure, or ``None``."""
    for row in runs:
        if _run_state(row) != "closed":
            return row
    return None


def _run_generation(run: Dict[str, Any]) -> int:
    """The leg generation the run HOLDS now, from the platform's own snapshot."""
    try:
        generation = int(run.get("structure_generation") or 1)
    except (TypeError, ValueError):
        return 1
    return generation if generation >= 1 else 1


def _run_legs(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [leg for leg in list(run.get("legs") or []) if isinstance(leg, dict)]


def _run_expiry(run: Dict[str, Any]) -> str:
    """The expiry the run HOLDS: its own legs' expiry, never a stale edge field."""
    expiries = {
        str(leg.get("expiry_key") or "").strip()
        for leg in _run_legs(run)
        if str(leg.get("expiry_key") or "").strip()
    }
    return expiries.pop() if len(expiries) == 1 else ""


def _run_units(run: Dict[str, Any]) -> int:
    """How many structure units the run holds, from its own legs' own size."""
    units = set()
    for leg in _run_legs(run):
        lot = int(_number(leg.get("lot_size"), 0.0) or 0)
        quantity = abs(int(_number(leg.get("quantity"), 0.0) or 0))
        if lot <= 0:
            continue
        units.add(quantity // lot)
    units.discard(0)
    return units.pop() if len(units) == 1 else 0


# -- chain reads -------------------------------------------------------------


def _read_chain(
    ctx, underlying: str, *, expiry: Optional[str], max_age_seconds: float  # noqa: ANN001
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """A fresh chain and Greeks read for one expiry, or a named no-action."""
    chain = ctx.client.options.get_chain(underlying, expiry=expiry)
    fresh, why = _freshness(chain, max_age_seconds=max_age_seconds)
    if not fresh:
        _say(ctx, f"{underlying} option chain is not fresh ({why}); no action")
        return None, None
    rows = [row for row in list(chain.get("chain") or []) if isinstance(row, dict)]
    spot = _number(chain.get("spot_ltp"))
    resolved_expiry = str(chain.get("expiry") or "")
    if not rows or spot is None or not resolved_expiry:
        _say(ctx, "the option chain has no rows, no spot price or no expiry; no action")
        return None, None
    greeks = ctx.client.options.get_greeks(underlying, expiry=resolved_expiry)
    fresh, why = _freshness(greeks, max_age_seconds=max_age_seconds)
    if not fresh:
        _say(ctx, f"Greeks are not fresh for {resolved_expiry} ({why}); no action")
        return None, None
    if not list(greeks.get("contracts") or []):
        _say(ctx, f"Greeks are missing for {resolved_expiry}; no action")
        return None, None
    return {"rows": rows, "spot": spot, "expiry": resolved_expiry}, greeks


def _net_delta(legs: List[Dict[str, Any]], units: int) -> Optional[float]:
    """The structure's net delta from its composed legs and the live Greeks."""
    total = 0.0
    for leg in legs:
        delta = _number(leg.get("delta"))
        if delta is None:
            return None
        sign = 1.0 if str(leg.get("side") or "").upper() == "BUY" else -1.0
        total += sign * float(leg.get("ratio") or 1) * delta
    return total * max(1, int(units))


# -- the decision rule -------------------------------------------------------


def _days_to(expiry: str, today: date) -> Optional[int]:
    try:
        return (date.fromisoformat(str(expiry)[:10]) - today).days
    except ValueError:
        return None


def _roll_target(
    *, params: Dict[str, Any], held_expiry: str, expiries: List[str], today: date
) -> Optional[str]:
    """The expiry a roll should move to, or ``None`` when no roll is owed.

    ``roll_to_expiry`` names it explicitly (a declared operator choice, and the
    harness's deterministic lever); otherwise the nearest expiry AFTER the one
    held is chosen, and only once ``roll_days_to_expiry`` says the held expiry is
    close enough to leave.
    """
    declared = str(params.get("roll_to_expiry") or "").strip()
    if declared:
        return declared
    threshold = params.get("roll_days_to_expiry")
    if threshold is None:
        return None
    days = _days_to(held_expiry, today)
    if days is None or days > int(threshold):
        return None
    later = sorted(
        str(expiry) for expiry in expiries if str(expiry) > str(held_expiry) and str(expiry)
    )
    return later[0] if later else None


def _decide(
    *,
    params: Dict[str, Any],
    run: Optional[Dict[str, Any]],
    runs: List[Dict[str, Any]],
    held_expiry: str,
    held_units: int,
    net_delta: Optional[float],
    expiries: List[str],
    today: date,
) -> Dict[str, Any]:
    """The declared decision rule, with no side effect and no platform call.

    Precedence is exit, then roll, then resize: the explicit parameter wins, a
    roll preserves the size the run already holds, and a resize never changes the
    expiry. Every adjustment names the generation of the same read that produced
    it, which is the only basis the platform accepts.
    """
    if run is None:
        if runs:
            return {"action": "none", "reason": "the structure this strategy owns is already closed"}
        return {"action": "entry", "units": max(1, int(params.get("base_units") or 1))}

    state = _run_state(run)
    if state == "unknown":
        return {
            "action": "none",
            "reason": (
                "the option run status is not in the known vocabulary "
                f"({run.get('status')!r}); an unknown status is never managed"
            ),
        }
    if state == "closed":
        return {"action": "none", "reason": "the structure this strategy owns is already closed"}
    if not held_expiry or held_units <= 0:
        return {
            "action": "none",
            "reason": "the held run carries no readable expiry or unit size; no action",
        }
    generation = _run_generation(run)
    if bool(params.get("exit_position")):
        return {"action": "exit", "expiry": held_expiry, "units": held_units}
    target_expiry = _roll_target(
        params=params, held_expiry=held_expiry, expiries=expiries, today=today
    )
    if target_expiry is not None and target_expiry != held_expiry:
        return {
            "action": "roll",
            "expiry": target_expiry,
            "units": held_units,
            "based_on_generation": generation,
            "reason": (
                f"rolling {held_expiry} to {target_expiry} "
                f"({_days_to(held_expiry, today)}d to expiry)"
            ),
        }
    resize_units = params.get("resize_units")
    if resize_units is not None and int(resize_units) != held_units:
        threshold = _number(params.get("resize_delta_threshold"), 0.5) or 0.0
        forced = bool(params.get("force_resize"))
        over = net_delta is not None and abs(float(net_delta)) >= threshold
        if forced or over:
            return {
                "action": "resize",
                "expiry": held_expiry,
                "units": int(resize_units),
                "based_on_generation": generation,
                "reason": (
                    f"resizing {held_units} -> {int(resize_units)} units "
                    f"(net delta {net_delta}, threshold {threshold}, forced={forced})"
                ),
            }
    delta_note = "unreadable" if net_delta is None else round(float(net_delta), 4)
    return {
        "action": "none",
        "reason": (
            f"held {held_units} unit(s) on {held_expiry}; net delta {delta_note}, "
            "no resize or roll owed"
        ),
    }


# -- leg construction --------------------------------------------------------


def _entry_legs(
    chain: Dict[str, Any],
    greeks: Dict[str, Any],
    *,
    short_offset_points: float,
    wing_width_points: float,
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """The four frozen legs of the straddle with wings, or a named no-action."""
    rows = chain["rows"]
    spot = float(chain["spot"])
    by_strike = _by_strike(rows)
    greeks_by_strike = _greeks_by_strike(greeks)
    strikes = sorted(by_strike)
    short_strike = _nearest(strikes, spot + float(short_offset_points))
    if short_strike is None:
        return None, "no usable strike for the straddle"
    above = [strike for strike in strikes if strike >= short_strike + float(wing_width_points)]
    below = [strike for strike in strikes if strike <= short_strike - float(wing_width_points)]
    if not above or not below:
        return (
            None,
            "the chain carries no strikes at the declared wing width "
            f"({wing_width_points} points), so the structure cannot be hedged",
        )
    long_ce_strike = min(above)
    long_pe_strike = max(below)
    selection = [
        ("SELL", short_strike, "CE", "short"),
        ("SELL", short_strike, "PE", "short"),
        ("BUY", long_ce_strike, "CE", "hedge"),
        ("BUY", long_pe_strike, "PE", "hedge"),
    ]
    legs: List[Dict[str, Any]] = []
    for side, strike, option_type, role in selection:
        packet = _packet(by_strike.get(float(strike), {}), option_type)
        if packet is None:
            return None, f"no usable {option_type} contract at strike {strike}"
        greek = _greek_contract(greeks_by_strike, strike, option_type)
        if greek is None or _number(greek.get("delta")) is None:
            # Delta is the whole point of this example: without it there is no
            # honest way to say the structure is delta-neutral.
            return None, f"no delta for {option_type} {strike}; the structure cannot be measured"
        legs.append(
            {
                "instrument_token": packet["token"],
                "exchange": "NFO",
                "tradingsymbol": packet["tsym"],
                "side": side,
                "ratio": 1,
                "role": role,
                "option_type": option_type,
                "strike": float(strike),
                "ltp": packet["ltp"],
                "delta": _number(greek.get("delta")),
            }
        )
    return legs, ""


def _legs_from_run(
    run_legs: List[Dict[str, Any]], *, chain: Dict[str, Any], rolling: bool
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """The run's OWN legs as the desired state, re-priced from a fresh chain.

    A resize keeps every contract it holds; a roll resolves the SAME strike and
    option type on the target expiry, so the legs and roles are unchanged and only
    the expiry moves.
    """
    by_symbol = _by_symbol(chain["rows"])
    by_strike = _by_strike(chain["rows"])
    legs: List[Dict[str, Any]] = []
    for leg in run_legs:
        side = str(leg.get("transaction_type") or "").upper()
        option_type = str(leg.get("option_type") or "").upper()
        strike = _number(leg.get("strike"))
        ratio = int((leg.get("metadata") or {}).get("ratio") or 0) or 1
        if side not in {"BUY", "SELL"} or option_type not in {"CE", "PE"} or strike is None:
            return None, f"the held leg {leg.get('tradingsymbol')} is not readable as a leg"
        if rolling:
            row = by_strike.get(float(strike))
            packet = None if row is None else _packet(row, option_type)
        else:
            packet = by_symbol.get(str(leg.get("tradingsymbol") or "").upper())
        if packet is None:
            return (
                None,
                f"no usable live {option_type} contract for {leg.get('tradingsymbol')} "
                f"on {chain['expiry']}",
            )
        legs.append(
            {
                "instrument_token": packet["token"],
                "exchange": str(leg.get("exchange") or "NFO"),
                "tradingsymbol": packet["tsym"],
                "side": side,
                "ratio": ratio,
                "role": "short" if side == "SELL" else "hedge",
                "option_type": option_type,
                "strike": float(strike),
                "ltp": packet["ltp"],
                "delta": packet["delta"],
            }
        )
    return (legs, "") if legs else (None, "the held run carries no frozen legs")


def _exit_legs(
    run_legs: List[Dict[str, Any]], chain: Dict[str, Any]
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """The closing direction of every leg the run holds, at live premiums."""
    by_symbol = _by_symbol(chain["rows"])
    closing: List[Dict[str, Any]] = []
    for leg in run_legs:
        side = str(leg.get("transaction_type") or "").upper()
        symbol = str(leg.get("tradingsymbol") or "").upper()
        packet = by_symbol.get(symbol)
        if side not in {"BUY", "SELL"} or packet is None:
            return None, f"no usable live premium for {leg.get('tradingsymbol')}; not closing"
        lot = int(_number(leg.get("lot_size"), 0.0) or 0)
        quantity = abs(int(_number(leg.get("quantity"), 0.0) or 0))
        if lot <= 0 or quantity <= 0:
            return None, f"{leg.get('tradingsymbol')} carries no lot size or held quantity"
        closing.append(
            {
                "instrument_token": packet["token"],
                "exchange": str(leg.get("exchange") or "NFO"),
                "tradingsymbol": packet["tsym"],
                "side": "SELL" if side == "BUY" else "BUY",
                "ratio": max(1, quantity // lot),
                "role": "short" if side == "BUY" else "hedge",
                "option_type": str(leg.get("option_type") or ""),
                "strike": _number(leg.get("strike"), 0.0),
                "ltp": packet["ltp"],
            }
        )
    return (closing, "") if closing else (None, "the held run carries no legs to close")


# -- proposals ---------------------------------------------------------------


def _identity(ctx) -> Optional[Dict[str, str]]:  # noqa: ANN001
    identity = ctx.run.attribution()
    strategy_id = str(identity.get("strategy_id") or "")
    if not identity.get("attributed") or not strategy_id:
        _say(ctx, "no action: this run has no persisted strategy binding")
        return None
    return {
        "strategy_id": strategy_id,
        "account_scope": str(identity.get("account_id") or ctx.run.config.account_scope),
    }


def _proposal(
    ctx,  # noqa: ANN001
    identity: Dict[str, str],
    *,
    phase: str,
    underlying: str,
    expiry: str,
    product: str,
    expiry_policy: str,
    structure_id: str,
    legs: List[Dict[str, Any]],
    units: Optional[int] = None,
    option_run_id: Optional[str] = None,
    based_on_generation: Optional[int] = None,
    suffix: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "underlying": underlying,
        "expiry": expiry,
        "product": product,
        "structure_id": structure_id,
        "expiry_policy": expiry_policy,
        "phase": phase,
        "legs": [
            {
                "instrument_token": leg["instrument_token"],
                "exchange": leg["exchange"],
                "tradingsymbol": leg["tradingsymbol"],
                "side": leg["side"],
                "ratio": int(leg["ratio"]),
                "role": leg["role"],
                "reference_price": leg["ltp"],
            }
            for leg in legs
        ],
    }
    if units is not None:
        payload["structure_units"] = int(units)
    if option_run_id:
        payload["option_run_id"] = str(option_run_id)
    if based_on_generation is not None:
        payload["based_on_generation"] = int(based_on_generation)
    return {
        "evaluation_id": f"dynamic-straddle-{phase}{suffix}-{ctx.run_id}",
        "evaluation_kind": "run_now",
        "strategy_id": identity["strategy_id"],
        "strategy_run_id": ctx.run_id,
        "account_scope": identity["account_scope"],
        "target_kind": "option_structure",
        "payload": payload,
    }


# -- submission --------------------------------------------------------------


def _request_row_until_terminal(  # noqa: ANN001
    ctx, request_id: str, deadline_seconds: float
) -> Dict[str, Any]:
    """The request's own TERMINAL row: ``queued``/``dispatching`` are not outcomes."""
    if not request_id:
        return {}
    started = time.monotonic()
    row: Dict[str, Any] = {}
    while time.monotonic() - started < deadline_seconds:
        row = dict(ctx.run.execution_request(request_id) or {})
        if str(row.get("status") or "") in _TERMINAL_REQUEST_STATES:
            return row
        time.sleep(2.0)
    return row


def _submit(
    ctx,  # noqa: ANN001
    proposal: Dict[str, Any],
    *,
    deadline_seconds: float,
    what: str,
) -> Dict[str, Any]:
    """Submit one governed proposal and wait for its authoritative outcome."""
    try:
        submitted = ctx.run.submit_and_request_execution(
            proposal, idempotency_key=str(proposal["evaluation_id"])
        )
    except Exception as exc:  # noqa: BLE001 - a refused plan is an answer, not a crash
        _say(ctx, f"{what} was refused before a request existed ({str(exc)[:120]})")
        return {}
    request = dict(submitted.get("execution_request") or {})
    _say(ctx, f"{what} requested: {request.get('request_id')} status={request.get('status')}")
    return _request_row_until_terminal(ctx, str(request.get("request_id") or ""), deadline_seconds)


def _describe(row: Dict[str, Any]) -> str:
    return (
        f"status={row.get('status') or 'unknown'} "
        f"refusal_code={row.get('refusal_code') or 'none'}"
    )


def _owned_runs(ctx, underlying: str) -> Optional[List[Dict[str, Any]]]:  # noqa: ANN001
    """Every option run this strategy owns for the underlying, or ``None``.

    An unreadable or truncated discovery is ``None``: an unknown read is never
    treated as "no run", because that is how a second structure gets opened.
    """
    snapshot = ctx.run.owned_work()
    coverage = dict(snapshot.get("option_runs_coverage") or {})
    if str(coverage.get("coverage") or "unknown") != "known":
        _say(
            ctx,
            "no action: this strategy's option runs could not be read completely "
            f"(reason={coverage.get('reason') or 'unknown'}); refusing to guess",
        )
        return None
    deduped: Dict[str, Dict[str, Any]] = {}
    for row in list(snapshot.get("option_runs") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("underlying") or "").upper() != underlying:
            continue
        deduped.setdefault(str(row.get("option_run_id") or ""), row)
    return list(deduped.values())


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _chain_expiries(ctx, underlying: str, *, front: str) -> List[str]:  # noqa: ANN001
    payload = ctx.client.options.list_expiries(underlying)
    expiries = [str(value) for value in list(payload.get("expiries") or []) if str(value)]
    if front and front not in expiries:
        expiries.append(front)
    return sorted(dict.fromkeys(expiries))


def _finish_held(ctx, underlying: str, action: str) -> int:  # noqa: ANN001
    """Finish this evaluation with the structure HELD, naming its own platform state."""
    runs = _owned_runs(ctx, underlying)
    if runs is None:
        return 0
    run = _held_run(runs)
    if run is None:
        _say(ctx, f"unresolved: no held option run is readable after {action}")
        return 2
    _say(
        ctx,
        "dynamic straddle held: "
        f"generation={_run_generation(run)} units={_run_units(run)} "
        f"expiry={_run_expiry(run)} status={run.get('status')} after {action}",
    )
    return 0


def _verify_landed(
    ctx, underlying: str, *, expected_generation: int  # noqa: ANN001
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Re-read the platform's own record and require the adjustment to have LANDED."""
    runs = _owned_runs(ctx, underlying)
    if runs is None:
        return False, None
    run = _held_run(runs)
    if run is None:
        _say(ctx, "unresolved: no held option run is readable after the adjustment")
        return False, None
    generation = _run_generation(run)
    status = str(run.get("status") or "")
    if status != "entered" or generation != int(expected_generation):
        _say(
            ctx,
            "unresolved: after the adjustment the run is "
            f"status={status!r} generation={generation} (wanted entered/{expected_generation})",
        )
        return False, run
    return True, run


def main(ctx) -> int:  # noqa: ANN001 - the hosted contract is main(ctx)
    params: Dict[str, Any] = dict(ctx.params or {})
    underlying = str(params.get("underlying") or "NIFTY").strip().upper()
    product = str(params.get("product") or "NRML").strip().upper()
    expiry_policy = str(params.get("expiry_policy") or "exit_before_cutoff")
    deadline_seconds = float(params.get("deadline_seconds") or 120)
    max_age_seconds = float(params.get("quote_max_age_seconds") or 300)

    runs = _owned_runs(ctx, underlying)
    if runs is None:
        return 0
    run = _held_run(runs)
    held_expiry = _run_expiry(run) if run is not None else ""
    held_units = _run_units(run) if run is not None else 0

    # ONE chain/Greeks read for the run the strategy holds (or the front expiry
    # when nothing is held). The decision, the desired legs and the frozen
    # generation basis all come from this same read.
    chain, greeks = _read_chain(
        ctx, underlying, expiry=held_expiry or None, max_age_seconds=max_age_seconds
    )
    if chain is None or greeks is None:
        return 0
    expiries = _chain_expiries(ctx, underlying, front=str(chain["expiry"]))

    short_offset = _number(params.get("short_offset_points"), 0.0) or 0.0
    wing_width = _number(params.get("wing_width_points"), 250.0) or 0.0
    if run is None:
        legs, why = _entry_legs(
            chain, greeks, short_offset_points=short_offset, wing_width_points=wing_width
        )
        net_delta = None
    else:
        legs, why = _legs_from_run(_run_legs(run), chain=chain, rolling=False)
        net_delta = _net_delta(legs, held_units) if legs is not None else None
    if legs is None:
        _say(ctx, f"no action: {why}")
        return 0

    decision = _decide(
        params=params,
        run=run,
        runs=runs,
        held_expiry=held_expiry,
        held_units=held_units or max(1, int(params.get("base_units") or 1)),
        net_delta=net_delta,
        expiries=expiries,
        today=_today(),
    )
    if decision["action"] == "none":
        _say(ctx, f"no action: {decision['reason']}")
        return 0

    identity = _identity(ctx)
    if identity is None:
        return 0
    unit_size = int(decision.get("units") or max(1, int(params.get("base_units") or 1)))
    structure_id = (
        f"dynamic-straddle-{underlying}-{chain['expiry']}"
        if run is None
        else str(run.get("structure_id") or f"dynamic-straddle-{underlying}")
    )

    if decision["action"] == "entry":
        proposal = _proposal(
            ctx,
            identity,
            phase="entry",
            underlying=underlying,
            expiry=str(chain["expiry"]),
            product=product,
            expiry_policy=expiry_policy,
            structure_id=structure_id,
            legs=legs,
            units=unit_size,
        )
        row = _submit(ctx, proposal, deadline_seconds=deadline_seconds, what="entry")
        _say(ctx, f"entry answer {_describe(row)}")
        if str(row.get("status") or "") != "executed":
            _say(ctx, "no action: the entry did not reach an executed outcome")
            return 0
        return _finish_held(ctx, underlying, "entry")

    if decision["action"] == "exit":
        closing, why = _exit_legs(_run_legs(run), chain)
        if closing is None:
            _say(ctx, f"no action: {why}")
            return 0
        proposal = _proposal(
            ctx,
            identity,
            phase="exit",
            underlying=underlying,
            expiry=held_expiry or str(chain["expiry"]),
            product=product,
            expiry_policy=expiry_policy,
            structure_id=structure_id,
            legs=closing,
            option_run_id=str(run.get("option_run_id") or ""),
        )
        row = _submit(ctx, proposal, deadline_seconds=deadline_seconds, what="exit")
        _say(ctx, f"exit answer {_describe(row)}")
        if str(row.get("status") or "") != "executed":
            _say(ctx, "no action: the exit did not reach an executed outcome")
            return 0
        remaining = _owned_runs(ctx, underlying)
        if remaining is None or _held_run(remaining) is not None:
            _say(ctx, "unresolved: the exit executed but the structure is not provably closed")
            return 2
        _say(
            ctx,
            f"dynamic straddle exited: {len(remaining)} option run(s) closed, "
            "no structure held",
        )
        return 0

    # resize or roll: ONE governed adjustment against the generation just observed.
    expected_generation = int(_run_generation(run)) + 1
    rolling = decision["action"] == "roll"
    target_expiry = str(decision["expiry"])
    if rolling and target_expiry != str(chain["expiry"]):
        rolled_chain, rolled_greeks = _read_chain(
            ctx, underlying, expiry=target_expiry, max_age_seconds=max_age_seconds
        )
        if rolled_chain is None or rolled_greeks is None:
            return 0
        chain = rolled_chain
        legs, why = _legs_from_run(_run_legs(run), chain=chain, rolling=True)
        if legs is None:
            _say(ctx, f"no action: {why}")
            return 0
    proposal = _proposal(
        ctx,
        identity,
        phase="adjust",
        underlying=underlying,
        expiry=str(chain["expiry"]),
        product=product,
        expiry_policy=expiry_policy,
        structure_id=structure_id,
        legs=legs,
        units=unit_size,
        option_run_id=str(run.get("option_run_id") or ""),
        based_on_generation=int(decision["based_on_generation"]),
    )
    _say(ctx, f"{decision['action']} decided: {decision['reason']}")
    row = _submit(
        ctx, proposal, deadline_seconds=deadline_seconds, what=f"{decision['action']} adjust"
    )
    _say(ctx, f"{decision['action']} answer {_describe(row)}")
    if str(row.get("status") or "") != "executed":
        _say(ctx, f"no action: the {decision['action']} did not reach an executed outcome")
        return 0

    landed, landed_run = _verify_landed(ctx, underlying, expected_generation=expected_generation)
    if not landed:
        return 2
    if bool(params.get("duplicate_entry_probe")):
        _say(ctx, _duplicate_entry_probe(ctx, identity, landed_run or run, legs, units=unit_size))
    if bool(params.get("stale_basis_probe")):
        _say(
            ctx,
            _stale_basis_probe(
                ctx, identity, landed_run or run, legs, deadline_seconds=deadline_seconds
            ),
        )
    return _finish_held(ctx, underlying, decision["action"])


def _duplicate_entry_probe(  # noqa: ANN001
    ctx,
    identity: Dict[str, str],
    run: Dict[str, Any],
    legs: List[Dict[str, Any]],
    *,
    units: int,
) -> str:
    """Ask the platform whether a SECOND entry for the held structure is admitted.

    The probe re-states the structure the run ALREADY holds, so the platform's own
    duplicate gate answers it. It places no order, and the example acts on nothing
    it hears.
    """
    proposal = _proposal(
        ctx,
        identity,
        phase="entry",
        underlying=str(run.get("underlying") or ""),
        expiry=_run_expiry(run),
        product=str(run.get("product") or "NRML"),
        expiry_policy=str(run.get("expiry_policy") or "exit_before_cutoff"),
        structure_id=str(run.get("structure_id") or ""),
        legs=legs,
        units=units,
        suffix="-probe",
    )
    try:
        submitted = ctx.run.submit_and_request_execution(
            proposal, idempotency_key=str(proposal["evaluation_id"])
        )
    except Exception as exc:  # noqa: BLE001 - the refusal is reported, never hidden
        return f"duplicate entry probe refused before a request existed ({str(exc)[:120]})"
    request = dict(submitted.get("execution_request") or {})
    request_id = str(request.get("request_id") or "")
    row = _request_row_until_terminal(ctx, request_id, 30.0)
    return f"duplicate entry probe answered {_describe(row)} request={request_id}"


def _stale_basis_probe(  # noqa: ANN001
    ctx,
    identity: Dict[str, str],
    run: Dict[str, Any],
    legs: List[Dict[str, Any]],
    *,
    deadline_seconds: float,
) -> str:
    """Ask the platform whether an adjustment frozen against an OLD generation lands.

    ``based_on_generation`` is deliberately one behind the run's held generation:
    an approved target is never re-derived against a newer structure. The probe
    places no order.
    """
    generation = _run_generation(run)
    if generation <= 1:
        return "stale basis probe skipped: the run is still on its first generation"
    proposal = _proposal(
        ctx,
        identity,
        phase="adjust",
        underlying=str(run.get("underlying") or ""),
        expiry=_run_expiry(run),
        product=str(run.get("product") or "NRML"),
        expiry_policy=str(run.get("expiry_policy") or "exit_before_cutoff"),
        structure_id=str(run.get("structure_id") or ""),
        legs=legs,
        units=_run_units(run) or 1,
        option_run_id=str(run.get("option_run_id") or ""),
        based_on_generation=max(1, generation - 1),
        suffix="-stale",
    )
    try:
        submitted = ctx.run.submit_and_request_execution(
            proposal, idempotency_key=str(proposal["evaluation_id"])
        )
    except Exception as exc:  # noqa: BLE001 - the refusal is reported, never hidden
        return f"stale basis probe refused before a request existed ({str(exc)[:120]})"
    request = dict(submitted.get("execution_request") or {})
    request_id = str(request.get("request_id") or "")
    row = _request_row_until_terminal(ctx, request_id, deadline_seconds)
    return f"stale basis probe answered {_describe(row)} request={request_id}"
