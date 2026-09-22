"""Production evidence readers for the hosted live execution path.

Every reader returns AUTHORITATIVE platform evidence (attribution projection,
market data, broker margin, ingested fills) or raises a named refusal. There is
no test-only callback in the production factory: the factory in
``backend.strategies.live_service`` wires these functions, and tests fake the
BROKER boundary (the intent handler) rather than the evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import text

from backend.algo_runtime.account_scope import parse_account_scope
from backend.app.database import SessionLocal


class LiveEvidenceUnavailable(RuntimeError):
    """Authoritative evidence could not be read: unknown, never zero/flat."""

    def __init__(self, reason_code: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(reason_code)
        self.reason_code = str(reason_code)
        self.detail = dict(detail or {})


def broker_user_id_from_account_scope(account_scope: str) -> str:
    parsed = parse_account_scope(account_scope)
    if parsed.mode != "live" or not parsed.broker_user_id:
        raise LiveEvidenceUnavailable(
            "LIVE_ACCOUNT_SCOPE_REQUIRED",
            {"account_scope": str(account_scope or "")},
        )
    return str(parsed.broker_user_id)


def _session_row(account_scope: str, session_factory: Optional[Callable[[], Any]] = None):
    broker_user_id = broker_user_id_from_account_scope(account_scope)
    factory = session_factory or SessionLocal
    with factory() as session:
        row = session.execute(
            text(
                """
                SELECT session_id, access_token
                FROM public.kite_sessions
                WHERE broker_user_id = :broker_user_id
                  AND access_token IS NOT NULL
                ORDER BY CASE WHEN session_id = 'system' THEN 0 ELSE 1 END, created_at DESC
                LIMIT 1
                """
            ),
            {"broker_user_id": broker_user_id},
        ).fetchone()
    if not row:
        raise LiveEvidenceUnavailable(
            "LIVE_BROKER_SESSION_UNAVAILABLE",
            {"account_scope": str(account_scope or "")},
        )
    return row


def live_session_id_for_account(
    account_scope: str, *, session_factory: Optional[Callable[[], Any]] = None
) -> str:
    """The authoritative broker session id for an owner's live account binding."""
    row = _session_row(account_scope, session_factory)
    return str(row[0])


def live_kite_for_account(account_scope: str, *, session_factory: Optional[Callable[[], Any]] = None):
    """The authoritative broker client for an owner's live account binding."""
    from backend.broker_api.session.kite_session import build_kite_client

    row = _session_row(account_scope, session_factory)
    return build_kite_client(str(row[1]), session_id=str(row[0]))


def live_margin_evidence(
    account_scope: str,
    plan: Mapping[str, Any],
    *,
    session_factory: Optional[Callable[[], Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Authoritative live margin for the plan's legs, or ``None`` when unknown.

    ``None`` is a real answer: admission refuses ``MARGIN_UNAVAILABLE`` rather
    than assuming headroom. The snapshot must be COMPLETE: a response that does
    not cover every frozen leg is incomplete evidence, not partial headroom.
    """
    try:
        from backend.broker_api.orders.models import OrderMarginInput
        from backend.broker_api.orders.service import OrdersService

        legs = list((plan.get("resolved_plan") or {}).get("legs") or [])
        if not legs:
            return None
        items = []
        for leg in legs:
            quantity = abs(float(leg.get("signed_quantity", leg.get("target_weight", 0.0)) or 0.0))
            if quantity <= 0:
                continue
            items.append(
                OrderMarginInput(
                    exchange=str(leg.get("broker_exchange") or leg.get("exchange") or "NSE"),
                    tradingsymbol=str(leg.get("broker_symbol") or leg.get("tradingsymbol") or ""),
                    transaction_type="BUY" if float(leg.get("signed_quantity") or 0) >= 0 else "SELL",
                    variety="regular",
                    product=str(leg.get("product") or "CNC"),
                    order_type="MARKET",
                    quantity=quantity,
                    price=float(leg.get("reference_price") or 0),
                )
            )
        if not items:
            return None
        kite = live_kite_for_account(account_scope, session_factory=session_factory)
        quotes = OrdersService().order_margins(kite, items, f"admission-{account_scope}", None)
        quotes = list(quotes or [])
        if len(quotes) != len(items):
            # Incomplete coverage is unknown: the plan's requirement must be
            # priced for EVERY leg before any of it can be admitted.
            return None
        if any(getattr(quote, "total", None) is None for quote in quotes):
            return None
        usable = sum(float(getattr(quote, "total", 0.0) or 0.0) for quote in quotes)
        return {
            "usable": usable,
            "as_of": datetime.now(timezone.utc),
            "legs": [str(getattr(quote, "tradingsymbol", "") or "") for quote in quotes],
        }
    except Exception:  # noqa: BLE001 - unavailable evidence is not headroom
        return None


def attributed_position_reader(
    session_factory: Optional[Callable[[], Any]] = None,
    *,
    environment: str = "live",
) -> Callable[..., int]:
    """Attributed CURRENT quantity for one leg, from the canonical projection.

    Unknown/unpublished attribution refuses: an empty projection is not a flat
    account. Callers are expected to publish the book (full recompute) first.
    """
    factory = session_factory or SessionLocal

    def _reader(*, plan: Mapping[str, Any], leg: Mapping[str, Any]) -> int:
        account_id = str(plan.get("account_id") or "")
        strategy_id = str(plan.get("strategy_id") or "")
        instrument_id = str(leg.get("instrument_id") or "")
        product = str(leg.get("product") or "")
        if not (account_id and strategy_id and instrument_id):
            raise LiveEvidenceUnavailable(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {"plan_id": str(plan.get("plan_id") or ""), "reason": "missing attribution coordinates"},
            )
        with factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT canonical_instrument_id, product, net_quantity, unresolved_reason
                    FROM public.strategy_position_projection
                    WHERE account_id = :account_id
                      AND strategy_id = :strategy_id
                      AND execution_environment = :environment
                    """
                ),
                {"account_id": account_id, "strategy_id": strategy_id, "environment": environment},
            ).fetchall()
            published = session.execute(
                text(
                    """
                    SELECT 1
                    FROM public.strategy_projection_state
                    WHERE account_id = :account_id
                      AND strategy_id = :strategy_id
                      AND execution_environment = :environment
                    """
                ),
                {"account_id": account_id, "strategy_id": strategy_id, "environment": environment},
            ).first()
        if not rows and not published:
            # Never published is UNKNOWN, not flat: publishing the book (full
            # recompute) is the caller's job before it may size a live step.
            raise LiveEvidenceUnavailable(
                "LIVE_POSITION_EVIDENCE_UNAVAILABLE",
                {
                    "plan_id": str(plan.get("plan_id") or ""),
                    "message": "no published live attribution exists for this book",
                },
            )
        total = 0
        matched = False
        for row in rows:
            canonical = str(row[0] or "")
            row_product = str(row[1] or "")
            if canonical and canonical == instrument_id and (not product or row_product == product):
                total += int(row[2] or 0)
                matched = True
        if not matched:
            # The book is published and simply does not hold this identity: a
            # genuine flat for this leg.
            return 0
        return int(total)

    return _reader


async def live_quote_for_leg(leg: Mapping[str, Any]) -> Dict[str, Any]:
    """A fresh market-runtime quote for the EXACT frozen leg, in adapter shape.

    The response is matched back to the frozen instrument (token first, symbol
    second); a payload that comes back for a different instrument is a refusal,
    never "the first quote in the list". Freshness is carried through as
    ``as_of``/``is_stale`` so the adapter's own staleness bound decides.
    """
    from backend.api.services.market_data import WorkerMarketDataService, WorkerQuoteRequest

    token = leg.get("instrument_token") or leg.get("token")
    symbol = str(leg.get("tradingsymbol") or leg.get("broker_symbol") or "")
    if not token and not symbol:
        raise LiveEvidenceUnavailable(
            "LIVE_QUOTE_MISSING",
            {
                "instrument_id": str(leg.get("instrument_id") or ""),
                "reason": "the frozen leg carries neither an instrument token nor a symbol",
            },
        )
    request = WorkerQuoteRequest(
        symbols=[symbol] if symbol and not token else [],
        instrument_tokens=[int(token)] if token else [],
    )
    payload = await WorkerMarketDataService().get_quotes(request)
    quotes = list(payload.get("quotes") or [])
    if not quotes:
        raise LiveEvidenceUnavailable(
            "LIVE_QUOTE_MISSING",
            {"instrument_id": str(leg.get("instrument_id") or ""), "missing": payload.get("missing")},
        )
    quote = None
    if token:
        for candidate in quotes:
            if str(candidate.get("instrument_token") or "") == str(int(token)):
                quote = candidate
                break
    if quote is None and symbol:
        for candidate in quotes:
            if str(candidate.get("tradingsymbol") or candidate.get("symbol") or "").upper() == symbol.upper():
                quote = candidate
                break
    if quote is None:
        raise LiveEvidenceUnavailable(
            "LIVE_QUOTE_INSTRUMENT_MISMATCH",
            {
                "instrument_id": str(leg.get("instrument_id") or ""),
                "instrument_token": int(token) if token else None,
                "tradingsymbol": symbol,
                "returned": [
                    {
                        "instrument_token": candidate.get("instrument_token"),
                        "tradingsymbol": candidate.get("tradingsymbol") or candidate.get("symbol"),
                    }
                    for candidate in quotes
                ],
            },
        )
    ltp = quote.get("last_price")
    if ltp in (None, ""):
        raise LiveEvidenceUnavailable(
            "LIVE_QUOTE_MISSING", {"instrument_id": str(leg.get("instrument_id") or "")}
        )
    return {
        "instrument_id": str(leg.get("instrument_id") or ""),
        "ltp": float(ltp),
        "as_of": quote.get("received_at") or datetime.now(timezone.utc),
        "source": "market_runtime",
        "is_stale": bool(quote.get("is_stale")),
    }


def ingested_fill_reader(session_factory: Optional[Callable[[], Any]] = None) -> Callable[..., List[Dict[str, Any]]]:
    """Confirmed fills for a plan step, from the canonical ingestion fact table.

    Only fills whose broker order id is bound to this plan step (via the durable
    submission claim) are returned: an unrelated order on the same account can
    never satisfy a step. Duplicate deliveries collapse on the broker trade id.
    """
    factory = session_factory or SessionLocal

    def _reader(
        *, broker_order_ids: Sequence[str], plan: Mapping[str, Any] | None = None
    ) -> List[Dict[str, Any]]:
        order_ids = [str(value) for value in (broker_order_ids or []) if str(value)]
        if not order_ids:
            return []
        with factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT trade_id, order_id, tradingsymbol, transaction_type, quantity,
                           price, fill_timestamp
                    FROM public.order_trade_fills
                    WHERE order_id = ANY(:order_ids)
                    ORDER BY fill_timestamp, trade_id
                    """
                ),
                {"order_ids": order_ids},
            ).fetchall()
        seen: set[str] = set()
        fills: List[Dict[str, Any]] = []
        for row in rows:
            trade_id = str(row[0] or "")
            if trade_id and trade_id in seen:
                continue
            if trade_id:
                seen.add(trade_id)
            fills.append(
                {
                    "trade_id": trade_id,
                    "broker_order_id": str(row[1] or ""),
                    "tradingsymbol": str(row[2] or ""),
                    "transaction_type": str(row[3] or ""),
                    "quantity": int(row[4] or 0),
                    "price": float(row[5] or 0.0) if row[5] is not None else None,
                    "filled_at": row[6],
                }
            )
        return fills

    return _reader


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
