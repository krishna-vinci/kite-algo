"""Example 3 - owner index universe -> equal-weight full-snapshot portfolio.

The sequence, and the rules that keep it honest:

1. ONE owner-owned universe is resolved and its **same** persisted revision is
   pinned: the strategy reads the revision list for that universe and uses the
   newest revision's ``revision_id`` together with the members it returned, so a
   concurrent re-resolve cannot swap the scope under a target the strategy
   already sized;
2. complete membership and complete pricing are validated before any target
   exists. A rejected member, a coverage block that is not complete, or any
   member without a usable price is a named no-action - never an unintended
   liquidation of the rest;
3. the strategy's OWN attributed book is read by its real identity:
   ``(exchange, tradingsymbol, product)`` - the same tradingsymbol in two series
   is two positions, and a bare-symbol sum would merge them;
4. while ANY relevant work is outstanding the whole rebalance is DEFERRED rather
   than pretending the pending quantity was incorporated. A re-evaluation then
   re-reads the book and either produces no delta (no proposal at all) or a
   target that reflects what has actually settled;
5. an unpublished projection is "unknown", never a flat book.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple


def _note(ctx, text: str) -> None:  # noqa: ANN001
    """Mirror the decision to the child log, then report it as progress.

    The platform caps a progress note at 200 characters, so the mirrored line is
    truncated for the API while the log keeps the full text.
    """
    print(f"[strategy] {text}", file=sys.stderr, flush=True)
    try:
        ctx.progress(text[:200])
    except Exception as exc:  # noqa: BLE001 - the platform is the authority
        print(f"[strategy] progress refused: {exc}", file=sys.stderr, flush=True)
        raise


def _stop(ctx, reason: str, **fields: Any) -> int:  # noqa: ANN001
    payload = json.dumps(fields, default=str, sort_keys=True) if fields else ""
    _note(ctx, f"no action: {reason}{(' ' + payload) if payload else ''}")
    return 0


def _unresolved(ctx, reason: str, **fields: Any) -> int:  # noqa: ANN001
    payload = json.dumps(fields, default=str, sort_keys=True) if fields else ""
    _note(ctx, f"unresolved: {reason}{(' ' + payload) if payload else ''}")
    return 2


def _number(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _coverage_complete(coverage: Mapping[str, Any]) -> bool:
    """Whether the service's OWN coverage block shows complete membership.

    The service reports ``{candidates, rejected, resolved, source, ...}`` for a
    resolved revision: every candidate was either resolved or explicitly
    rejected, and at least one member resolved.
    """
    if not coverage:
        return False
    for key in ("complete", "is_complete", "membership_complete"):
        if key in coverage:
            return bool(coverage[key])
    resolved = coverage.get("resolved")
    candidates = coverage.get("candidates")
    rejected = coverage.get("rejected")
    if resolved is not None and candidates is not None:
        try:
            resolved_count = int(resolved)
            rejected_count = int(rejected or 0)
            candidate_count = int(candidates)
        except (TypeError, ValueError):
            return False
        return resolved_count > 0 and (resolved_count + rejected_count) == candidate_count
    return False


def _pinned_revision(ctx, name: str) -> Optional[Tuple[str, List[str], Dict[str, Any]]]:  # noqa: ANN001
    """Resolve the universe and pin the SAME persisted revision it reports."""
    resolved = ctx.client.resolve_universe(name)
    resolved_members = [str(item).strip().upper() for item in list(resolved.get("members") or [])]
    resolved_coverage = dict(resolved.get("coverage") or {})
    revisions = ctx.client.universe_revisions(name, limit=1)
    items = list(revisions.get("revisions") or [])
    if not items:
        return None
    newest = dict(items[0])
    revision_id = str(newest.get("revision_id") or "")
    if not revision_id:
        return None
    revision_members = [str(item).strip().upper() for item in list(newest.get("members") or [])]
    members = revision_members or resolved_members
    coverage = dict(newest.get("coverage") or resolved_coverage)
    # The revision is the scope; the resolved read above must agree with it.
    if resolved_members and revision_members and sorted(resolved_members) != sorted(revision_members):
        return None
    return revision_id, members, coverage


def _own_book(snapshot: Mapping[str, Any]) -> Dict[Tuple[str, str, str], int]:
    """Own positions keyed by the real identity: (exchange, symbol, product)."""
    book: Dict[Tuple[str, str, str], int] = {}
    for row in list(snapshot.get("positions") or []):
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("exchange") or "").upper(),
            str(row.get("tradingsymbol") or "").upper(),
            str(row.get("product") or "").upper(),
        )
        book[key] = book.get(key, 0) + int(_number(row.get("net_quantity"), 0.0) or 0)
    return book


def _pending(snapshot: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [row for row in list(snapshot.get("pending") or []) if isinstance(row, dict)]


_TERMINAL_REQUEST_STATES = {"executed", "refused", "rejected", "dispatch_unresolved"}


def _await_request(ctx, request_id: str, deadline_seconds: float) -> Dict[str, Any]:  # noqa: ANN001
    if not request_id:
        return {"status": "unknown"}
    started = time.monotonic()
    while time.monotonic() - started < deadline_seconds:
        ctx.progress(f"waiting on rebalance request {request_id}")
        row = ctx.run.execution_request(request_id)
        status = str(row.get("status") or "")
        if status in _TERMINAL_REQUEST_STATES:
            return row
        time.sleep(2.0)
    return {"status": "timeout", "request_id": request_id}


def main(ctx) -> int:  # noqa: ANN001 - the hosted contract is main(ctx)
    params: Dict[str, Any] = dict(ctx.params or {})

    universe_name = str(params.get("universe") or "").strip()
    budget_inr = _number(params.get("budget_inr"), 0.0) or 0.0
    cash_buffer_pct = _number(params.get("cash_buffer_pct"), 0.0) or 0.0
    product = str(params.get("product") or "CNC").strip().upper()
    exchange = str(params.get("exchange") or "NSE").strip().upper()
    deadline_seconds = float(params.get("deadline_seconds") or 120)
    tolerance = int(params.get("no_change_tolerance", 0) or 0)

    if not universe_name:
        return _stop(ctx, "no universe configured")
    if budget_inr <= 0:
        return _stop(ctx, "no positive budget configured")

    identity = ctx.run.attribution()
    if not identity.get("attributed") or not identity.get("strategy_id"):
        return _stop(ctx, "the run has no persisted strategy binding; refusing to propose")
    strategy_id = str(identity["strategy_id"])
    account_scope = str(identity.get("account_id") or ctx.run.config.account_scope)

    # -- 1/2. membership, completeness and pricing ---------------------------
    pinned = _pinned_revision(ctx, universe_name)
    if pinned is None:
        return _stop(
            ctx,
            "the universe did not resolve to one pinned revision with a matching member set",
            universe=universe_name,
        )
    revision_id, members, coverage = pinned
    if not members:
        return _stop(ctx, "the pinned revision is empty", universe=universe_name)
    if not _coverage_complete(coverage):
        return _stop(
            ctx, "membership coverage is not complete", universe=universe_name, coverage=coverage
        )
    if len({member.upper() for member in members}) != len(members):
        return _stop(ctx, "the pinned membership contains duplicates", universe=universe_name)

    # Revision members are exchange-qualified catalog keys. A bare symbol is
    # qualified with the configured exchange; an already-qualified member keeps
    # its own exchange rather than being re-prefixed.
    coordinates = [
        member if ":" in member else f"{exchange}:{member}".replace(" ", "")
        for member in members
    ]
    quotes = ctx.client.get_quotes(coordinates, mode="quote")
    rows = {
        str(row.get("symbol") or row.get("public_key") or row.get("tradingsymbol") or "")
        .upper()
        .replace(" ", ""): row
        for row in list(quotes.get("quotes") or quotes.get("data") or [])
        if isinstance(row, dict)
    }
    prices: Dict[str, float] = {}
    missing: List[str] = []
    for member, coordinate in zip(members, coordinates):
        row = (
            rows.get(coordinate.upper().replace(" ", ""))
            or rows.get(member.upper().replace(" ", ""))
            or {}
        )
        price = _number(row.get("last_price") or row.get("ltp"))
        if price is None or price <= 0:
            missing.append(member)
        else:
            prices[coordinate.upper().replace(" ", "")] = price
    if missing:
        return _stop(
            ctx,
            "some members have no usable price; a partial price set would liquidate the rest",
            missing=missing[:10],
            missing_count=len(missing),
            member_count=len(members),
        )

    # -- 3/4. the OWN book, outstanding work, and the deferral rule ----------
    snapshot = ctx.run.owned_work()
    book = _own_book(snapshot)
    pending = _pending(snapshot)
    coverage_state = str(snapshot.get("coverage") or "unknown")
    if coverage_state != "known":
        return _stop(
            ctx,
            "the attributed book is not published, so a target could liquidate a prior attempt",
            coverage=coverage_state,
        )
    if pending:
        # Deferred, not "incorporated": a pending quantity is not a settled one,
        # and pretending otherwise is how a rebalance duplicates itself.
        return _stop(
            ctx,
            "work is already outstanding for this strategy; deferring the whole rebalance",
            coverage=coverage_state,
            pending=len(pending),
            pending_remaining=[
                (row.get("state"), row.get("remaining_quantity")) for row in pending
            ],
        )

    # -- 5. equal weight, whole shares, lots, residual ----------------------
    investable = budget_inr * (1.0 - max(0.0, min(cash_buffer_pct, 0.99)))
    per_member = investable / len(members)
    lots = _lot_sizes(ctx, coordinates)
    target_quantities: Dict[str, int] = {}
    target_weights: Dict[str, float] = {}
    planned_notional = 0.0
    for member, coordinate in zip(members, coordinates):
        key = member.upper()
        normalized = coordinate.upper().replace(" ", "")
        price = prices.get(normalized) or prices.get(normalized.split(":")[-1]) or prices.get(key)
        if price is None:
            return _stop(ctx, "no usable price for a member", member=key, coordinate=coordinate)
        # The catalog is authoritative for the traded unit. An unknown lot is a
        # named refusal, not a silent assumption of one share.
        lot = lots.get(normalized) or lots.get(normalized.split(":")[-1]) or lots.get(key)
        if lot is None:
            return _stop(
                ctx,
                "the catalog did not provide a lot size for a member",
                member=key,
                coordinate=coordinate,
            )
        quantity = int(per_member // (price * lot)) * lot
        if quantity <= 0:
            return _stop(
                ctx,
                "the per-member budget is below one lot, so the snapshot would be incomplete",
                member=key,
                per_member=round(per_member, 2),
                lot=lot,
                price=price,
            )
        target_quantities[key] = quantity
        planned_notional += quantity * price
        target_weights[key] = round(quantity * price / investable, 8)
    residual = budget_inr - planned_notional

    deltas: Dict[str, int] = {}
    for member, coordinate, target in zip(members, coordinates, target_quantities.values()):
        book_exchange = coordinate.split(":")[0].upper()
        bare_symbol = coordinate.split(":")[-1].upper().replace(" ", "")
        current = book.get((book_exchange, bare_symbol, product), 0)
        delta = target - current
        if abs(delta) > tolerance:
            deltas[member.upper()] = delta
    _note(
        ctx,
        f"pinned revision {revision_id}: {len(members)} members, target notional "
        f"{round(planned_notional, 2)}, residual {round(residual, 2)}, "
        f"deltas {deltas or '{}'}",
    )
    if not deltas:
        return _stop(
            ctx,
            "the book already matches the equal-weight target; no proposal",
            revision_id=revision_id,
            residual=round(residual, 2),
        )

    evaluation_id = f"eqw-{ctx.run_id}-{revision_id}"
    proposal = {
        "evaluation_id": evaluation_id,
        "evaluation_kind": "run_now",
        "strategy_id": strategy_id,
        "strategy_run_id": ctx.run_id,
        "account_scope": account_scope,
        "target_kind": "target_weights",
        "payload": {
            "universe_revision_id": revision_id,
            "members": members,
            "target_weights": target_weights,
            "product": product,
            # The owner's admission allocation is the freezing and SIZING basis.
            # Stating the same number is allowed; stating a different one is
            # refused by name (CAPITAL_BASIS_MISMATCH) rather than silently
            # executed at the allocation, so the refusal is read back here.
            "capital_basis_inr": budget_inr,
            "cash_buffer_pct": cash_buffer_pct,
            "reference_prices": prices,
        },
    }
    # Proposal identity and request identity stay separate on purpose: a refused
    # plan has no execution request at all, and the strategy must be able to NAME
    # that refusal instead of crashing on a missing plan id.
    submitted = ctx.run.submit_proposal(proposal)
    plan = dict(submitted.get("plan") or {})
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id:
        refusal = dict(submitted.get("refusal") or {})
        return _stop(
            ctx,
            "the platform refused the weights plan; no order was sent",
            status=str(submitted.get("status") or ""),
            code=str(refusal.get("rejection_reason") or ""),
            allocation=refusal.get("authoritative_allocation_inr"),
            stated=refusal.get("stated_capital_basis_inr"),
        )
    request = ctx.run.request_execution(plan_id, idempotency_key=f"eqw-{evaluation_id}")
    request_id = str(request.get("request_id") or "")
    _note(ctx, f"rebalance requested {request_id} status={request.get('status')}")

    final = _await_request(ctx, request_id, deadline_seconds)
    status = str(final.get("status") or "")
    if status == "refused":
        return _stop(ctx, "the rebalance was refused", code=final.get("refusal_code"))
    if status == "dispatch_unresolved":
        return _unresolved(ctx, "the rebalance outcome is unknown", code=final.get("refusal_code"))
    if status != "executed":
        return _unresolved(ctx, "the rebalance never reached an authoritative outcome", status=status)

    # The projection keys a book row by its own (bare) tradingsymbol while the
    # membership may be the exchange-qualified public key, so the expectation is
    # compared on the bare symbol.
    expected_positions = {
        str(symbol).split(":")[-1].upper(): int(quantity)
        for symbol, quantity in target_quantities.items()
    }
    # The platform floors each weight leg to the pinned lot AFTER its own
    # arithmetic, so a member can land one lot away from the client's own floor.
    # That is the platform's sizing rule, not a disagreement, so the wait allows
    # one lot per member and the exact traded quantities are asserted by the
    # harness from the frozen plan.
    slack = {
        symbol: int(
            (lots.get(symbol) or lots.get(f"NSE:{symbol}") or 1)
        )
        for symbol in expected_positions
    }
    settled = _await_settled(
        ctx, deadline_seconds, expect=expected_positions, slack=slack
    )
    if _pending(settled):
        return _unresolved(
            ctx, "rebalance work is still outstanding", pending=len(_pending(settled))
        )
    if str(settled.get("coverage")) != "known":
        return _unresolved(ctx, "the book is not published after the rebalance", coverage=settled.get("coverage"))
    positions = _position_map(settled)
    off_target = {
        symbol: {"have": positions.get(symbol, 0), "want": int(quantity), "slack": slack.get(symbol, 1)}
        for symbol, quantity in expected_positions.items()
        if abs(positions.get(symbol, 0) - int(quantity)) > int(slack.get(symbol, 1))
    }
    if off_target:
        # A dispatch is not a fill: if the own book does not yet show the target,
        # the rebalance is not settled and must not be reported as if it were.
        return _unresolved(
            ctx,
            "the attributed book does not show the equal-weight target",
            off_target=off_target,
        )
    # The owner's admission allocation is the ENFORCED sizing basis, and the
    # platform overwrites a caller-supplied one on purpose. A declared budget that
    # is smaller than that allocation would be spent past, so the example reports
    # the settled notional rather than implying the declared budget was respected.
    settled_notional = 0.0
    for member, coordinate in zip(members, coordinates):
        symbol = str(member).split(":")[-1].upper()
        price = prices.get(coordinate.upper().replace(" ", "")) or prices.get(symbol)
        if price is None:
            continue
        settled_notional += abs(int(positions.get(symbol, 0))) * float(price)
    if settled_notional > budget_inr + 1.0:
        return _unresolved(
            ctx,
            "the settled notional exceeds the declared budget; the owner's admission "
            "allocation is the sizing basis and must match it",
            settled_notional=round(settled_notional, 2),
            budget_inr=budget_inr,
        )
    _note(
        ctx,
        f"settled notional {round(settled_notional, 2)} within budget {budget_inr}",
    )
    _note(ctx, "rebalance dispatched and settled with no outstanding work")
    return 0


def _lot_sizes(ctx, coordinates: List[str]) -> Dict[str, int]:  # noqa: ANN001
    """Lot size per member from the catalog. A missing entry is reported, not guessed."""
    resolved = ctx.client.resolve_tickers(coordinates)
    rows = list(resolved.get("instruments") or [])
    lots: Dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        lot = row.get("lot_size")
        if not lot:
            continue
        # The example keys lots by the member's own tradingsymbol; the resolved
        # row carries the exchange-qualified and bare forms, so both are stored.
        for value in (row.get("symbol"), row.get("public_key"), row.get("tradingsymbol")):
            symbol = str(value or "").upper().replace(" ", "")
            if symbol:
                lots[symbol] = max(1, int(lot))
                lots[symbol.split(":")[-1]] = max(1, int(lot))
    return lots


def _position_map(snapshot: Dict[str, Any]) -> Dict[str, int]:
    """The strategy's own book as ``{SYMBOL: net_quantity}``."""
    totals: Dict[str, int] = {}
    for row in list(snapshot.get("positions") or []):
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        if not symbol:
            continue
        totals[symbol] = totals.get(symbol, 0) + int(row.get("net_quantity") or 0)
    return totals


def _await_settled(
    ctx,  # noqa: ANN001
    deadline_seconds: float,
    expect: Optional[Dict[str, int]] = None,
    slack: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Wait until the book is published, has no outstanding work AND matches.

    The attributed book is rebuilt on demand, so "nothing outstanding" can be
    read before the rebuild lands. When the caller knows the target it submitted,
    the wait continues until the own book agrees with it, allowing the per-member
    lot slack the platform's own floor can produce.
    """
    wanted = {str(symbol).upper(): int(quantity) for symbol, quantity in (expect or {}).items()}
    tolerance = {str(symbol).upper(): int(value) for symbol, value in (slack or {}).items()}
    started = time.monotonic()
    last: Dict[str, Any] = {}
    while time.monotonic() - started < deadline_seconds:
        last = ctx.run.owned_work()
        pending = _pending(last)
        positions = _position_map(last)
        missing = {
            symbol: qty
            for symbol, qty in wanted.items()
            if abs(positions.get(symbol, 0) - qty) > tolerance.get(symbol, 0)
        }
        ctx.progress(
            f"settling: coverage={last.get('coverage')} pending={len(pending)} "
            f"awaited={missing or '{}'}"
        )
        if str(last.get("coverage")) == "known" and not pending and not missing:
            return last
        time.sleep(2.0)
    return last
