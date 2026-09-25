"""Example 2 - options premiums/Greeks + an index-ticker setup, with one bounded
adjustment that never duplicates itself.

The decision chain, in order:

1. the INDEX TICKER's own candles drive the setup (an indicator over ``NIFTY 50``
   or ``BANKNIFTY`` - never a constituent-breadth substitute);
2. the option chain for the selected expiry is read for premiums and Greeks;
3. a supported DEFINED STRUCTURE (a bull call spread by default) is selected from
   that chain and **frozen explicitly**: both legs are resolved to real broker
   tokens before a proposal exists, so a moving chain can never redefine the
   position later. This example does not invent an arbitrary leg-replacement
   capability; it uses the frozen-leg contract the engine actually supports;
4. the entry is submitted through the governed request API, and the platform
   keeps the engine's own entry/exit/protection and lot rules;
5. the supported ADJUSTMENT is a governed CLOSE of the structure this strategy
   already owns: the run identity is discovered from
   ``owned_work()["option_runs"]`` (derived from this strategy's own bound
   attempts, never from a caller-supplied id), and the close is submitted as an
   ``option_structure`` plan in the ``exit`` phase that targets FLAT and closes
   short liabilities first. A repeat observation sees outstanding work and sends
   nothing. A fresh structure re-entry is only defensible after a proven close.

   Two evaluation-boundary parameters exist for the whole-platform acceptance
   harness, and neither changes what the strategy is allowed to do:

   * ``hold_after_entry`` stops this evaluation once its own entry has executed,
     leaving the structure HELD for a LATER supervised evaluation to close. That
     is the restart shape: a finite child is disposeable, its structure is not.
   * ``duplicate_entry_probe`` asks the platform, once, whether a second entry
     for the SAME held structure would be admitted. The strategy reports the
     answer it receives; it does not act on it, and the close that follows is the
     only order this evaluation submits.

Stale or missing Greeks/quotes, an unpublished book, an unreadable option-run
snapshot and a partially filled leg are named no-action states: the strategy says
why it did nothing instead of guessing.

"""

from __future__ import annotations

import time
import sys
from datetime import datetime, timezone
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


def _series_tail(values: Any, offset: int) -> Optional[float]:
    if isinstance(values, dict):
        for key in ("rsi", "ema", "sma", "value", "close"):
            series = values.get(key)
            if isinstance(series, list) and len(series) >= offset:
                return _number(series[-offset])
        return None
    if isinstance(values, list) and len(values) >= offset:
        return _number(values[-offset])
    return None


def _freshness(service_payload: Dict[str, Any], *, max_age_seconds: float) -> Tuple[bool, str]:
    """Whether a market read is provably current.

    A non-empty chain or Greeks payload is NOT freshness. The platform stamps
    these reads with ``updated_at`` and surfaces an upstream ``resource_error``
    when the session snapshot is not usable, so freshness is decided from those
    signals: a missing or unparsable timestamp is "not proven", never "assume
    current".
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
        text = str(stamp).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    except ValueError:
        return False, f"updated_at is not an ISO timestamp ({stamp})"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - moment).total_seconds()
    if age > max_age_seconds:
        return False, f"updated_at is {int(age)}s old (max {int(max_age_seconds)}s)"
    return True, ""


def _bars_from_candles(candles: Dict[str, Any]) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    for row in list(candles.get("candles") or []):
        if not isinstance(row, dict):
            continue
        bars.append(
            {
                "timestamp": row.get("timestamp") or row.get("time"),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": _number(row.get("close")),
                "volume": _number(row.get("volume"), 0.0),
                "is_complete": row.get("is_complete", True),
            }
        )
    return bars


def _leg(row: Dict[str, Any], side: str) -> Optional[Dict[str, Any]]:
    """One chain row's call or put packet, or ``None`` when it is not usable.

    A leg needs a broker token and a premium. Greeks are checked separately,
    because a missing Greek is a no-action state rather than a reason to guess a
    strike.
    """
    packet = row.get(side) or row.get(side.upper())
    if not isinstance(packet, dict):
        return None
    token = packet.get("token") or packet.get("instrument_token")
    premium = _number(packet.get("ltp"))
    if token is None or premium is None:
        return None
    return {
        "token": int(token),
        "tsym": str(packet.get("tsym") or packet.get("tradingsymbol") or ""),
        "ltp": premium,
        "iv": _number(packet.get("iv")),
        "delta": _number(packet.get("delta")),
        "oi": _number(packet.get("oi")),
    }


def _select_strike(
    chain_rows: List[Dict[str, Any]], spot: float, offset_points: float, side: str
) -> Optional[Tuple[float, Dict[str, Any]]]:
    """The usable strike nearest ``spot + offset_points`` on the requested side."""
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for row in chain_rows:
        strike = _number(row.get("strike"))
        if strike is None:
            continue
        packet = _leg(row, side)
        if packet is None:
            continue
        candidates.append((strike, packet))
    if not candidates:
        return None
    target = float(spot) + float(offset_points)
    return min(candidates, key=lambda item: abs(item[0] - target))


def _pending_adjustment(snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """ANY outstanding work of this strategy, whatever state it is in.

    The snapshot's pending rows are by definition work that has not reached a
    terminal, settled outcome. Whitelisting a few states would let an unfamiliar
    one read as "nothing outstanding", which is exactly how an adjustment gets
    duplicated, so the whole list blocks.
    """
    for row in list(snapshot.get("pending") or []):
        if not isinstance(row, dict):
            continue
        return row
    return None


def main(ctx) -> int:  # noqa: ANN001 - the hosted contract is main(ctx)
    params: Dict[str, Any] = dict(ctx.params or {})

    # The option chain is keyed by the bare underlying name; the index CANDLES
    # are read from that index's catalog coordinate (space-free).
    underlying = str(params.get("underlying") or "NIFTY").strip().upper()
    index_exchange = str(params.get("index_exchange") or "NSE").strip().upper()
    # Evaluation-boundary switches for the acceptance harness. They are NOT
    # permissions: neither one lets this strategy submit an order it could not
    # submit anyway. ``hold_after_entry`` ends the evaluation with the structure
    # held; ``duplicate_entry_probe`` only ASKS the platform whether a second
    # entry for the held structure would be admitted.
    hold_after_entry = bool(params.get("hold_after_entry"))
    duplicate_entry_probe = bool(params.get("duplicate_entry_probe"))
    probe_done = False
    index_ticker = str(
        params.get("index_ticker")
        or (underlying if ":" in underlying else f"{index_exchange}:{underlying}50")
    ).replace(" ", "")
    underlying = index_ticker.split(":")[-1].removesuffix("50") or underlying
    interval = str(params.get("interval") or "5minute")
    lookback = int(params.get("lookback") or 60)
    rsi_period = int(params.get("rsi_period") or 14)
    bullish_level = _number(params.get("bullish_rsi_level"), 55.0) or 55.0
    long_offset = _number(params.get("long_offset_points"), 0.0) or 0.0
    short_offset = _number(params.get("short_offset_points"), 100.0) or 100.0
    product = str(params.get("product") or "NRML").strip().upper()
    expiry_policy = str(params.get("expiry_policy") or "exit_before_cutoff")
    deadline_seconds = float(params.get("deadline_seconds") or 120)

    # -- 1. index-ticker setup ----------------------------------------------
    candles = ctx.client.get_candles(index_ticker, interval=interval, lookback=lookback)
    bars = _bars_from_candles(candles)
    if len(bars) < rsi_period + 2:
        _say(ctx, f"{underlying} returned {len(bars)} usable candles; no action")
        return 0
    rsi = ctx.client.calculate_indicator({"name": "rsi", "bars": bars, "period": rsi_period})
    if not rsi.get("ready"):
        _say(ctx, f"rsi({rsi_period}) is not ready yet; no action")
        return 0
    rsi_value = _series_tail(rsi.get("values"), 1)
    if rsi_value is None:
        _say(ctx, "the index ticker indicator is unavailable; no action")
        return 0
    if rsi_value < bullish_level:
        _say(ctx, f"{underlying} rsi={rsi_value} is below {bullish_level}: no bullish setup")
        return 0
    _say(ctx, f"{underlying} rsi={rsi_value} >= {bullish_level}: bullish setup confirmed")

    # -- 2..5. the bounded observation loop ----------------------------------
    # One finite child carries the whole decision: it submits the entry, stays
    # alive while the platform sequences it, discovers its own option run from
    # ``owned_work``, submits ONE governed close, and only then finishes. Every
    # pass re-reads the book, so a repeat observation never duplicates work.
    started = time.monotonic()
    entry_submitted = False
    close_submitted = False
    while time.monotonic() - started < deadline_seconds:
        snapshot = ctx.run.owned_work()
        pending = _pending_adjustment(snapshot)

        # THIS strategy's own option runs, discovered from its bound attempts. An
        # unknown read is never treated as "no run": a wrong guess would open a
        # second structure.
        run_coverage = dict(snapshot.get("option_runs_coverage") or {})
        if str(run_coverage.get("coverage") or "unknown") != "known":
            _say(
                ctx,
                "no action: this strategy's option runs could not be read completely "
                f"(reason={run_coverage.get('reason') or 'unknown'}); refusing to guess",
            )
            return 0
        runs = [
            row
            for row in list(snapshot.get("option_runs") or [])
            if isinstance(row, dict) and str(row.get("underlying") or "").upper() == underlying
        ]

        if pending is not None:
            _say(
                ctx,
                f"outstanding work (plan={pending.get('plan_id')} state={pending.get('state')} "
                f"remaining={pending.get('remaining_quantity')}); waiting rather than repeating it",
            )
            time.sleep(2.0)
            continue

        if not entry_submitted and not runs:
            entry_legs = _frozen_entry_legs(ctx, underlying, expiry_policy, product)
            if entry_legs is None:
                return 0
            entry_submitted = True
            if not _submit_entry(
                ctx, underlying, entry_legs[0], entry_legs[1], product, expiry_policy, deadline_seconds
            ):
                # The entry declined to act (no binding, an unresolved request, a
                # refused plan) and already named why: finishing is honest, and no
                # close is attempted for a structure that was never confirmed.
                return 0
            if hold_after_entry:
                # The restart shape: this evaluation ENTERS and finishes with the
                # structure held. Nothing is closed here; a LATER supervised
                # evaluation reads the same durable run and closes it.
                _say(
                    ctx,
                    "entry executed and is held; this evaluation finishes without "
                    "closing (hold_after_entry=true) and the structure is carried "
                    "forward as held",
                )
                return 0
            continue

        if runs and not close_submitted:
            run = _open_run(runs)
            if run is None and all(_run_state(row) == "closed" for row in runs):
                _say(
                    ctx,
                    f"this strategy's {underlying} option run(s) are already closed "
                    f"({[str(r.get('status')) for r in runs]}); no re-entry and no adjustment",
                )
                return 0
            if run is None:
                _say(
                    ctx,
                    "no action: this strategy's option run status is not in a known "
                    f"vocabulary ({[str(r.get('status')) for r in runs]}); an unknown "
                    "status is not a closed structure",
                )
                return 2
            if duplicate_entry_probe and not probe_done:
                # Ask the platform whether a SECOND entry for the structure this
                # strategy already holds would be admitted. The answer is a
                # REFUSAL the platform owns (the admission guard), never something
                # this strategy decides for itself; the probe places no order.
                probe_done = True
                _say(ctx, _duplicate_entry_probe(ctx, underlying, run, product, expiry_policy, deadline_seconds))
                continue
            if _submit_close(ctx, underlying, run, product, deadline_seconds):
                close_submitted = True
            else:
                # A named refusal (stale Greeks, an unusable premium, a timeout) is
                # not a submitted close and must not be reported as one.
                return 0
            continue

        if close_submitted:
            # The close request came back terminal, which is NOT proof of a closed
            # structure. A closed run needs its own durable status, no outstanding
            # leg work, a published book and no remaining attributed quantity.
            verdict, reason = _close_evidence(snapshot, runs)
            if verdict == "closed":
                _say(ctx, f"structure closed with no outstanding work ({reason})")
                return 0
            if verdict == "open":
                _say(ctx, f"the structure is not closed yet ({reason}); waiting")
                time.sleep(2.0)
                continue
            _say(ctx, f"unresolved: the structure's closure cannot be proven ({reason})")
            return 2

        time.sleep(2.0)

    _say(ctx, "unresolved: the option observation loop never reached a settled structure")
    return 2


def _run_state(run: Dict[str, Any]) -> str:
    """``open`` / ``closed`` / ``unknown`` from the run's OWN durable status.

    The run's status is authoritative: ``entered`` holds the structure, and its
    ``completed_legs`` only record which ENTRY legs have filled. A status outside
    both vocabularies is UNKNOWN, which is neither closed nor safe to re-enter.
    """
    status = str(run.get("status") or "").strip().lower()
    if status in _CLOSED_RUN_STATUSES:
        return "closed"
    if status in _OPEN_RUN_STATUSES:
        return "open"
    return "unknown"


def _open_run(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A run that still holds (or may still hold) the structure.

    An UNKNOWN status is treated as still holding: it is the fail-closed choice,
    because treating an unreadable run as closed is how a second structure gets
    opened on top of a live one.
    """
    for row in runs:
        if _run_state(row) != "closed":
            return row
    return None


def _close_evidence(
    snapshot: Dict[str, Any], runs: List[Dict[str, Any]]
) -> Tuple[str, str]:
    """Whether the strategy's own evidence proves the structure is closed.

    ``closed`` needs ALL of: a published attributed book, a run whose own status
    is closed, no outstanding or failed leg work on that run, and no remaining
    non-zero quantity for its symbols. Anything the example cannot read is
    ``unknown`` — a terminal request is not a closed structure.
    """
    if str(snapshot.get("coverage")) != "known":
        return "unknown", f"the attributed book is not published (coverage={snapshot.get('coverage')})"
    if not runs:
        return "unknown", "no option run is readable for this strategy"
    states = {str(row.get("option_run_id")): _run_state(row) for row in runs}
    if any(state == "unknown" for state in states.values()):
        return "unknown", f"an option run status is not in a known vocabulary ({states})"
    if any(state != "closed" for state in states.values()):
        return "open", f"an option run is still open ({states})"
    for row in runs:
        outstanding = [
            str(leg.get("leg_id") or leg.get("tradingsymbol") or "?")
            for leg in list(row.get("pending_legs") or []) + list(row.get("failed_legs") or [])
            if isinstance(leg, dict)
        ]
        if outstanding:
            return "open", f"option run {row.get('option_run_id')} still has leg work {outstanding}"
    symbols = {
        str(leg.get("tradingsymbol") or "").strip().upper()
        for row in runs
        for leg in list(row.get("legs") or [])
        if isinstance(leg, dict)
    }
    symbols.discard("")
    residual = [
        row
        for row in list(snapshot.get("positions") or [])
        if isinstance(row, dict)
        and str(row.get("tradingsymbol") or "").strip().upper() in symbols
        and int(_number(row.get("net_quantity"), 0.0) or 0) != 0
    ]
    if residual:
        return "open", f"the strategy's own book still reports {len(residual)} open leg(s)"
    return "closed", f"runs {sorted(states)} are closed with no outstanding work"


def _frozen_entry_legs(
    ctx, underlying: str, expiry_policy: str, product: str  # noqa: ANN001
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Resolve BOTH legs client-side and freeze them, or name why we did not."""
    chain = ctx.client.options.get_chain(underlying)
    rows = list(chain.get("chain") or [])
    spot = _number(chain.get("spot_ltp"))
    expiry = str(chain.get("expiry") or "")
    if not rows or spot is None or not expiry:
        _say(ctx, "the option chain is missing rows, a spot price or an expiry; no action")
        return None
    greeks = ctx.client.options.get_greeks(underlying, expiry=expiry)
    greek_rows = {_number(row.get("strike")): row for row in list(greeks.get("contracts") or [])}
    if not greek_rows:
        _say(ctx, "Greeks are unavailable for this expiry (stale or missing); no action")
        return None
    params = dict(ctx.params or {})
    long_strike_row = _select_strike(
        rows, spot, _number(params.get("long_offset_points"), 0.0) or 0.0, "ce"
    )
    short_strike_row = _select_strike(
        rows, spot, _number(params.get("short_offset_points"), 100.0) or 100.0, "ce"
    )
    if long_strike_row is None or short_strike_row is None:
        _say(ctx, "no usable strikes for this expiry (missing premium or token); no action")
        return None
    long_strike, long_packet = long_strike_row
    short_strike, short_packet = short_strike_row
    if long_strike >= short_strike:
        _say(ctx, "the configured offsets do not form a call spread; no action")
        return None
    greek_check = greek_rows.get(_number(long_strike))
    if not greek_check or not greek_check.get("ce"):
        _say(ctx, f"Greeks are missing for strike {long_strike}; no action")
        return None
    long_leg = {
        "instrument_token": long_packet["token"],
        "exchange": "NFO",
        "tradingsymbol": long_packet["tsym"],
        "side": "BUY",
        "ratio": 1,
        "reference_price": long_packet["ltp"],
    }
    short_leg = {
        "instrument_token": short_packet["token"],
        "exchange": "NFO",
        "tradingsymbol": short_packet["tsym"],
        "side": "SELL",
        "ratio": 1,
        "reference_price": short_packet["ltp"],
    }
    _say(ctx,
        f"frozen bull call spread {long_leg['tradingsymbol']} / {short_leg['tradingsymbol']} "
        f"expiry={expiry} policy={expiry_policy} product={product} "
        f"premium={round(long_packet['ltp'] - short_packet['ltp'], 2)} "
        f"delta={long_packet['delta']}/{short_packet['delta']} iv={long_packet['iv']}/{short_packet['iv']}"
    )
    return long_leg, short_leg


def _entry_proposal(
    ctx,  # noqa: ANN001
    underlying: str,
    long_leg: Dict[str, Any],
    short_leg: Dict[str, Any],
    product: str,
    expiry_policy: str,
    *,
    suffix: str = "",
) -> Optional[Dict[str, Any]]:
    """The governed ``option_structure`` entry proposal for these frozen legs."""
    chain = ctx.client.options.get_chain(underlying)
    identity = ctx.run.attribution()
    strategy_id = str(identity.get("strategy_id") or "")
    account_scope = str(identity.get("account_id") or ctx.run.config.account_scope)
    if not identity.get("attributed") or not strategy_id:
        _say(ctx, "no action: this run has no persisted strategy binding")
        return None
    evaluation_id = f"opt-entry{suffix}-{ctx.run_id}-{long_leg['tradingsymbol']}"
    return {
        "evaluation_id": evaluation_id,
        "evaluation_kind": "run_now",
        "strategy_id": strategy_id,
        "strategy_run_id": ctx.run_id,
        "account_scope": account_scope,
        "target_kind": "option_structure",
        "payload": {
            "underlying": underlying,
            "expiry": str(chain.get("expiry") or ""),
            "product": product,
            "structure_id": f"bull-call-{long_leg['tradingsymbol']}-{short_leg['tradingsymbol']}",
            "expiry_policy": expiry_policy,
            "phase": "entry",
            "legs": [
                {
                    "instrument_token": long_leg["instrument_token"],
                    "exchange": long_leg["exchange"],
                    "tradingsymbol": long_leg["tradingsymbol"],
                    "side": "BUY",
                    "ratio": 1,
                    "reference_price": long_leg["reference_price"],
                },
                {
                    "instrument_token": short_leg["instrument_token"],
                    "exchange": short_leg["exchange"],
                    "tradingsymbol": short_leg["tradingsymbol"],
                    "side": "SELL",
                    "ratio": 1,
                    "reference_price": short_leg["reference_price"],
                },
            ],
        },
    }


def _submit_entry(
    ctx,  # noqa: ANN001
    underlying: str,
    long_leg: Dict[str, Any],
    short_leg: Dict[str, Any],
    product: str,
    expiry_policy: str,
    deadline_seconds: float,
) -> bool:
    """Submit the governed entry and wait for its authoritative outcome.

    ``True`` only when the request reached ``executed``; a missing binding, a
    refusal, an unresolved dispatch or the deadline all return ``False`` after
    naming the reason.
    """
    proposal = _entry_proposal(
        ctx, underlying, long_leg, short_leg, product, expiry_policy
    )
    if proposal is None:
        return False
    evaluation_id = str(proposal["evaluation_id"])
    submitted = ctx.run.submit_and_request_execution(
        proposal, idempotency_key=f"opt-entry-{evaluation_id}"
    )
    request = dict(submitted.get("execution_request") or {})
    _say(ctx, f"entry requested: {request.get('request_id')} status={request.get('status')}")
    if not _wait_for_request(ctx, str(request.get("request_id") or ""), deadline_seconds):
        # Manual waits for the owner decision; autonomous waits for the dispatcher.
        # Both end in an authoritative outcome, and neither is "done" before it.
        _say(
            ctx,
            f"no action: the entry request {request.get('request_id')} did not reach an "
            "executed outcome",
        )
        return False
    report = ctx.run.owned_work()
    pending = _pending_adjustment(report)
    partial = [
        row
        for row in list(report.get("pending") or [])
        if isinstance(row, dict) and str(row.get("state")) in {"partial", "finalizing"}
    ]
    if partial:
        _say(ctx, f"a leg is only partially filled ({len(partial)} outstanding step(s)); no action")
    elif pending is not None:
        _say(ctx, "entry work is still outstanding; waiting rather than adjusting")
    return True


def _submit_close(
    ctx,  # noqa: ANN001
    underlying: str,
    run: Dict[str, Any],
    product: str,
    deadline_seconds: float,
) -> bool:
    """The one supported adjustment: CLOSE the run's own frozen structure.

    An ``option_structure`` plan in the ``exit`` phase targets FLAT for this run's
    own legs, closes short liabilities first, and refuses a leg whose direction
    would open or extend exposure. The plan carries the run reference discovered
    from ``owned_work()["option_runs"]``, which was derived from this strategy's
    own bound attempts - never from a caller-supplied identity.
    """
    run_legs = [leg for leg in list(run.get("legs") or []) if isinstance(leg, dict)]
    if not run_legs:
        _say(ctx, "no action: the option run holds no frozen legs to close")
        return False

    max_age_seconds = float(dict(ctx.params or {}).get("quote_max_age_seconds") or 300.0)

    # Freshness is mandatory before closing: a stale chain or missing Greek is a
    # named refusal, not a reason to guess a price.
    chain = ctx.client.options.get_chain(underlying)
    fresh, why = _freshness(chain, max_age_seconds=max_age_seconds)
    if not fresh:
        _say(ctx, f"no action: the option chain is not fresh ({why}); not closing")
        return False
    expiry = str(run.get("expiry") or chain.get("expiry") or "")
    greeks = ctx.client.options.get_greeks(underlying, expiry=expiry)
    fresh, why = _freshness(greeks, max_age_seconds=max_age_seconds)
    if not fresh:
        _say(ctx, f"no action: Greeks are not fresh for this expiry ({why}); not closing")
        return False
    if not list(greeks.get("contracts") or []):
        _say(ctx, "no action: Greeks are missing for this expiry; not closing")
        return False
    # A frozen leg is identified by its tradingsymbol, which the chain carries as
    # ``tsym``; matching on the strike alone would miss a leg whose stored strike
    # is absent or formatted differently.
    by_symbol = {}
    by_strike = {}
    for row in list(chain.get("chain") or []):
        if not isinstance(row, dict):
            continue
        by_strike[str(row.get("strike"))] = row
        for packet in (row.get("ce"), row.get("pe"), row.get("CE"), row.get("PE")):
            if isinstance(packet, dict) and packet.get("tsym"):
                by_symbol[str(packet["tsym"]).upper()] = (row, packet)

    closing_legs: List[Dict[str, Any]] = []
    for leg in run_legs:
        side = str(leg.get("transaction_type") or "").upper()
        symbol = str(leg.get("tradingsymbol") or "").upper()
        matched = by_symbol.get(symbol)
        if matched is not None:
            row, packet = matched
        else:
            row = by_strike.get(str(_number(leg.get("strike")))) or {}
            packet = (
                row.get("CE" if str(leg.get("option_type") or "").upper() in {"CE", "CALL"} else "PE")
                or {}
            )
        reference = _number(packet.get("ltp"))
        if reference is None or reference <= 0:
            # The leg's stored price is history: closing against it would submit a
            # price the market has not confirmed, so a missing live premium is a
            # named refusal rather than a fallback.
            _say(ctx,
                f"no action: no usable live premium for {leg.get('tradingsymbol')}; not closing"
            )
            return False
        if str(leg.get("instrument_token") or "").strip() == "" and not packet.get("token"):
            _say(
                ctx,
                f"no action: no broker token for {leg.get('tradingsymbol')}; not closing",
            )
            return False
        # The executed unit comes from the run's own frozen leg. A missing lot is
        # a named refusal: defaulting to the quantity (or one share) would submit
        # a silently different size.
        lot = int(_number(leg.get("lot_size"), 0.0) or 0)
        if lot <= 0:
            _say(
                ctx,
                f"no action: the run's leg {leg.get('tradingsymbol')} carries no lot size; "
                "refusing to guess the traded unit",
            )
            return False
        quantity = int(_number(leg.get("quantity"), 0.0) or 0)
        if quantity <= 0:
            _say(ctx, f"no action: {leg.get('tradingsymbol')} has no held quantity; not closing")
            return False
        ratio = max(1, int(round(quantity / lot)))
        closing_legs.append(
            {
                # The frozen plan needs a broker token; the run leg carries it, and
                # the chain packet is the fallback when the leg's copy is absent.
                "instrument_token": leg.get("instrument_token") or packet.get("token"),
                "exchange": str(leg.get("exchange") or "NFO"),
                "tradingsymbol": str(leg.get("tradingsymbol") or ""),
                # The CLOSING direction is the opposite of the open side.
                "side": "SELL" if side == "BUY" else "BUY",
                "ratio": ratio,
                "reference_price": reference,
            }
        )

    identity = ctx.run.attribution()
    evaluation_id = f"opt-close-{ctx.run_id}-{str(run.get('option_run_id'))[:12]}"
    submitted = ctx.run.submit_and_request_execution(
        {
            "evaluation_id": evaluation_id,
            "evaluation_kind": "run_now",
            "strategy_id": str(identity.get("strategy_id") or ""),
            "strategy_run_id": ctx.run_id,
            "account_scope": str(identity.get("account_id") or ctx.run.config.account_scope),
            "target_kind": "option_structure",
            "payload": {
                "underlying": underlying,
                "expiry": expiry,
                "product": product,
                "structure_id": str(run.get("structure_id") or run.get("option_run_id") or ""),
                "expiry_policy": str(run.get("expiry_policy") or "exit_before_cutoff"),
                "phase": "exit",
                "option_run_id": str(run.get("option_run_id") or ""),
                "legs": closing_legs,
            },
        },
        idempotency_key=f"opt-close-{evaluation_id}",
    )
    request = dict(submitted.get("execution_request") or {})
    _say(ctx,
        f"structure close requested for run {run.get('option_run_id')}: "
        f"{request.get('request_id')} status={request.get('status')}"
    )
    # EVERY mode waits for an authoritative outcome. ``awaiting_approval`` (manual)
    # and ``queued``/``dispatching`` (autonomous, through the dispatcher) are all
    # in-flight: returning early on the manual status alone left an autonomous
    # close unobserved, and the caller then read that as "submitted and done".
    if not _wait_for_request(ctx, str(request.get("request_id") or ""), deadline_seconds):
        _say(
            ctx,
            f"no action: the close request {request.get('request_id')} did not reach an "
            "executed outcome before the deadline",
        )
        return False
    return True


def _request_row_until_terminal(  # noqa: ANN001
    ctx, request_id: str, deadline_seconds: float
) -> Dict[str, Any]:
    """The request's own TERMINAL row, or the last row seen at the deadline.

    ``queued`` and ``dispatching`` are in flight, not outcomes: the platform's
    own terminal vocabulary (``executed``/``refused``/``rejected``/
    ``dispatch_unresolved``) is the only thing this waits for.
    """
    if not request_id:
        return {}
    started = time.monotonic()
    row: Dict[str, Any] = {}
    while time.monotonic() - started < deadline_seconds:
        row = dict(ctx.run.execution_request(request_id) or {})
        status = str(row.get("status") or "")
        if status in _TERMINAL_REQUEST_STATES:
            _say(ctx, f"request {request_id} is now {status}")
            return row
        _say(
            ctx,
            f"waiting for request {request_id}: status={status or 'unknown'} "
            f"mode={row.get('authorization_mode') or 'unknown'}",
        )
        time.sleep(2.0)
    return row


def _wait_for_request(ctx, request_id: str, deadline_seconds: float) -> bool:  # noqa: ANN001
    """Wait for an AUTHORITATIVE request state; ``queued``/``dispatching`` are not it.

    ``True`` means the request reached ``executed``. A refusal, an unresolved
    dispatch or the deadline is ``False`` — the caller must not read any of those
    as a completed close.
    """
    row = _request_row_until_terminal(ctx, request_id, deadline_seconds)
    return str(row.get("status") or "") == "executed"


def _duplicate_entry_probe(  # noqa: ANN001
    ctx,
    underlying: str,
    run: Dict[str, Any],
    product: str,
    expiry_policy: str,
    deadline_seconds: float,
) -> str:
    """Ask the platform whether a second entry for the HELD structure is admitted.

    The probe answers one question the strategy must not answer for itself: if
    this evaluation re-issued its entry signal for a structure the strategy
    already holds, would the platform refuse it? It is built from the SAME frozen
    selection the entry used, and it is SKIPPED - never submitted - when the
    freshly frozen legs are not the structure the held run carries, because a
    probe that could open something new is not a probe.

    The platform's own refusal code is what this reports; no order is placed by
    the probe, and the caller goes on to submit the ONE close it owes.
    """
    held = sorted(
        (
            str(leg.get("tradingsymbol") or "").upper(),
            str(leg.get("transaction_type") or "").upper(),
        )
        for leg in list(run.get("legs") or [])
        if isinstance(leg, dict)
    )
    if not held:
        return "duplicate entry probe skipped: the held run carries no frozen legs"
    resolved = _frozen_entry_legs(ctx, underlying, expiry_policy, product)
    if resolved is None:
        return "duplicate entry probe skipped: the entry legs could not be frozen"
    long_leg, short_leg = resolved
    probe_keys = sorted(
        [
            (str(long_leg["tradingsymbol"]).upper(), "BUY"),
            (str(short_leg["tradingsymbol"]).upper(), "SELL"),
        ]
    )
    if probe_keys != held:
        return (
            "duplicate entry probe skipped: the frozen entry "
            f"{probe_keys} is not the structure this strategy holds {held}"
        )
    proposal = _entry_proposal(
        ctx, underlying, long_leg, short_leg, product, expiry_policy, suffix="-probe"
    )
    if proposal is None:
        return "duplicate entry probe skipped: this run has no persisted strategy binding"
    submitted = ctx.run.submit_and_request_execution(
        proposal, idempotency_key=f"opt-entry-probe-{proposal['evaluation_id']}"
    )
    request = dict(submitted.get("execution_request") or {})
    request_id = str(request.get("request_id") or "")
    row = _request_row_until_terminal(ctx, request_id, deadline_seconds)
    status = str(row.get("status") or "unknown")
    refusal = str(row.get("refusal_code") or "")
    return (
        f"duplicate entry probe answered {status} "
        f"refusal_code={refusal or 'none'} request={request_id}"
    )
