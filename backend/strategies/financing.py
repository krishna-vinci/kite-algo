"""One capacity rule for both the admission verdict and the reservation ledger.

Before this module the two gates disagreed: admission compared a strategy's
attributed book against that strategy's own allocation, while the reservation
ledger summed *account-wide* ``consumed`` + held rows (with no environment) and
compared that against a single strategy's allocation. Two coherent questions
were being answered with one incoherent number.

The contract keeps them separate and explicit:

* **strategy exposure budget** — ``strategy_admission_policies.allocation_inr`` is
  a limit on ONE strategy's own book, in ONE execution environment. Every term
  below is scoped to ``(account, strategy, environment)``, so strategy B's
  reservations can never consume strategy A's budget and a paper plan can never
  be charged for a live book.
* **account actual funds** — the broker's usable margin / the paper account's
  available funds. That is a real, separate constraint enforced against the
  *incremental* funding a plan needs, never against a strategy allocation.

Two rules make the numbers honest:

1. ``consumed`` reservations carry capacity ONLY until the exposure they backed
   is visible in the published attributed book (``strategy_projection_state``).
   After that the position itself carries the exposure, and charging the
   reservation too would double-count the same filled exposure. History stays in
   the ledger; it is simply not capacity any more.
2. A plan's allocation test is on its DESIRED POST-PLAN book (what the strategy
   will hold once the plan is done), while its *funding* test is incremental. A
   rebalance that sells A and buys B therefore fits the budget when the post-plan
   book fits — but the part of the funding that is not covered by free budget
   headroom must come from this plan's own confirmed releases, which is what
   :func:`staged_funding` describes for the executor.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func, select

from backend.strategies.attribution_models import (
    StrategyPositionProjection,
    StrategyProjectionState,
    StrategyReservation,
    StrategyReservationEvent,
)

__all__ = [
    "PENDING_COMMITMENT_STATUSES",
    "account_capacity_held_inr",
    "capacity_held",
    "current_book",
    "free_headroom_inr",
    "opens_or_grows_exposure",
    "plan_exposure",
    "staged_funding",
]

#: Statuses that hold capacity as an UNFILLED commitment.
PENDING_COMMITMENT_STATUSES = ("active", "renewed", "action_required")


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _leg_key(leg: Mapping[str, Any]) -> Tuple[str, str]:
    return (
        str(
            leg.get("instrument_id")
            or leg.get("canonical_instrument_id")
            or leg.get("tradingsymbol")
            or ""
        ),
        str(leg.get("product") or "").upper(),
    )


def opens_or_grows_exposure(target: int, current: int) -> bool:
    """The executor's own D-6 funding rule, reused rather than re-derived."""
    from backend.strategies.execution import PaperPlanExecutor

    return bool(PaperPlanExecutor._opens_or_grows_exposure(int(target or 0), int(current or 0)))


def _floor_to_lot(delta: int, lot: int) -> int:
    """The executor's own delta flooring."""
    if delta == 0 or lot <= 1:
        return delta
    sign = 1 if delta > 0 else -1
    return sign * ((abs(delta) // lot) * lot)


def current_book(
    db: Any, *, strategy_id: str, account_id: str, execution_environment: str
) -> Tuple[Dict[Tuple[str, str], int], List[str]]:
    """The strategy's own attributed book for ONE environment.

    Environment-scoped on purpose: a paper plan must never be sized against the
    live book, and vice versa. ``identity_kind='raw'`` rows are unresolved facts
    and are NAMED, never valued as if they were attributed.
    """
    rows = db.execute(
        select(
            StrategyPositionProjection.canonical_instrument_id,
            StrategyPositionProjection.product,
            StrategyPositionProjection.net_quantity,
            StrategyPositionProjection.identity_kind,
        ).where(
            StrategyPositionProjection.account_id == str(account_id),
            StrategyPositionProjection.strategy_id == str(strategy_id),
            StrategyPositionProjection.execution_environment == str(execution_environment),
        )
    ).all()
    book: Dict[Tuple[str, str], int] = {}
    unresolved: List[str] = []
    for canonical_id, product, quantity, identity_kind in rows:
        if str(identity_kind) != "canonical":
            unresolved.append(f"{product}:{quantity}")
            continue
        key = (str(canonical_id or ""), str(product or "").upper())
        book[key] = book.get(key, 0) + int(quantity or 0)
    return {key: value for key, value in book.items() if value != 0}, unresolved


def plan_exposure(
    db: Any, plan: Mapping[str, Any], *, execution_environment: str
) -> Dict[str, Any]:
    """Current / desired / incremental exposure for one plan, per instrument.

    The desired post-plan quantity for a coordinate is exactly what the executors
    produce: a leg's frozen target where the plan names the coordinate, and
    today's attributed quantity where it does not. Re-applying an unchanged
    target is therefore a zero-quantity, zero-additional-capital operation, and
    the gross / per-instrument / open-count checks include unchanged held names.

    Sizing mirrors the executor EXACTLY: a signed quantity is the instruction and
    the DELTA is floored to the pinned lot; a weight leg is sized with
    ``weight x basis x (1 - buffer) / price`` and floored to the pinned lot, and
    is long-only. Valuation is per coordinate from the plan's pinned reference
    prices; a required coordinate with no valid price is reported in ``unvalued``
    so the caller refuses rather than valuing it as zero. Sell legs contribute no
    incremental funding.

    This lives here (not in admission) because the RESERVATION LEDGER must be able
    to run the same arithmetic under its own lock: a detached admission verdict is
    not authoritative by the time capacity is claimed.
    """
    legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
    account_id = str(plan.get("account_id") or "")
    strategy_id = str(plan.get("strategy_id") or "")
    resolved = dict(plan.get("resolved_plan") or {})
    logical = dict(plan.get("logical_plan") or {})
    try:
        basis_raw = resolved.get("capital_basis_inr", logical.get("capital_basis_inr"))
        capital_basis = None if basis_raw is None else float(basis_raw)
    except (TypeError, ValueError):
        capital_basis = None
    try:
        buffer_raw = resolved.get("cash_buffer_pct", logical.get("cash_buffer_pct"))
        buffer_pct = 0.0 if buffer_raw is None else float(buffer_raw)
    except (TypeError, ValueError):
        buffer_pct = 0.0

    prices: Dict[Tuple[str, str], float] = {}
    targets: Dict[Tuple[str, str], int] = {}
    lots: Dict[Tuple[str, str], int] = {}
    long_only: set = set()
    symbols: Dict[Tuple[str, str], str] = {}
    unvalued: List[Dict[str, Any]] = []
    for leg in legs:
        key = _leg_key(leg)
        symbol = str(leg.get("tradingsymbol") or leg.get("broker_symbol") or "")
        symbols.setdefault(key, symbol)
        price = _as_float(leg.get("reference_price"))
        valid_price = price is not None and abs(price) > 0
        if valid_price:
            prices[key] = abs(float(price))
        try:
            lots[key] = int(leg.get("lot_size") or 1)
        except (TypeError, ValueError):
            lots[key] = 1
        lot = lots[key]
        explicit = leg.get("signed_quantity")
        weight = leg.get("target_weight")
        if explicit is not None:
            target_qty: Optional[int] = int(float(explicit))
        elif weight is not None:
            if capital_basis is None or not valid_price:
                target_qty = None
            else:
                units = int(
                    (
                        abs(float(weight or 0.0))
                        * capital_basis
                        * max(0.0, 1.0 - buffer_pct)
                    )
                    // abs(float(price))
                )
                target_qty = _floor_to_lot(units, lot) if lot > 1 else units
            long_only.add(key)
        else:
            target_qty = None
        if target_qty is None:
            unvalued.append(
                {"coordinate": list(key), "tradingsymbol": symbol, "reason": "no_sized_target"}
            )
        else:
            targets[key] = int(target_qty)

    current, unresolved_facts = current_book(
        db,
        strategy_id=strategy_id,
        account_id=account_id,
        execution_environment=execution_environment,
    )
    # An option ADJUST's frozen desired state IS the post-plan book: the engine
    # releases every leg the run holds that the target does not name (the release
    # half of a roll, and a removal). Leaving those coordinates at "held
    # unchanged" would keep a generation the plan is about to empty in the
    # post-plan book, where it has no reference price of its own - so a legitimate
    # roll would be refused POSITION_VALUATION_UNAVAILABLE for a book the plan
    # closes. Their target is flat, so the release is visible as a reduction and
    # the post-plan book is exactly what the run will hold.
    if str((resolved.get("option_run") or {}).get("phase") or "") == "adjust":
        for key in current:
            if key not in targets:
                targets[key] = 0
                lots.setdefault(key, 1)
    for raw_key in unresolved_facts:
        unvalued.append(
            {
                "coordinate": [str(raw_key)],
                "tradingsymbol": "",
                "reason": "unresolved_projection_fact",
            }
        )

    post: Dict[Tuple[str, str], int] = dict(current)
    for key, target in targets.items():
        post[key] = int(target)
    post = {key: value for key, value in post.items() if value != 0}

    for key in sorted(set(current) | set(targets)):
        # A coordinate whose POST-plan quantity is zero contributes zero to the
        # post-plan book: its missing price is not required evidence. This is
        # what lets a pure EXIT (reduce-to-flat) be admitted without a reference
        # price, while any surviving or growing holding still must be priceable.
        if int(post.get(key, 0)) == 0:
            continue
        if key not in prices:
            unvalued.append(
                {
                    "coordinate": list(key),
                    "tradingsymbol": symbols.get(key, ""),
                    "reason": "no_valid_price",
                }
            )

    def _value(key: Tuple[str, str], quantity: int) -> Optional[float]:
        price = prices.get(key)
        if price is None:
            return None
        return abs(float(quantity)) * float(price)

    current_values = [_value(key, value) for key, value in current.items()]
    current_exposure = (
        None if any(value is None for value in current_values) else float(sum(current_values))
    )
    post_values = [_value(key, value) for key, value in post.items()]
    desired_exposure = (
        None if any(value is None for value in post_values) else float(sum(post_values))
    )

    incremental = 0.0
    per_instrument: List[Dict[str, Any]] = []
    # Iterate the FULL coordinate set (held names plus the plan's own targets),
    # not the zero-filtered post book: a coordinate CLOSED to zero is a real
    # reduction and must be visible as a negative order quantity (it is what
    # funds a staged rebalance), even though it is absent from the post book.
    for key in sorted(set(current) | set(targets)):
        before = int(current.get(key, 0))
        price = prices.get(key)
        target = int(targets.get(key, before))
        delta = target - before
        if key in long_only and delta < 0:
            delta = max(delta, -before)
        quantity = _floor_to_lot(delta, lots.get(key, 1))
        increases = key in targets and opens_or_grows_exposure(target, before)
        if increases and price is not None:
            incremental += abs(float(quantity)) * float(price)
        per_instrument.append(
            {
                "coordinate": list(key),
                "tradingsymbol": symbols.get(key, ""),
                "current_quantity": before,
                "target_quantity": int(target),
                "delta": int(delta),
                "order_quantity": int(quantity),
                "increases_exposure": bool(increases),
                "notional_inr": (
                    None if price is None else abs(float(int(target))) * float(price)
                ),
            }
        )

    return {
        "execution_environment": str(execution_environment),
        "current_exposure_inr": current_exposure,
        "desired_exposure_inr": desired_exposure,
        "incremental_funding_inr": float(incremental),
        "plan_requirement_inr": float(incremental),
        "post_instruments": len(post),
        "post_quantities": {f"{key[0]}:{key[1]}": value for key, value in post.items()},
        "per_instrument": per_instrument,
        "unvalued": unvalued,
        "unresolved_projection_facts": len(unresolved_facts),
    }


def _publication(
    db: Any, *, account_id: str, strategy_id: str, execution_environment: str
) -> Dict[str, Any]:
    """The book's publication evidence: when, and whether it was COMPLETE.

    An unreadable or absent marker is treated as "never published": the
    fail-closed direction, because it keeps a consumed reservation holding
    capacity instead of releasing it on missing evidence.
    """
    try:
        row = db.execute(
            select(
                StrategyProjectionState.last_rebuild_at,
                StrategyProjectionState.content_sha256,
            ).where(
                StrategyProjectionState.account_id == str(account_id),
                StrategyProjectionState.strategy_id == str(strategy_id),
                StrategyProjectionState.execution_environment == str(execution_environment),
            )
        ).first()
    except Exception:  # noqa: BLE001 - a missing/unreadable marker is no evidence
        return {"published_at": None, "complete": False}
    if row is None:
        return {"published_at": None, "complete": False}
    return {
        "published_at": _as_aware(row[0]),
        # A publication without a content digest is not a complete snapshot.
        "complete": bool(row[1]),
    }


def _consumption_times(db: Any, *, reservation_ids: List[str]) -> Dict[str, datetime]:
    """Immutable ``consumed`` event times, keyed by reservation id.

    The reservation row's ``created_at`` is NOT the consumption time: a rebuild
    that lands between creation and consumption would otherwise look like proof
    that the fill is published.
    """
    if not reservation_ids:
        return {}
    try:
        rows = db.execute(
            select(
                StrategyReservationEvent.reservation_id,
                func.max(StrategyReservationEvent.created_at),
            )
            .where(
                StrategyReservationEvent.reservation_id.in_(reservation_ids),
                StrategyReservationEvent.event == "consumed",
            )
            .group_by(StrategyReservationEvent.reservation_id)
        ).all()
    except Exception:  # noqa: BLE001 - unreadable events are no evidence
        return {}
    stamps: Dict[str, datetime] = {}
    for reservation_id, created_at in rows:
        aware = _as_aware(created_at)
        if aware is not None:
            stamps[str(reservation_id)] = aware
    return stamps


def capacity_held(
    db: Any,
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
) -> Dict[str, Any]:
    """Capacity held for ONE strategy in ONE environment, with its evidence.

    The evidence rule for a ``consumed`` reservation is deliberately strict,
    because a wrong "already published" answer releases budget that is still
    spent:

    * the reservation must carry an IMMUTABLE consumption timestamp - the
      server-written ``consumed`` event (the reservation row itself has no
      ``consumed_at``, and ``created_at`` is NOT that timestamp: a rebuild that
      lands between creation and consumption proves nothing about the fill);
    * the book must have been (re)published AT OR AFTER that consumption
      timestamp, so the fills the consumption followed are inside the snapshot;
    * the publication must be a COMPLETE one (``strategy_projection_state`` with
      a ``content_sha256``), not a bare/partial row.

    Anything else leaves the reservation holding capacity. The consumed history
    is always in the ledger for audit; it is simply still budget.
    """
    published = _publication(
        db,
        account_id=account_id,
        strategy_id=strategy_id,
        execution_environment=execution_environment,
    )
    rows = db.execute(
        select(
            StrategyReservation.reservation_id,
            StrategyReservation.status,
            StrategyReservation.reserved_notional_inr,
        ).where(
            StrategyReservation.account_id == str(account_id),
            StrategyReservation.strategy_id == str(strategy_id),
            StrategyReservation.execution_environment == str(execution_environment),
            StrategyReservation.status.in_(PENDING_COMMITMENT_STATUSES + ("consumed",)),
        )
    ).all()
    consumed_ids = [str(row[0]) for row in rows if str(row[1]) == "consumed"]
    consumed_at = _consumption_times(db, reservation_ids=consumed_ids)
    unfilled = 0.0
    consumed_published = 0.0
    consumed_unpublished = 0.0
    for reservation_id, status, notional in rows:
        value = float(notional or 0.0)
        if str(status) in PENDING_COMMITMENT_STATUSES:
            unfilled += value
            continue
        stamp = consumed_at.get(str(reservation_id))
        if (
            stamp is not None
            and published["complete"]
            and published["published_at"] is not None
            and published["published_at"] >= stamp
        ):
            consumed_published += value
        else:
            consumed_unpublished += value
    return {
        "unfilled_commitments_inr": float(unfilled),
        "consumed_published_inr": float(consumed_published),
        "consumed_unpublished_inr": float(consumed_unpublished),
        # What capacity a NEW plan must fit beside: unfilled commitments plus any
        # consumed reservation whose fill is not provably inside the published
        # book.
        "held_inr": float(unfilled + consumed_unpublished),
        # Everything this strategy's budget is ALREADY committed to, including
        # published fills. This is the number a concurrent claim must be measured
        # against: two plans must not both be admitted against the same headroom
        # (the budget is spent by a fill whether or not the book has caught up).
        "committed_inr": float(unfilled + consumed_unpublished + consumed_published),
        "consumed_evidence": {
            str(reservation_id): (
                "published" if consumed_at.get(str(reservation_id)) else "no_consumption_event"
            )
            for reservation_id in consumed_ids
        },
        "projection_published_at": published["published_at"],
        "publication_complete": bool(published["complete"]),
    }


def account_capacity_held_inr(
    db: Any, *, account_id: str, execution_environment: str
) -> float:
    """Unfilled commitments across EVERY strategy of the account/environment.

    This is the ACCOUNT-FUNDS number, and it is deliberately narrower than the
    per-strategy :func:`capacity_held`: two strategies must not both promise the
    same account money, and the money a ``consumed`` reservation already spent is
    absent from the broker's *currently available* figure the caller supplies.
    Counting consumed rows here would therefore subtract the same cash twice.
    (The strategy BUDGET, by contrast, is spent by a fill whether or not the book
    has caught up, which is why ``consumed`` IS in ``capacity_held``'s
    ``committed_inr``.)
    """
    total = db.execute(
        select(func.coalesce(func.sum(StrategyReservation.reserved_notional_inr), 0.0)).where(
            StrategyReservation.account_id == str(account_id),
            StrategyReservation.execution_environment == str(execution_environment),
            StrategyReservation.status.in_(PENDING_COMMITMENT_STATUSES),
        )
    ).scalar()
    return float(total or 0.0)


def free_headroom_inr(*, allocation_inr: Optional[float], committed_inr: float) -> Optional[float]:
    """Budget left after everything already committed for this strategy.

    ``None`` means the budget is not enforced (a NULL allocation), which is a
    different statement from a budget of zero.
    """
    if allocation_inr is None:
        return None
    return float(allocation_inr) - float(committed_inr)


def staged_funding(
    *,
    allocation_inr: Optional[float],
    current_exposure_inr: float,
    pending_commitments_inr: float,
    incremental_funding_inr: float,
) -> Dict[str, Any]:
    """How much of a plan's funding must come from its OWN confirmed releases.

    ``incremental_funding_inr`` is what the plan must actually place. The part
    covered by free budget headroom (allocation minus the strategy's current book
    and its unfilled commitments) is available immediately; any remainder is a
    ``shortfall`` that may only be funded by the plan's own confirmed sell
    outcomes. Nothing here credits a PROJECTED sale: the number is a
    requirement, and the executor enforces it against confirmed fills.
    """
    headroom = free_headroom_inr(
        allocation_inr=allocation_inr,
        committed_inr=float(current_exposure_inr) + float(pending_commitments_inr),
    )
    if headroom is None:
        return {
            "account_allocation_enforced": False,
            "free_headroom_inr": None,
            "funding_shortfall_inr": 0.0,
            "requires_staged_financing": False,
        }
    shortfall = max(0.0, float(incremental_funding_inr) - float(headroom))
    return {
        "account_allocation_enforced": True,
        "free_headroom_inr": float(headroom),
        "funding_shortfall_inr": float(shortfall),
        "requires_staged_financing": bool(shortfall > 0),
    }
