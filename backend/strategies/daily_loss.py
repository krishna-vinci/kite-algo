"""Today's realized loss for one strategy, and the account-wide day-loss cap.

Two controls read one number each, and both are handed to admission as
*evidence* (admission stays pure - it never reaches for the network or the
database itself):

* the strategy's ``daily_loss_budget_inr`` - the strategy's OWN realized P&L for
  today's IST session, computed from its attributed confirmed fills;
* the account-wide ``account_daily_loss_cap_inr`` (platform live settings) - the
  BROKER's own day P&L for the whole account, summed from the reconciled
  ``account_positions`` book (the same ``realized_pnl + unrealised`` figure the
  realtime positions service publishes as ``pnl``).

Method (strategy realized P&L): **average cost (moving average)**. Every
attributed fill is replayed in ``(fill_timestamp, trade_id)`` order, keeping a
signed position and an average price per ``(instrument_token, product)`` book.
A fill that ADDS in the direction of the open position re-weights the average; a
fill that REDUCES realizes ``(price - average) * closed_quantity`` signed by the
position side, and a fill that crosses flat re-opens the remainder at its own
price. Only the realized part of fills whose own timestamp is inside TODAY'S IST
calendar day is summed - the cost basis still comes from the full history, so a
position carried in from an earlier day realizes correctly when it closes.

Charges: subtracted *when the fill source records them*. Neither
``order_trade_fills`` nor ``paper_trades`` carries a per-fill charge, so the
reader takes the strategy's own ORDER-level estimate for orders that filled
today - ``live_order_intents.cost_contract_json`` for live,
``paper_orders.metadata_json`` for paper - deduplicated per order. A charge
lookup that cannot be read degrades to zero (charges are an enhancement, not the
control) and never turns readable P&L into unreadable evidence.

Fail-closed: an evidence source that cannot be read returns ``None`` so admission
refuses an exposure-increasing plan instead of silently treating the limit as
satisfied. Only the ACCOUNT-wide P&L is fail-closed this way when a cap is
configured; an absent or unreadable settings row means "no cap configured",
exactly as the live-lane gate treats an unreadable row as "no persisted policy".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import bindparam, select, text

from backend.platform.settings import read_live_settings
from backend.shared.serialization import _row_mapping
from backend.strategies.attribution import SqlAttributionStore
from backend.strategies.attribution_models import StrategyRunBinding

logger = logging.getLogger(__name__)

#: The exchange's trading day is the IST calendar day. India has never observed
#: DST, so a fixed +05:30 offset is exact and needs no tz database.
IST = timezone(timedelta(hours=5, minutes=30))

# The broker book is normally reconciled every few seconds. Four missed periods
# is fresh enough for a control-plane read, while an overnight or outage-era row
# can never masquerade as today's broker evidence.
POSITIONS_FRESHNESS = timedelta(seconds=120)
SESSION_OPEN = time(9, 15)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class FillFact:
    """One attributed confirmed fill, with the price the average-cost fold needs."""

    order_id: str
    instrument_token: int
    product: str
    signed_quantity: int
    price: float
    effective_at: datetime


@dataclass(frozen=True)
class RealizedPnl:
    """The session's realized P&L for one strategy, gross and net of charges."""

    gross_pnl_inr: float
    charges_inr: float
    net_pnl_inr: float
    fill_count: int

    @property
    def loss_inr(self) -> float:
        """The day's realized LOSS as a non-negative magnitude (0 when flat/profit)."""
        return max(0.0, -self.net_pnl_inr)


def session_date(moment: datetime, *, tz: timezone = IST) -> date:
    """The IST calendar day a moment belongs to (the session's own date)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def realized_pnl_for_session(
    fills: Iterable[FillFact],
    *,
    session_day: date,
    charges_by_order: Optional[Mapping[str, float]] = None,
    tz: timezone = IST,
) -> RealizedPnl:
    """Average-cost realized P&L for ``session_day`` over one strategy's fills.

    Every fill is replayed, but only fills dated inside ``session_day`` contribute
    realized P&L; charges are the sum for orders that had a fill inside the day.
    """
    charges = charges_by_order or {}
    ordered = sorted(
        fills, key=lambda fact: (fact.effective_at, fact.order_id, fact.instrument_token)
    )

    quantities: Dict[Tuple[int, str], int] = {}
    averages: Dict[Tuple[int, str], float] = {}
    gross = 0.0
    fill_count = 0
    charged_orders: Dict[str, float] = {}

    for fact in ordered:
        key = (int(fact.instrument_token), str(fact.product or ""))
        quantity = quantities.get(key, 0)
        average = averages.get(key, 0.0)
        delta = int(fact.signed_quantity)
        price = float(fact.price)
        in_session = session_date(fact.effective_at, tz=tz) == session_day

        if delta == 0:
            continue
        if in_session:
            fill_count += 1
            charged_orders[fact.order_id] = float(charges.get(fact.order_id) or 0.0)

        opening_or_adding = quantity == 0 or (quantity > 0) == (delta > 0)
        if opening_or_adding:
            new_quantity = quantity + delta
            total_cost = abs(quantity) * average + abs(delta) * price
            averages[key] = total_cost / abs(new_quantity)
            quantities[key] = new_quantity
            continue

        # Reducing, closing, or crossing flat: realize the closed slice at the
        # running average, then carry any remainder at the fill price.
        closed = min(abs(delta), abs(quantity))
        realized = (price - average) * closed * (1.0 if quantity > 0 else -1.0)
        if in_session:
            gross += realized
        new_quantity = quantity + delta
        quantities[key] = new_quantity
        if new_quantity == 0:
            averages[key] = 0.0
        elif (new_quantity > 0) != (quantity > 0):
            # Crossed flat: the remainder is a NEW position at the fill price.
            averages[key] = price

    total_charges = sum(charged_orders.values())
    return RealizedPnl(
        gross_pnl_inr=gross,
        charges_inr=total_charges,
        net_pnl_inr=gross - total_charges,
        fill_count=fill_count,
    )


def _charges_from_json(payload: Any, *, keys: Sequence[str]) -> float:
    """The first parseable charge figure in a stored order payload, else 0."""
    document = payload
    if isinstance(document, (str, bytes, bytearray)):
        try:
            document = json.loads(document)
        except (TypeError, ValueError):
            return 0.0
    if not isinstance(document, Mapping):
        return 0.0
    for key in keys:
        found = _as_float(document.get(key))
        if found is not None:
            return float(found)
    cost_contract = document.get("cost_contract")
    if isinstance(cost_contract, Mapping):
        found = _as_float(cost_contract.get("total_charges"))
        if found is not None:
            return float(found)
    return 0.0


def _live_charge_map(
    session: Any, *, account_id: str, order_ids: Sequence[str]
) -> Dict[str, float]:
    """Order-level live charge estimates for ``order_ids`` (deduplicated per order)."""
    if not order_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT broker_order_id, cost_contract_json "
            "FROM public.live_order_intents "
            "WHERE account_id = :account_id AND broker_order_id IS NOT NULL"
        ),
        {"account_id": account_id},
    ).fetchall()
    wanted = set(order_ids)
    charges: Dict[str, float] = {}
    for row in rows:
        values = _row_mapping(row)
        order_id = str(values.get("broker_order_id") or "").strip()
        if order_id not in wanted:
            continue
        charges[order_id] = max(
            charges.get(order_id, 0.0),
            _charges_from_json(values.get("cost_contract_json"), keys=("total_charges",)),
        )
    return charges


def _paper_charge_map(
    session: Any, *, account_scope: str, order_ids: Sequence[str]
) -> Dict[str, float]:
    """Order-level paper charge estimates for ``order_ids`` (deduplicated per order)."""
    if not order_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT order_id, metadata_json "
            "FROM public.paper_orders "
            "WHERE account_scope = :account_scope"
        ),
        {"account_scope": account_scope},
    ).fetchall()
    wanted = set(order_ids)
    charges: Dict[str, float] = {}
    for row in rows:
        values = _row_mapping(row)
        order_id = str(values.get("order_id") or "").strip()
        if order_id not in wanted:
            continue
        charges[order_id] = max(
            charges.get(order_id, 0.0),
            _charges_from_json(
                values.get("metadata_json"),
                keys=("estimated_charges", "total_charges"),
            ),
        )
    return charges


def _bound_run_ids(
    session: Any, *, account_id: str, strategy_id: str, execution_environment: str
) -> set:
    rows = session.execute(
        select(StrategyRunBinding.strategy_run_id).where(
            StrategyRunBinding.account_id == account_id,
            StrategyRunBinding.strategy_id == strategy_id,
            StrategyRunBinding.execution_environment == execution_environment,
        )
    ).scalars().all()
    return {str(row) for row in rows}


def _live_fills(
    session: Any,
    *,
    account_id: str,
    bound_run_ids: set,
    owned_orders: Mapping[str, str],
) -> List[FillFact]:
    own_ids = [order_id for order_id, run_id in owned_orders.items() if run_id in bound_run_ids]
    if not own_ids:
        return []
    if session.bind.dialect.name == "postgresql":
        predicate = "otf.order_id = ANY(:owned_order_ids)"
        params: Dict[str, Any] = {"account_id": account_id, "owned_order_ids": list(own_ids)}
        statement = text(
            f"""
            SELECT otf.order_id, otf.trade_id, otf.instrument_token, otf.product,
                   CASE WHEN UPPER(otf.transaction_type) = 'BUY'
                        THEN otf.quantity ELSE -otf.quantity END AS signed_quantity,
                   otf.price, otf.fill_timestamp
            FROM public.order_trade_fills otf
            WHERE otf.account_id = :account_id
              AND {predicate}
            ORDER BY otf.fill_timestamp ASC, otf.trade_id ASC
            """
        )
    else:
        predicate = "otf.order_id IN :owned_order_ids"
        params = {"account_id": account_id}
        statement = text(
            f"""
            SELECT otf.order_id, otf.trade_id, otf.instrument_token, otf.product,
                   CASE WHEN UPPER(otf.transaction_type) = 'BUY'
                        THEN otf.quantity ELSE -otf.quantity END AS signed_quantity,
                   otf.price, otf.fill_timestamp
            FROM public.order_trade_fills otf
            WHERE otf.account_id = :account_id
              AND {predicate}
            ORDER BY otf.fill_timestamp ASC, otf.trade_id ASC
            """
        ).bindparams(bindparam("owned_order_ids", value=list(own_ids), expanding=True))
    rows = session.execute(statement, params).fetchall()
    return _fill_facts(rows)


def _paper_fills(
    session: Any,
    *,
    account_scope: str,
    bound_run_ids: set,
) -> List[FillFact]:
    rows = session.execute(
        text(
            """
            SELECT pt.order_id, pt.trade_id, po.instrument_token, po.product,
                   CASE WHEN UPPER(pt.transaction_type) = 'BUY'
                        THEN pt.quantity ELSE -pt.quantity END AS signed_quantity,
                   pt.price, pt.trade_timestamp,
                   COALESCE(po.metadata_json -> 'attribution' ->> 'strategy_run_id',
                            po.metadata_json ->> 'strategy_run_id') AS strategy_run_id
            FROM public.paper_trades pt
            INNER JOIN public.paper_orders po
              ON po.account_scope = pt.account_scope AND po.order_id = pt.order_id
            WHERE pt.account_scope = :account_scope
            ORDER BY pt.trade_timestamp ASC, pt.trade_id ASC
            """
        ),
        {"account_scope": account_scope},
    ).fetchall()

    facts: List[FillFact] = []
    seen: set = set()
    for row in rows:
        values = _row_mapping(row)
        trade_id = str(values.get("trade_id") or "").strip()
        if not trade_id or trade_id in seen:
            continue
        seen.add(trade_id)
        if str(values.get("strategy_run_id") or "").strip() not in bound_run_ids:
            continue
        fact = _fill_fact(values)
        if fact is not None:
            facts.append(fact)
    return facts


def _fill_fact(values: Mapping[str, Any]) -> Optional[FillFact]:
    moment = _as_datetime(values.get("fill_timestamp") or values.get("trade_timestamp"))
    price = _as_float(values.get("price"))
    if moment is None or price is None:
        return None
    return FillFact(
        order_id=str(values.get("order_id") or "").strip(),
        instrument_token=int(values.get("instrument_token") or 0),
        product=str(values.get("product") or ""),
        signed_quantity=int(values.get("signed_quantity") or 0),
        price=float(price),
        effective_at=moment,
    )


def _fill_facts(rows: Sequence[Any]) -> List[FillFact]:
    facts: List[FillFact] = []
    for row in rows:
        fact = _fill_fact(_row_mapping(row))
        if fact is not None:
            facts.append(fact)
    return facts


def strategy_daily_realized_loss_inr(
    *,
    account_id: str,
    strategy_id: str,
    execution_environment: str,
    session_factory: Optional[Callable[[], Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Today's realized LOSS in INR for one strategy, or ``None`` when unreadable.

    ``None`` is the fail-closed answer: admission refuses an exposure-increasing
    plan rather than treating an unreadable source as a satisfied limit. A strategy
    with no attributed fills answers ``0.0`` (a real, readable "no loss").
    """
    moment = now or _utcnow()
    environment = str(execution_environment or "live").lower()
    store = SqlAttributionStore(session_factory=session_factory)
    session = None
    try:
        session = store.session_factory()
        bound_run_ids = _bound_run_ids(
            session,
            account_id=str(account_id),
            strategy_id=str(strategy_id),
            execution_environment=environment,
        )
        if environment == "live":
            owned_orders, _anomalies = store.resolve_owned_orders(
                account_id=str(account_id), db=session
            )
            facts = _live_fills(
                session,
                account_id=str(account_id),
                bound_run_ids=bound_run_ids,
                owned_orders=owned_orders,
            )
            charges = _live_charge_map(
                session,
                account_id=str(account_id),
                order_ids=[fact.order_id for fact in facts],
            )
        else:
            facts = _paper_fills(
                session,
                account_scope=str(account_id),
                bound_run_ids=bound_run_ids,
            )
            charges = _paper_charge_map(
                session,
                account_scope=str(account_id),
                order_ids=[fact.order_id for fact in facts],
            )
    except Exception:  # noqa: BLE001 - an unreadable source is unreadable evidence
        logger.warning(
            "strategy daily realized loss could not be read for %s/%s",
            strategy_id,
            environment,
            exc_info=True,
        )
        return None
    finally:
        if session is not None:
            session.close()

    result = realized_pnl_for_session(
        facts, session_day=session_date(moment), charges_by_order=charges
    )
    return result.loss_inr


def account_day_pnl_inr(
    *,
    account_id: str,
    session_factory: Optional[Callable[[], Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """The broker's own day P&L for the account, or ``None`` when unreadable.

    The figure is the sum over the reconciled ``account_positions`` book of
    ``realized_pnl + (last_price - average_price) * net_quantity`` - exactly the
    ``pnl`` the realtime positions service publishes per position. A book is
    readable only when reconciliation is current for the active IST session. A
    non-flat position whose price was never marked is not readable evidence (a
    zero last price would fabricate a loss).
    """
    from backend.platform.settings import platform_session_factory

    moment = now or _utcnow()
    session = None
    try:
        factory = platform_session_factory(session_factory)
        session = factory()
        rows = session.execute(
            text(
                """
                SELECT realized_pnl, last_price, average_price, net_quantity,
                       MAX(last_reconciled_at) AS last_reconciled_at
                FROM public.account_positions WHERE account_id = :account_id
                GROUP BY realized_pnl, last_price, average_price, net_quantity
                """
            ),
            {"account_id": str(account_id)},
        ).fetchall()
    except Exception:  # noqa: BLE001 - an unreadable book is unreadable evidence
        logger.warning("account day P&L could not be read", exc_info=True)
        return None
    finally:
        if session is not None:
            session.close()

    last_reconciled = next(
        (_as_datetime(_row_mapping(row).get("last_reconciled_at")) for row in rows),
        None,
    )
    session_day = session_date(moment)
    session_start = datetime.combine(session_day, SESSION_OPEN, IST)
    if (
        last_reconciled is None
        or session_date(last_reconciled) != session_day
        or last_reconciled < session_start
        or moment - last_reconciled > POSITIONS_FRESHNESS
    ):
        return None

    total_pnl = 0.0
    for row in rows:
        values = _row_mapping(row)
        quantity = int(values.get("net_quantity") or 0)
        realized = _as_float(values.get("realized_pnl"))
        if realized is None:
            return None
        total_pnl += float(realized)
        if quantity == 0:
            continue
        last_price = _as_float(values.get("last_price"))
        average_price = _as_float(values.get("average_price"))
        if last_price is None or last_price <= 0 or average_price is None:
            return None
        total_pnl += (float(last_price) - float(average_price)) * quantity
    return total_pnl


def account_daily_loss_cap_inr(
    *, session_factory: Optional[Callable[[], Any]] = None
) -> Optional[float]:
    """The configured account-wide day-loss cap, or ``None`` when there is none."""
    persisted = read_live_settings(session_factory)
    if persisted is None:
        return None
    return persisted.account_daily_loss_cap_inr


def admission_daily_loss_evidence(
    *,
    plan: Mapping[str, Any],
    environment: str,
    session_factory: Optional[Callable[[], Any]] = None,
    now: Optional[datetime] = None,
    cap_reader: Optional[Callable[..., Optional[float]]] = None,
    day_pnl_reader: Optional[Callable[..., Optional[float]]] = None,
) -> Dict[str, Optional[float]]:
    """The two daily-loss evidence axes for one plan, ready for ``evaluate``.

    The account-wide axes are read only for a LIVE plan and only when a cap is
    actually configured, so an unconfigured cap costs one settings read and no
    positions read at all.
    """
    account_id = str(plan.get("account_id") or "")
    strategy_id = str(plan.get("strategy_id") or "")
    live = str(environment or "live").lower() == "live"
    evidence: Dict[str, Optional[float]] = {
        "realized_loss_inr": strategy_daily_realized_loss_inr(
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=str(environment),
            session_factory=session_factory,
            now=now,
        ),
        "account_day_pnl_inr": None,
        "account_daily_loss_cap_inr": None,
    }
    if not live or not account_id:
        return evidence
    read_cap = cap_reader or account_daily_loss_cap_inr
    cap = read_cap(session_factory=session_factory)
    evidence["account_daily_loss_cap_inr"] = cap
    if cap is not None:
        read_pnl = day_pnl_reader or account_day_pnl_inr
        evidence["account_day_pnl_inr"] = read_pnl(
            account_id=account_id, session_factory=session_factory
        )
    return evidence


__all__ = [
    "FillFact",
    "IST",
    "RealizedPnl",
    "account_daily_loss_cap_inr",
    "account_day_pnl_inr",
    "admission_daily_loss_evidence",
    "realized_pnl_for_session",
    "session_date",
    "strategy_daily_realized_loss_inr",
]
