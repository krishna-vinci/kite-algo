from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from broker_api.instruments_repository import InstrumentsRepository
from broker_api.redis_events import publish_event

from .charges import PaperChargesCalculator
from .events import paper_order_event_payload, paper_position_event_payload, paper_trade_event_payload
from .margin_engine import PaperMarginEngine
from .models import (
    FundLedgerEntryType,
    PaperAccount,
    PaperFundLedgerEntry,
    PaperOrder,
    PaperOrderSide,
    PaperOrderStatus,
    PaperOrderType,
    PaperPosition,
    PaperTrade,
)
from .repository import SqlAlchemyPaperRepository


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _AtomicBasketRejected(Exception):
    def __init__(self, payload: Dict[str, Any]) -> None:
        super().__init__(payload.get("reason") or "Atomic basket rejected")
        self.payload = payload


class PaperTradingService:
    def __init__(
        self,
        *,
        repository: SqlAlchemyPaperRepository | None = None,
        instruments_repository: InstrumentsRepository | None = None,
        market_data_runtime: Any | None = None,
        margin_engine: PaperMarginEngine | None = None,
        charges_calculator: PaperChargesCalculator | None = None,
        journal_service: Any | None = None,
        default_starting_balance: Decimal | str | float = Decimal("1000000"),
    ) -> None:
        self.repository = repository or SqlAlchemyPaperRepository()
        self.instruments_repository = instruments_repository or InstrumentsRepository()
        self.market_data_runtime = market_data_runtime
        self.margin_engine = margin_engine or PaperMarginEngine()
        self.charges_calculator = charges_calculator or PaperChargesCalculator()
        self.journal_service = journal_service
        self.default_starting_balance = Decimal(str(default_starting_balance))
        self._account_locks: Dict[str, asyncio.Lock] = {}

    async def ensure_account(self, account_scope: str, *, starting_balance: Decimal | None = None) -> PaperAccount:
        account = await asyncio.to_thread(self.repository.get_account, account_scope)
        if account is not None:
            return account
        now = _utcnow()
        return await asyncio.to_thread(
            self.repository.upsert_account,
            PaperAccount(
                account_scope=account_scope,
                starting_balance=starting_balance if starting_balance is not None else self.default_starting_balance,
                available_funds=starting_balance if starting_balance is not None else self.default_starting_balance,
                blocked_funds=Decimal("0"),
                realized_pnl=Decimal("0"),
                metadata={},
                created_at=now,
                updated_at=now,
            ),
        )

    async def execute_intent(self, *, intent: Any, instance_context: Dict[str, Any]) -> Dict[str, Any]:
        dependency_spec = instance_context.get("dependency_spec") or {}
        account_scope = str(dependency_spec.get("account_scope") or "").strip()
        if not account_scope:
            raise ValueError("paper execution requires dependency_spec.account_scope")
        if intent.intent_type == "place_order":
            return await self.place_order(account_scope=account_scope, order_payload=intent.payload.get("order") or {}, attribution=self._attribution(intent, instance_context))
        if intent.intent_type == "place_basket":
            basket_payload = intent.payload.get("basket") or intent.payload
            return await self.place_basket(account_scope=account_scope, basket_payload=basket_payload, attribution=self._attribution(intent, instance_context))
        raise ValueError(f"Unsupported paper intent type '{intent.intent_type}'")

    async def place_order(self, *, account_scope: str, order_payload: Dict[str, Any], attribution: Dict[str, Any]) -> Dict[str, Any]:
        async with self._account_lock(account_scope):
            return await self._place_order_locked(account_scope=account_scope, order_payload=order_payload, attribution=attribution)

    async def _place_order_locked(self, *, account_scope: str, order_payload: Dict[str, Any], attribution: Dict[str, Any]) -> Dict[str, Any]:
        attribution = self._merged_attribution(attribution, (order_payload or {}).get("metadata"))
        request = self._normalize_order_request(order_payload)
        account = await self.ensure_account(account_scope)
        instrument = await asyncio.to_thread(
            self.instruments_repository.get_instrument_by_exchange_symbol,
            request["exchange"],
            request["tradingsymbol"],
        )
        if not instrument:
            return await self._reject_order(account_scope, request, attribution, reason=f"Instrument not found for {request['exchange']}:{request['tradingsymbol']}")

        existing_position = await asyncio.to_thread(
            self.repository.get_position,
            account_scope,
            int(instrument["instrument_token"]),
            request["product"],
        )
        market_snapshot = await self._market_snapshot(int(instrument["instrument_token"]))
        reference_price = self._reference_price(request=request, instrument=instrument, market_snapshot=market_snapshot)
        if reference_price <= 0:
            return await self._reject_order(account_scope, request, attribution, reason="No reference price available for paper execution")

        lot_size = int(instrument.get("lot_size") or 1)
        if lot_size > 1 and int(request["quantity"]) % lot_size != 0:
            return await self._reject_order(account_scope, request, attribution, reason=f"Quantity must be a multiple of lot size {lot_size}")

        old_margin = self._position_margin(existing_position, instrument_type=instrument.get("instrument_type"), reference_price=reference_price)
        hypothetical = self._apply_fill_to_position(account_scope=account_scope, position=existing_position, request=request, fill_price=reference_price, instrument=instrument)
        new_margin = self._position_margin(hypothetical, instrument_type=instrument.get("instrument_type"), reference_price=reference_price)
        incremental_margin = max(new_margin - old_margin, Decimal("0"))
        estimated_charges = self.charges_calculator.estimate(
            price=reference_price,
            quantity=request["quantity"],
            instrument_type=instrument.get("instrument_type"),
            exchange=request["exchange"],
            product=request["product"],
        )
        required_cash = incremental_margin + estimated_charges
        if account.available_funds < required_cash:
            return await self._reject_order(account_scope, request, attribution, reason="Insufficient paper funds or margin")

        order = PaperOrder(
            account_scope=account_scope,
            order_id=f"PAPER-{uuid.uuid4().hex[:12].upper()}",
            instrument_token=int(instrument["instrument_token"]),
            exchange=request["exchange"],
            tradingsymbol=request["tradingsymbol"],
            product=request["product"],
            transaction_type=str(request["transaction_type"]).lower(),
            order_type=str(request["order_type"]).replace("-", "_").lower(),
            quantity=request["quantity"],
            price=Decimal(str(request["price"])) if request.get("price") is not None else None,
            trigger_price=Decimal(str(request["trigger_price"])) if request.get("trigger_price") is not None else None,
            status=PaperOrderStatus.PENDING,
            metadata={
                **attribution,
                "reserved_margin": str(incremental_margin),
                "estimated_charges": str(estimated_charges),
                "instrument_type": str(instrument.get("instrument_type") or ""),
                "lot_size": lot_size,
            },
        )
        order = await asyncio.to_thread(self.repository.insert_order, order)
        await self._record_journal_order(order)
        account = await self._apply_account_delta(account, reserve_delta=incremental_margin, reserve_entry=order.order_id)
        await publish_event("paper.orders.events", paper_order_event_payload(event_type="accepted", order=order))
        order = await self._persist_stop_limit_trigger_state(order=order, request=request, market_snapshot=market_snapshot)

        if self._should_fill_immediately(order, request=request, market_snapshot=market_snapshot):
            fill_price = self._fill_price(order, market_snapshot=market_snapshot, fallback=reference_price)
            return await self._fill_order(order=order, account=account, request=request, instrument=instrument, fill_price=fill_price, existing_position=existing_position)

        pending_order = order.model_copy(update={"status": PaperOrderStatus.OPEN, "updated_at": _utcnow()})
        pending_order = await asyncio.to_thread(self.repository.update_order, pending_order)
        await publish_event("paper.orders.events", paper_order_event_payload(event_type="open", order=pending_order))
        return {"mode": "paper", "status": "accepted", "order": pending_order.model_dump(mode="json")}

    async def place_basket(self, *, account_scope: str, basket_payload: Dict[str, Any], attribution: Dict[str, Any]) -> Dict[str, Any]:
        async with self._account_lock(account_scope):
            return await self._place_basket_locked(account_scope=account_scope, basket_payload=basket_payload, attribution=attribution)

    async def _place_basket_locked(self, *, account_scope: str, basket_payload: Dict[str, Any], attribution: Dict[str, Any]) -> Dict[str, Any]:
        orders = list((basket_payload or {}).get("orders") or [])
        all_or_none = bool((basket_payload or {}).get("all_or_none"))
        attribution = self._merged_attribution(attribution, (basket_payload or {}).get("metadata"))
        if all_or_none:
            return await self._place_basket_all_or_none_atomic(account_scope=account_scope, orders=orders, attribution=attribution)
        results: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        for raw_order in orders:
            result = await self._place_order_locked(
                account_scope=account_scope,
                order_payload=raw_order,
                attribution=self._merged_attribution(attribution, (raw_order or {}).get("metadata")),
            )
            results.append(result)
            if result.get("status") == "rejected":
                failures.append(result)
                if all_or_none:
                    break
        overall = "success" if not failures else ("failed" if len(failures) == len(results) else "partial")
        return {"mode": "paper", "status": overall, "results": results, "errors": failures}

    async def _place_basket_all_or_none_atomic(self, *, account_scope: str, orders: List[Dict[str, Any]], attribution: Dict[str, Any]) -> Dict[str, Any]:
        prepared: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []
        for raw_order in orders:
            request = self._normalize_order_request(raw_order)
            order_attribution = self._merged_attribution(attribution, (raw_order or {}).get("metadata"))
            instrument = await asyncio.to_thread(
                self.instruments_repository.get_instrument_by_exchange_symbol,
                request["exchange"],
                request["tradingsymbol"],
            )
            if not instrument:
                rejected = self._build_rejected_result(
                    account_scope,
                    request,
                    attribution,
                    reason=f"Instrument not found for {request['exchange']}:{request['tradingsymbol']}",
                )
                return {"mode": "paper", "status": "failed", "results": [], "errors": [rejected]}
            market_snapshot = await self._market_snapshot(int(instrument["instrument_token"]))
            prepared.append((request, instrument, market_snapshot, order_attribution))

        staged_events: List[Tuple[str, Dict[str, Any]]] = []
        staged_results: List[Dict[str, Any]] = []
        try:
            with self.repository.unit_of_work() as uow:
                account = uow.get_account(account_scope)
                if account is None:
                    now = _utcnow()
                    account = uow.upsert_account(
                        PaperAccount(
                            account_scope=account_scope,
                            starting_balance=self.default_starting_balance,
                            available_funds=self.default_starting_balance,
                            blocked_funds=Decimal("0"),
                            realized_pnl=Decimal("0"),
                            metadata={},
                            created_at=now,
                            updated_at=now,
                        )
                    )

                for request, instrument, market_snapshot, order_attribution in prepared:
                    result, account = self._place_order_locked_uow(
                        uow=uow,
                        account_scope=account_scope,
                        account=account,
                        request=request,
                        instrument=instrument,
                        market_snapshot=market_snapshot,
                        attribution=order_attribution,
                        staged_events=staged_events,
                    )
                    staged_results.append(result)

                    if result.get("status") == "rejected":
                        raise _AtomicBasketRejected(result)
        except _AtomicBasketRejected as exc:
            return {"mode": "paper", "status": "failed", "results": [], "errors": [exc.payload]}

        for topic, payload in staged_events:
            await publish_event(topic, payload)
        for result in staged_results:
            order_payload = result.get("order") or {}
            trade_payload = result.get("trade") or {}
            if order_payload:
                await self._record_journal_order(PaperOrder.model_validate(order_payload))
            if trade_payload:
                await self._record_journal_trade(PaperTrade.model_validate(trade_payload))
        return {"mode": "paper", "status": "success", "results": staged_results, "errors": []}

    async def process_tick(self, tick: Dict[str, Any]) -> None:
        instrument_token = int(tick.get("instrument_token") or 0)
        if instrument_token <= 0:
            return
        pending_orders = await asyncio.to_thread(self.repository.list_pending_orders_for_instrument, instrument_token)
        for order in pending_orders:
            request = self._request_from_paper_order(order)
            order = await self._persist_stop_limit_trigger_state(order=order, request=request, market_snapshot=tick)
            if not self._order_should_trigger(order, request=request, market_snapshot=tick):
                continue
            async with self._account_lock(order.account_scope):
                account = await self.ensure_account(order.account_scope)
                existing_position = await asyncio.to_thread(self.repository.get_position, order.account_scope, order.instrument_token, order.product)
                instrument = await asyncio.to_thread(self.instruments_repository.get_instrument_by_exchange_symbol, order.exchange, order.tradingsymbol or "")
                if not instrument:
                    continue
                fill_price = self._fill_price(order, market_snapshot=tick, fallback=Decimal(str(tick.get("last_price") or 0)))
                await self._fill_order(order=order, account=account, request=request, instrument=instrument, fill_price=fill_price, existing_position=existing_position)

        positions = await asyncio.to_thread(self.repository.list_open_positions_for_instrument, instrument_token)
        for position in positions:
            async with self._account_lock(position.account_scope):
                await self._mark_to_market(position, tick)

    async def list_orders(self, account_scope: str, *, strategy_tag: str | None = None, algo_instance_id: str | None = None, limit: int = 200) -> List[PaperOrder]:
        orders = await asyncio.to_thread(self.repository.list_orders, account_scope, limit=limit)
        return [order for order in orders if self._matches_attribution(order.metadata, strategy_tag=strategy_tag, algo_instance_id=algo_instance_id)]

    async def list_trades(self, account_scope: str, *, strategy_tag: str | None = None, algo_instance_id: str | None = None, limit: int = 500) -> List[PaperTrade]:
        trades = await asyncio.to_thread(self.repository.list_trades, account_scope, limit=limit)
        return [trade for trade in trades if self._matches_attribution(trade.metadata, strategy_tag=strategy_tag, algo_instance_id=algo_instance_id)]

    async def list_positions(self, account_scope: str, *, only_open: bool = False) -> List[PaperPosition]:
        return await asyncio.to_thread(self.repository.list_positions, account_scope, only_open=only_open)

    async def get_account_summary(self, account_scope: str) -> Dict[str, Any]:
        account = await self.ensure_account(account_scope)
        positions = await self.list_positions(account_scope, only_open=True)
        unrealized = sum((Decimal(position.unrealized_pnl) for position in positions), Decimal("0"))
        return {
            "account_scope": account.account_scope,
            "currency": account.currency,
            "starting_balance": float(account.starting_balance),
            "available_funds": float(account.available_funds),
            "blocked_funds": float(account.blocked_funds),
            "realized_pnl": float(account.realized_pnl),
            "unrealized_pnl": float(unrealized),
            "open_position_count": len(positions),
        }

    async def reset_account(self, account_scope: str, *, starting_balance: Decimal | None = None) -> Dict[str, Any]:
        balance = Decimal(str(starting_balance if starting_balance is not None else self.default_starting_balance))
        now = _utcnow()
        await asyncio.to_thread(self.repository.clear_account_scope, account_scope)
        account = await asyncio.to_thread(
            self.repository.upsert_account,
            PaperAccount(
                account_scope=account_scope,
                starting_balance=balance,
                available_funds=balance,
                blocked_funds=Decimal("0"),
                realized_pnl=Decimal("0"),
                metadata={"reset_at": now.isoformat()},
                created_at=now,
                updated_at=now,
            ),
        )
        return {"account": account.model_dump(mode="json")}

    async def active_market_tokens(self) -> List[int]:
        return await asyncio.to_thread(self.repository.list_active_market_tokens)

    def _attribution(self, intent: Any, instance_context: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(instance_context.get("metadata") or {})
        dependency_spec = dict(instance_context.get("dependency_spec") or {})
        return {
            "algo_instance_id": instance_context.get("instance_id"),
            "algo_type": instance_context.get("algo_type"),
            "strategy_tag": instance_context.get("algo_type"),
            "intent_type": intent.intent_type,
            "dedupe_key": getattr(intent, "dedupe_key", None),
            "journal_run_id": metadata.get("journal_run_id") or dependency_spec.get("journal_run_id"),
            "journal_ref": metadata.get("journal_ref") or dependency_spec.get("journal_ref"),
        }
    
    def _merged_attribution(self, attribution: Dict[str, Any], *metadata_sources: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged = dict(attribution or {})
        for metadata in metadata_sources:
            if not metadata:
                continue
            for key in ("journal_run_id", "journal_ref"):
                if key not in merged or merged.get(key) in (None, ""):
                    value = metadata.get(key)
                    if value not in (None, ""):
                        merged[key] = value
        return merged

    def _resolve_journal_run_id(self, attribution: Optional[Dict[str, Any]]) -> Optional[str]:
        if self.journal_service is None:
            return None
        payload = dict(attribution or {})
        try:
            return self.journal_service.resolve_run_id(
                journal_run_id=payload.get("journal_run_id"),
                journal_ref=payload.get("journal_ref"),
            )
        except Exception:
            return None

    async def _record_journal_order(self, order: PaperOrder) -> None:
        run_id = self._resolve_journal_run_id(order.metadata)
        if not run_id or self.journal_service is None:
            return
        await asyncio.to_thread(self.journal_service.record_paper_order, run_id=run_id, order_id=order.order_id)

    async def _record_journal_trade(self, trade: PaperTrade) -> None:
        run_id = self._resolve_journal_run_id(trade.metadata)
        if not run_id or self.journal_service is None:
            return
        await asyncio.to_thread(
            self.journal_service.record_paper_trade,
            run_id=run_id,
            trade_id=trade.trade_id,
            order_id=trade.order_id,
            trade_timestamp=trade.trade_timestamp,
            side=str(trade.transaction_type),
            quantity=int(trade.quantity),
            price=trade.price,
            payload=dict(trade.metadata or {}),
        )

    async def _market_snapshot(self, instrument_token: int) -> Dict[str, Any]:
        if self.market_data_runtime is None:
            return {}
        tick = await self.market_data_runtime.get_tick(instrument_token)
        if tick:
            return dict(tick)
        last_price = await self.market_data_runtime.get_last_price(instrument_token)
        return {"last_price": last_price} if last_price is not None else {}

    def _reference_price(self, *, request: Dict[str, Any], instrument: Dict[str, Any], market_snapshot: Dict[str, Any]) -> Decimal:
        if str(request["order_type"]).upper() in {"LIMIT", "SL"} and request.get("price") is not None:
            return Decimal(str(request["price"]))
        if str(request["order_type"]).upper() == "SL-M" and request.get("trigger_price") is not None:
            return Decimal(str(request["trigger_price"]))
        if market_snapshot.get("last_price") is not None:
            return Decimal(str(market_snapshot["last_price"]))
        if instrument.get("last_price") is not None:
            return Decimal(str(instrument["last_price"]))
        return Decimal("0")

    def _position_margin(self, position: PaperPosition | None, *, instrument_type: Any, reference_price: Decimal) -> Decimal:
        if position is None or int(position.net_quantity) == 0:
            return Decimal("0")
        side = "BUY" if int(position.net_quantity) > 0 else "SELL"
        return self.margin_engine.required_margin(
            side=side,
            product=position.product,
            quantity=abs(int(position.net_quantity)),
            reference_price=reference_price if reference_price > 0 else Decimal(position.average_price or 0),
            instrument_type=str(instrument_type or position.metadata.get("instrument_type") or ""),
        )

    def _apply_fill_to_position(self, *, account_scope: str, position: PaperPosition | None, request: Dict[str, Any], fill_price: Decimal, instrument: Dict[str, Any]) -> PaperPosition:
        side = 1 if str(request["transaction_type"]).upper() == "BUY" else -1
        qty = int(request["quantity"])
        existing_qty = int(position.net_quantity) if position is not None else 0
        avg_price = Decimal(position.average_price) if position is not None else Decimal("0")
        realized_pnl = Decimal(position.realized_pnl) if position is not None else Decimal("0")
        buy_qty = int(position.buy_quantity) if position is not None else 0
        sell_qty = int(position.sell_quantity) if position is not None else 0
        buy_value = Decimal(position.buy_value) if position is not None else Decimal("0")
        sell_value = Decimal(position.sell_value) if position is not None else Decimal("0")

        if side > 0:
            buy_qty += qty
            buy_value += fill_price * Decimal(qty)
        else:
            sell_qty += qty
            sell_value += fill_price * Decimal(qty)

        if existing_qty == 0 or (existing_qty > 0 and side > 0) or (existing_qty < 0 and side < 0):
            new_qty = existing_qty + (side * qty)
            weighted_base_qty = abs(existing_qty)
            new_avg = fill_price if weighted_base_qty == 0 else ((avg_price * Decimal(weighted_base_qty)) + (fill_price * Decimal(qty))) / Decimal(abs(new_qty))
        else:
            closing_qty = min(abs(existing_qty), qty)
            if existing_qty > 0:
                realized_pnl += (fill_price - avg_price) * Decimal(closing_qty)
            else:
                realized_pnl += (avg_price - fill_price) * Decimal(closing_qty)
            remaining_qty = qty - closing_qty
            new_qty = existing_qty + (side * qty)
            if new_qty == 0:
                new_avg = Decimal("0")
            elif remaining_qty > 0 and ((new_qty > 0 and side > 0) or (new_qty < 0 and side < 0)):
                new_avg = fill_price
            else:
                new_avg = avg_price

        last_price = fill_price
        unrealized = Decimal("0")
        if new_qty > 0:
            unrealized = (last_price - new_avg) * Decimal(new_qty)
        elif new_qty < 0:
            unrealized = (new_avg - last_price) * Decimal(abs(new_qty))

        metadata = dict(position.metadata if position is not None else {})
        metadata["instrument_type"] = str(instrument.get("instrument_type") or "")
        metadata["last_price"] = str(last_price)

        return PaperPosition(
            account_scope=position.account_scope if position is not None else account_scope,
            instrument_token=int(instrument["instrument_token"]),
            product=request["product"],
            exchange=request["exchange"],
            tradingsymbol=request["tradingsymbol"],
            net_quantity=new_qty,
            average_price=new_avg,
            buy_quantity=buy_qty,
            sell_quantity=sell_qty,
            buy_value=buy_value,
            sell_value=sell_value,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized,
            updated_at=_utcnow(),
            metadata=metadata,
        )

    async def _fill_order(
        self,
        *,
        order: PaperOrder,
        account: PaperAccount,
        request: Dict[str, Any],
        instrument: Dict[str, Any],
        fill_price: Decimal,
        existing_position: PaperPosition | None,
    ) -> Dict[str, Any]:
        reserved_margin = Decimal(str((order.metadata or {}).get("reserved_margin") or "0"))
        old_margin = self._position_margin(existing_position, instrument_type=instrument.get("instrument_type"), reference_price=fill_price)
        new_position = self._apply_fill_to_position(account_scope=order.account_scope, position=existing_position, request=request, fill_price=fill_price, instrument=instrument)
        new_position = new_position.model_copy(update={"account_scope": order.account_scope})
        new_margin = self._position_margin(new_position, instrument_type=instrument.get("instrument_type"), reference_price=fill_price)
        margin_adjustment = new_margin - (old_margin + reserved_margin)
        charges = Decimal(str((order.metadata or {}).get("estimated_charges") or "0"))
        projected_available = Decimal(account.available_funds) - margin_adjustment - charges
        if projected_available < 0:
            rejected = order.model_copy(update={"status": PaperOrderStatus.REJECTED, "updated_at": _utcnow(), "metadata": {**order.metadata, "rejection_reason": "Insufficient funds for fill-time margin adjustment"}})
            rejected = await asyncio.to_thread(self.repository.update_order, rejected)
            await self._apply_account_delta(account, reserve_delta=-reserved_margin, reserve_entry=rejected.order_id)
            await publish_event("paper.orders.events", paper_order_event_payload(event_type="rejected", order=rejected))
            return {"mode": "paper", "status": "rejected", "reason": "Insufficient funds for fill-time margin adjustment", "order": rejected.model_dump(mode="json")}

        if margin_adjustment != 0:
            account = await self._apply_account_delta(account, reserve_delta=margin_adjustment, reserve_entry=order.order_id)
        account = await self._apply_cash_delta(account, delta=-charges, entry_type=FundLedgerEntryType.DEBIT, reference_id=order.order_id, notes="paper_trade_charges")

        realized_delta = Decimal(new_position.realized_pnl) - Decimal(existing_position.realized_pnl if existing_position is not None else 0)
        if realized_delta != 0:
            account = await self._apply_cash_delta(
                account,
                delta=realized_delta,
                entry_type=FundLedgerEntryType.CREDIT if realized_delta > 0 else FundLedgerEntryType.DEBIT,
                reference_id=order.order_id,
                notes="paper_realized_pnl",
            )
            account = account.model_copy(update={"realized_pnl": Decimal(account.realized_pnl) + realized_delta, "updated_at": _utcnow()})
            account = await asyncio.to_thread(self.repository.upsert_account, account)

        completed = order.model_copy(
            update={
                "status": PaperOrderStatus.FILLED,
                "filled_quantity": order.quantity,
                "pending_quantity": 0,
                "average_price": fill_price,
                "updated_at": _utcnow(),
                "completed_at": _utcnow(),
            }
        )
        completed = await asyncio.to_thread(self.repository.update_order, completed)
        trade = await asyncio.to_thread(
            self.repository.insert_trade,
            PaperTrade(
                account_scope=order.account_scope,
                trade_id=f"PTRD-{uuid.uuid4().hex[:12].upper()}",
                order_id=order.order_id,
                instrument_token=order.instrument_token,
                transaction_type=order.transaction_type,
                quantity=order.quantity,
                price=fill_price,
                trade_timestamp=_utcnow(),
                metadata=dict(order.metadata or {}),
            ),
        )
        await self._record_journal_trade(trade)
        new_position.metadata["margin_in_use"] = str(new_margin)
        new_position.metadata["last_price"] = str(fill_price)
        new_position = await asyncio.to_thread(self.repository.upsert_position, new_position)
        await publish_event("paper.orders.events", paper_order_event_payload(event_type="filled", order=completed))
        await publish_event("paper.trades.events", paper_trade_event_payload(event_type="filled", trade=trade))
        await publish_event("paper.positions.events", paper_position_event_payload(event_type="updated", position=new_position))
        return {
            "mode": "paper",
            "status": "filled",
            "order": completed.model_dump(mode="json"),
            "trade": trade.model_dump(mode="json"),
            "position": new_position.model_dump(mode="json"),
        }

    async def _mark_to_market(self, position: PaperPosition, tick: Dict[str, Any]) -> None:
        last_price = Decimal(str(tick.get("last_price") or 0))
        if last_price <= 0:
            return
        average_price = Decimal(position.average_price)
        unrealized = Decimal("0")
        if int(position.net_quantity) > 0:
            unrealized = (last_price - average_price) * Decimal(int(position.net_quantity))
        elif int(position.net_quantity) < 0:
            unrealized = (average_price - last_price) * Decimal(abs(int(position.net_quantity)))
        updated = position.model_copy(update={"unrealized_pnl": unrealized, "updated_at": _utcnow(), "metadata": {**position.metadata, "last_price": str(last_price)}})
        updated = await asyncio.to_thread(self.repository.upsert_position, updated)
        await publish_event("paper.positions.events", paper_position_event_payload(event_type="mark_to_market", position=updated))

    def _place_order_locked_uow(
        self,
        *,
        uow: Any,
        account_scope: str,
        account: PaperAccount,
        request: Dict[str, Any],
        instrument: Dict[str, Any],
        market_snapshot: Dict[str, Any],
        attribution: Dict[str, Any],
        staged_events: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Dict[str, Any], PaperAccount]:
        existing_position = uow.get_position(account_scope, int(instrument["instrument_token"]), request["product"])
        reference_price = self._reference_price(request=request, instrument=instrument, market_snapshot=market_snapshot)
        if reference_price <= 0:
            return self._build_rejected_result(account_scope, request, attribution, reason="No reference price available for paper execution"), account

        lot_size = int(instrument.get("lot_size") or 1)
        if lot_size > 1 and int(request["quantity"]) % lot_size != 0:
            return self._build_rejected_result(account_scope, request, attribution, reason=f"Quantity must be a multiple of lot size {lot_size}"), account

        old_margin = self._position_margin(existing_position, instrument_type=instrument.get("instrument_type"), reference_price=reference_price)
        hypothetical = self._apply_fill_to_position(account_scope=account_scope, position=existing_position, request=request, fill_price=reference_price, instrument=instrument)
        new_margin = self._position_margin(hypothetical, instrument_type=instrument.get("instrument_type"), reference_price=reference_price)
        incremental_margin = max(new_margin - old_margin, Decimal("0"))
        estimated_charges = self.charges_calculator.estimate(
            price=reference_price,
            quantity=request["quantity"],
            instrument_type=instrument.get("instrument_type"),
            exchange=request["exchange"],
            product=request["product"],
        )
        required_cash = incremental_margin + estimated_charges
        if account.available_funds < required_cash:
            return self._build_rejected_result(account_scope, request, attribution, reason="Insufficient paper funds or margin"), account

        order = PaperOrder(
            account_scope=account_scope,
            order_id=f"PAPER-{uuid.uuid4().hex[:12].upper()}",
            instrument_token=int(instrument["instrument_token"]),
            exchange=request["exchange"],
            tradingsymbol=request["tradingsymbol"],
            product=request["product"],
            transaction_type=str(request["transaction_type"]).lower(),
            order_type=str(request["order_type"]).replace("-", "_").lower(),
            quantity=request["quantity"],
            price=Decimal(str(request["price"])) if request.get("price") is not None else None,
            trigger_price=Decimal(str(request["trigger_price"])) if request.get("trigger_price") is not None else None,
            status=PaperOrderStatus.PENDING,
            metadata={
                **attribution,
                "reserved_margin": str(incremental_margin),
                "estimated_charges": str(estimated_charges),
                "instrument_type": str(instrument.get("instrument_type") or ""),
                "lot_size": lot_size,
            },
        )
        order = uow.insert_order(order)
        account = self._apply_account_delta_uow(uow, account, reserve_delta=incremental_margin, reserve_entry=order.order_id)
        staged_events.append(("paper.orders.events", paper_order_event_payload(event_type="accepted", order=order)))
        order = self._persist_stop_limit_trigger_state_uow(uow, order=order, request=request, market_snapshot=market_snapshot)

        if not self._should_fill_immediately(order, request=request, market_snapshot=market_snapshot):
            pending_order = order.model_copy(update={"status": PaperOrderStatus.OPEN, "updated_at": _utcnow()})
            pending_order = uow.update_order(pending_order)
            staged_events.append(("paper.orders.events", paper_order_event_payload(event_type="open", order=pending_order)))
            return {"mode": "paper", "status": "accepted", "order": pending_order.model_dump(mode="json")}, account

        fill_price = self._fill_price(order, market_snapshot=market_snapshot, fallback=reference_price)
        return self._fill_order_uow(
            uow=uow,
            order=order,
            account=account,
            request=request,
            instrument=instrument,
            fill_price=fill_price,
            existing_position=existing_position,
            staged_events=staged_events,
        )

    def _fill_order_uow(
        self,
        *,
        uow: Any,
        order: PaperOrder,
        account: PaperAccount,
        request: Dict[str, Any],
        instrument: Dict[str, Any],
        fill_price: Decimal,
        existing_position: PaperPosition | None,
        staged_events: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Dict[str, Any], PaperAccount]:
        reserved_margin = Decimal(str((order.metadata or {}).get("reserved_margin") or "0"))
        old_margin = self._position_margin(existing_position, instrument_type=instrument.get("instrument_type"), reference_price=fill_price)
        new_position = self._apply_fill_to_position(account_scope=order.account_scope, position=existing_position, request=request, fill_price=fill_price, instrument=instrument)
        new_position = new_position.model_copy(update={"account_scope": order.account_scope})
        new_margin = self._position_margin(new_position, instrument_type=instrument.get("instrument_type"), reference_price=fill_price)
        margin_adjustment = new_margin - (old_margin + reserved_margin)
        charges = Decimal(str((order.metadata or {}).get("estimated_charges") or "0"))

        projected_available = Decimal(account.available_funds) - margin_adjustment - charges
        if projected_available < 0:
            return self._build_rejected_result(order.account_scope, request, dict(order.metadata or {}), reason="Insufficient funds for fill-time margin adjustment"), account

        if margin_adjustment != 0:
            account = self._apply_account_delta_uow(uow, account, reserve_delta=margin_adjustment, reserve_entry=order.order_id)
        account = self._apply_cash_delta_uow(
            uow,
            account,
            delta=-charges,
            entry_type=FundLedgerEntryType.DEBIT,
            reference_id=order.order_id,
            notes="paper_trade_charges",
        )

        realized_delta = Decimal(new_position.realized_pnl) - Decimal(existing_position.realized_pnl if existing_position is not None else 0)
        if realized_delta != 0:
            account = self._apply_cash_delta_uow(
                uow,
                account,
                delta=realized_delta,
                entry_type=FundLedgerEntryType.CREDIT if realized_delta > 0 else FundLedgerEntryType.DEBIT,
                reference_id=order.order_id,
                notes="paper_realized_pnl",
            )
            account = account.model_copy(update={"realized_pnl": Decimal(account.realized_pnl) + realized_delta, "updated_at": _utcnow()})
            account = uow.upsert_account(account)

        completed = order.model_copy(
            update={
                "status": PaperOrderStatus.FILLED,
                "filled_quantity": order.quantity,
                "pending_quantity": 0,
                "average_price": fill_price,
                "updated_at": _utcnow(),
                "completed_at": _utcnow(),
            }
        )
        completed = uow.update_order(completed)
        trade = uow.insert_trade(
            PaperTrade(
                account_scope=order.account_scope,
                trade_id=f"PTRD-{uuid.uuid4().hex[:12].upper()}",
                order_id=order.order_id,
                instrument_token=order.instrument_token,
                transaction_type=order.transaction_type,
                quantity=order.quantity,
                price=fill_price,
                trade_timestamp=_utcnow(),
                metadata=dict(order.metadata or {}),
            )
        )
        new_position.metadata["margin_in_use"] = str(new_margin)
        new_position.metadata["last_price"] = str(fill_price)
        new_position = uow.upsert_position(new_position)

        staged_events.append(("paper.orders.events", paper_order_event_payload(event_type="filled", order=completed)))
        staged_events.append(("paper.trades.events", paper_trade_event_payload(event_type="filled", trade=trade)))
        staged_events.append(("paper.positions.events", paper_position_event_payload(event_type="updated", position=new_position)))

        return {
            "mode": "paper",
            "status": "filled",
            "order": completed.model_dump(mode="json"),
            "trade": trade.model_dump(mode="json"),
            "position": new_position.model_dump(mode="json"),
        }, account

    def _apply_account_delta_uow(self, uow: Any, account: PaperAccount, *, reserve_delta: Decimal, reserve_entry: str) -> PaperAccount:
        if reserve_delta == 0:
            return account
        new_account = account.model_copy(
            update={
                "available_funds": Decimal(account.available_funds) - reserve_delta,
                "blocked_funds": Decimal(account.blocked_funds) + reserve_delta,
                "updated_at": _utcnow(),
            }
        )
        new_account = uow.upsert_account(new_account)
        uow.append_fund_ledger_entry(
            PaperFundLedgerEntry(
                account_scope=account.account_scope,
                entry_type=FundLedgerEntryType.RESERVE if reserve_delta > 0 else FundLedgerEntryType.RELEASE,
                amount=abs(reserve_delta),
                balance_after=new_account.available_funds,
                reference_type="order",
                reference_id=reserve_entry,
                notes="paper_margin_adjustment",
            )
        )
        return new_account

    def _apply_cash_delta_uow(
        self,
        uow: Any,
        account: PaperAccount,
        *,
        delta: Decimal,
        entry_type: FundLedgerEntryType,
        reference_id: str,
        notes: str,
    ) -> PaperAccount:
        if delta == 0:
            return account
        new_account = account.model_copy(update={"available_funds": Decimal(account.available_funds) + delta, "updated_at": _utcnow()})
        new_account = uow.upsert_account(new_account)
        uow.append_fund_ledger_entry(
            PaperFundLedgerEntry(
                account_scope=account.account_scope,
                entry_type=entry_type,
                amount=abs(delta),
                balance_after=new_account.available_funds,
                reference_type="order",
                reference_id=reference_id,
                notes=notes,
            )
        )
        return new_account

    def _build_rejected_result(self, account_scope: str, request: Dict[str, Any], attribution: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
        order = PaperOrder(
            account_scope=account_scope,
            order_id=f"PAPER-{uuid.uuid4().hex[:12].upper()}",
            instrument_token=int((attribution.get("instrument_token") or 0)),
            exchange=request["exchange"],
            tradingsymbol=request["tradingsymbol"],
            product=request["product"],
            transaction_type=str(request["transaction_type"]).lower(),
            order_type=str(request["order_type"]).replace("-", "_").lower(),
            quantity=request["quantity"],
            status=PaperOrderStatus.REJECTED,
            metadata={**attribution, "rejection_reason": reason},
        )
        return {"mode": "paper", "status": "rejected", "reason": reason, "order": order.model_dump(mode="json")}

    async def _apply_account_delta(self, account: PaperAccount, *, reserve_delta: Decimal, reserve_entry: str) -> PaperAccount:
        if reserve_delta == 0:
            return account
        new_account = account.model_copy(
            update={
                "available_funds": Decimal(account.available_funds) - reserve_delta,
                "blocked_funds": Decimal(account.blocked_funds) + reserve_delta,
                "updated_at": _utcnow(),
            }
        )
        new_account = await asyncio.to_thread(self.repository.upsert_account, new_account)
        await asyncio.to_thread(
            self.repository.append_fund_ledger_entry,
            PaperFundLedgerEntry(
                account_scope=account.account_scope,
                entry_type=FundLedgerEntryType.RESERVE if reserve_delta > 0 else FundLedgerEntryType.RELEASE,
                amount=abs(reserve_delta),
                balance_after=new_account.available_funds,
                reference_type="order",
                reference_id=reserve_entry,
                notes="paper_margin_adjustment",
            ),
        )
        return new_account

    async def _apply_cash_delta(
        self,
        account: PaperAccount,
        *,
        delta: Decimal,
        entry_type: FundLedgerEntryType,
        reference_id: str,
        notes: str,
    ) -> PaperAccount:
        if delta == 0:
            return account
        new_account = account.model_copy(update={"available_funds": Decimal(account.available_funds) + delta, "updated_at": _utcnow()})
        new_account = await asyncio.to_thread(self.repository.upsert_account, new_account)
        await asyncio.to_thread(
            self.repository.append_fund_ledger_entry,
            PaperFundLedgerEntry(
                account_scope=account.account_scope,
                entry_type=entry_type,
                amount=abs(delta),
                balance_after=new_account.available_funds,
                reference_type="order",
                reference_id=reference_id,
                notes=notes,
            ),
        )
        return new_account

    async def _reject_order(self, account_scope: str, request: Dict[str, Any], attribution: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
        order = await asyncio.to_thread(
            self.repository.insert_order,
            PaperOrder(
                account_scope=account_scope,
                order_id=f"PAPER-{uuid.uuid4().hex[:12].upper()}",
                instrument_token=int((attribution.get("instrument_token") or 0)),
                exchange=request["exchange"],
                tradingsymbol=request["tradingsymbol"],
                product=request["product"],
                transaction_type=str(request["transaction_type"]).lower(),
                order_type=str(request["order_type"]).replace("-", "_").lower(),
                quantity=request["quantity"],
                status=PaperOrderStatus.REJECTED,
                metadata={**attribution, "rejection_reason": reason},
            ),
        )
        await publish_event("paper.orders.events", paper_order_event_payload(event_type="rejected", order=order))
        return {"mode": "paper", "status": "rejected", "reason": reason, "order": order.model_dump(mode="json")}

    def _matches_attribution(self, metadata: Dict[str, Any], *, strategy_tag: str | None, algo_instance_id: str | None) -> bool:
        if strategy_tag and str(metadata.get("strategy_tag") or "") != strategy_tag:
            return False
        if algo_instance_id and str(metadata.get("algo_instance_id") or "") != algo_instance_id:
            return False
        return True

    def _should_fill_immediately(self, order: PaperOrder, *, request: Dict[str, Any], market_snapshot: Dict[str, Any]) -> bool:
        return self._request_should_fill_immediately(request=request, market_snapshot=market_snapshot)

    def _request_should_fill_immediately(self, *, request: Dict[str, Any], market_snapshot: Dict[str, Any]) -> bool:
        if str(request["order_type"]).upper() == "MARKET":
            return True
        return self._order_should_trigger(None, request=request, market_snapshot=market_snapshot)

    def _order_should_trigger(self, order: PaperOrder | None, *, request: Dict[str, Any], market_snapshot: Dict[str, Any]) -> bool:
        last_price = Decimal(str(market_snapshot.get("last_price") or 0))
        if last_price <= 0:
            return False
        if str(request["order_type"]).upper() == "LIMIT":
            return self._limit_order_fillable(request=request, market_snapshot=market_snapshot)
        if str(request["order_type"]).upper() == "SL":
            stop_triggered = bool((order.metadata or {}).get("stop_triggered")) if order is not None else self._stop_limit_trigger_reached(request=request, market_snapshot=market_snapshot)
            return stop_triggered and self._limit_order_fillable(request=request, market_snapshot=market_snapshot)
        if str(request["order_type"]).upper() == "SL-M":
            trigger_price = Decimal(str(request.get("trigger_price") or 0))
            if str(request["transaction_type"]).upper() == "BUY":
                return last_price >= trigger_price
            return last_price <= trigger_price
        return False

    def _stop_limit_trigger_reached(self, *, request: Dict[str, Any], market_snapshot: Dict[str, Any]) -> bool:
        last_price = Decimal(str(market_snapshot.get("last_price") or 0))
        trigger_price = Decimal(str(request.get("trigger_price") or 0))
        if last_price <= 0 or trigger_price <= 0:
            return False
        if str(request["transaction_type"]).upper() == "BUY":
            return last_price >= trigger_price
        return last_price <= trigger_price

    def _limit_order_fillable(self, *, request: Dict[str, Any], market_snapshot: Dict[str, Any]) -> bool:
        last_price = Decimal(str(market_snapshot.get("last_price") or 0))
        limit_price = Decimal(str(request.get("price") or 0))
        if limit_price <= 0 or last_price <= 0:
            return False
        depth = market_snapshot.get("depth") or {}
        buy_depth = depth.get("buy") or []
        sell_depth = depth.get("sell") or []
        if str(request["transaction_type"]).upper() == "BUY":
            best_sell = Decimal(str(sell_depth[0].get("price") or 0)) if sell_depth else Decimal("0")
            if best_sell > 0:
                return best_sell <= limit_price
            return last_price <= limit_price
        best_buy = Decimal(str(buy_depth[0].get("price") or 0)) if buy_depth else Decimal("0")
        if best_buy > 0:
            return best_buy >= limit_price
        return last_price >= limit_price

    def _fill_price(self, order: PaperOrder, *, market_snapshot: Dict[str, Any], fallback: Decimal) -> Decimal:
        depth = market_snapshot.get("depth") or {}
        buy_depth = depth.get("buy") or []
        sell_depth = depth.get("sell") or []
        order_type = self._paper_order_type_value(order.order_type)
        side = self._paper_side_value(order.transaction_type)
        if order_type == PaperOrderType.MARKET.value:
            if side == PaperOrderSide.BUY.value and sell_depth:
                return Decimal(str(sell_depth[0].get("price") or fallback))
            if side == PaperOrderSide.SELL.value and buy_depth:
                return Decimal(str(buy_depth[0].get("price") or fallback))
            return fallback
        if order_type == PaperOrderType.LIMIT.value and order.price is not None:
            return Decimal(order.price)
        if order_type == PaperOrderType.SL.value and order.price is not None:
            return Decimal(order.price)
        if order_type == PaperOrderType.SL_M.value:
            if side == PaperOrderSide.BUY.value and sell_depth:
                return Decimal(str(sell_depth[0].get("price") or fallback))
            if side == PaperOrderSide.SELL.value and buy_depth:
                return Decimal(str(buy_depth[0].get("price") or fallback))
            return fallback
        return Decimal(str(market_snapshot.get("last_price") or fallback))

    def _request_from_paper_order(self, order: PaperOrder) -> Dict[str, Any]:
        transaction_type = self._paper_side_value(order.transaction_type).upper()
        order_type = self._paper_order_type_value(order.order_type)
        return {
            "exchange": order.exchange,
            "tradingsymbol": order.tradingsymbol or "",
            "transaction_type": transaction_type,
            "variety": "regular",
            "product": order.product,
            "order_type": ("SL-M" if order_type == PaperOrderType.SL_M.value else order_type.upper()),
            "quantity": order.quantity,
            "price": float(order.price) if order.price is not None else None,
            "trigger_price": float(order.trigger_price) if order.trigger_price is not None else None,
        }

    def _paper_order_type_value(self, value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value or "").lower()

    def _paper_side_value(self, value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value or "").lower()

    async def _persist_stop_limit_trigger_state(self, *, order: PaperOrder, request: Dict[str, Any], market_snapshot: Dict[str, Any]) -> PaperOrder:
        if self._paper_order_type_value(order.order_type) != PaperOrderType.SL.value:
            return order
        if bool((order.metadata or {}).get("stop_triggered")):
            return order
        if not self._stop_limit_trigger_reached(request=request, market_snapshot=market_snapshot):
            return order
        updated = order.model_copy(
            update={
                "updated_at": _utcnow(),
                "metadata": {**dict(order.metadata or {}), "stop_triggered": True, "stop_triggered_at": _utcnow().isoformat()},
            }
        )
        return await asyncio.to_thread(self.repository.update_order, updated)

    def _persist_stop_limit_trigger_state_uow(self, uow: Any, *, order: PaperOrder, request: Dict[str, Any], market_snapshot: Dict[str, Any]) -> PaperOrder:
        if self._paper_order_type_value(order.order_type) != PaperOrderType.SL.value:
            return order
        if bool((order.metadata or {}).get("stop_triggered")):
            return order
        if not self._stop_limit_trigger_reached(request=request, market_snapshot=market_snapshot):
            return order
        updated = order.model_copy(
            update={
                "updated_at": _utcnow(),
                "metadata": {**dict(order.metadata or {}), "stop_triggered": True, "stop_triggered_at": _utcnow().isoformat()},
            }
        )
        return uow.update_order(updated)

    def _account_lock(self, account_scope: str) -> asyncio.Lock:
        lock = self._account_locks.get(account_scope)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[account_scope] = lock
        return lock

    async def _preflight_basket(self, account_scope: str, orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        account = await self.ensure_account(account_scope)
        simulated_account = account.model_copy(deep=True)
        simulated_positions: Dict[tuple[int, str], PaperPosition | None] = {}

        for index, raw_order in enumerate(orders):
            request = self._normalize_order_request(raw_order)
            instrument = await asyncio.to_thread(
                self.instruments_repository.get_instrument_by_exchange_symbol,
                request["exchange"],
                request["tradingsymbol"],
            )
            if not instrument:
                return {"status": "rejected", "reason": f"Basket preflight failed at leg {index}: instrument not found"}

            lot_size = int(instrument.get("lot_size") or 1)
            if lot_size > 1 and int(request["quantity"]) % lot_size != 0:
                return {"status": "rejected", "reason": f"Basket preflight failed at leg {index}: quantity must be a multiple of lot size {lot_size}"}

            key = (int(instrument["instrument_token"]), request["product"])
            existing_position = simulated_positions.get(key)
            if key not in simulated_positions:
                existing_position = await asyncio.to_thread(
                    self.repository.get_position,
                    account_scope,
                    int(instrument["instrument_token"]),
                    request["product"],
                )
            market_snapshot = await self._market_snapshot(int(instrument["instrument_token"]))
            reference_price = self._reference_price(request=request, instrument=instrument, market_snapshot=market_snapshot)
            if reference_price <= 0:
                return {"status": "rejected", "reason": f"Basket preflight failed at leg {index}: no reference price available"}

            hypothetical = self._apply_fill_to_position(
                account_scope=account_scope,
                position=existing_position,
                request=request,
                fill_price=reference_price,
                instrument=instrument,
            )
            old_margin = self._position_margin(existing_position, instrument_type=instrument.get("instrument_type"), reference_price=reference_price)
            new_margin = self._position_margin(hypothetical, instrument_type=instrument.get("instrument_type"), reference_price=reference_price)
            incremental_margin = max(new_margin - old_margin, Decimal("0"))
            charges = self.charges_calculator.estimate(
                price=reference_price,
                quantity=request["quantity"],
                instrument_type=instrument.get("instrument_type"),
                exchange=request["exchange"],
                product=request["product"],
            )
            if simulated_account.available_funds < incremental_margin + charges:
                return {"status": "rejected", "reason": f"Basket preflight failed at leg {index}: insufficient paper funds or margin"}

            if self._request_should_fill_immediately(request=request, market_snapshot=market_snapshot):
                realized_delta = Decimal(hypothetical.realized_pnl) - Decimal(existing_position.realized_pnl if existing_position is not None else 0)
                margin_delta = new_margin - old_margin
                simulated_account = simulated_account.model_copy(
                    update={
                        "available_funds": Decimal(simulated_account.available_funds) - margin_delta - charges + realized_delta,
                        "blocked_funds": Decimal(simulated_account.blocked_funds) + margin_delta,
                        "realized_pnl": Decimal(simulated_account.realized_pnl) + realized_delta,
                    }
                )
                simulated_positions[key] = hypothetical
            else:
                simulated_account = simulated_account.model_copy(
                    update={
                        "available_funds": Decimal(simulated_account.available_funds) - incremental_margin,
                        "blocked_funds": Decimal(simulated_account.blocked_funds) + incremental_margin,
                    }
                )

            if simulated_account.available_funds < 0:
                return {"status": "rejected", "reason": f"Basket preflight failed at leg {index}: simulated available funds went negative"}

        return {"status": "ok"}

    def _normalize_order_request(self, order_payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(order_payload or {})
        required = ["exchange", "tradingsymbol", "transaction_type", "product", "order_type", "quantity"]
        missing = [field for field in required if payload.get(field) in (None, "")]
        if missing:
            raise ValueError(f"Paper order missing required field(s): {', '.join(missing)}")
        quantity = int(payload["quantity"])
        if quantity <= 0:
            raise ValueError("Paper order quantity must be > 0")
        order_type = str(payload["order_type"]).upper()
        if order_type == "LIMIT" and payload.get("price") is None:
            raise ValueError("Paper LIMIT order requires price")
        if order_type == "SL" and (payload.get("price") is None or payload.get("trigger_price") is None):
            raise ValueError("Paper SL order requires price and trigger_price")
        if order_type == "SL-M" and payload.get("trigger_price") is None:
            raise ValueError("Paper SL-M order requires trigger_price")
        return {
            "exchange": str(payload["exchange"]).upper(),
            "tradingsymbol": str(payload["tradingsymbol"]).strip().upper(),
            "transaction_type": str(payload["transaction_type"]).upper(),
            "variety": str(payload.get("variety") or "regular"),
            "product": str(payload["product"]).upper(),
            "order_type": order_type,
            "quantity": quantity,
            "price": payload.get("price"),
            "trigger_price": payload.get("trigger_price"),
        }
