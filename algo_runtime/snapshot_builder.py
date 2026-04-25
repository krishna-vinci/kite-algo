from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from database import SessionLocal

from .models import (
    AlgoInstance,
    CandleSeriesSpec,
    DependencySpec,
    IndicatorSpec,
    OptionExpiryMode,
    OptionReadSpec,
    OptionView,
    OrderScope,
    Snapshot,
    TriggerEvent,
)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _staleness_payload(tick: Dict[str, Any]) -> Dict[str, Any]:
    exchange_dt = _parse_dt(tick.get("exchange_timestamp") or tick.get("last_trade_time"))
    received_dt = _parse_dt(tick.get("received_at"))
    reference = exchange_dt or received_dt
    stale_age_sec = None
    if reference is not None:
        stale_age_sec = max(0.0, (datetime.now(timezone.utc) - reference.astimezone(timezone.utc)).total_seconds())
    return {
        "exchange_timestamp": exchange_dt.isoformat() if exchange_dt else None,
        "received_at": received_dt.isoformat() if received_dt else None,
        "stale_age_sec": stale_age_sec,
    }


class MarketDataReader(Protocol):
    async def get_tick(self, token: int) -> Optional[Dict[str, Any]]: ...

    async def get_last_price(self, token: int) -> Optional[float]: ...

    def get_runtime_status(self) -> str: ...


class CandleDataReader(Protocol):
    async def get_latest_closed(self, token: int, timeframe: str) -> Optional[Dict[str, Any]]: ...

    async def get_forming(self, token: int, timeframe: str) -> Optional[Dict[str, Any]]: ...

    async def get_history(self, spec: CandleSeriesSpec) -> list[Dict[str, Any]]: ...


class IndicatorDataReader(Protocol):
    async def get_indicator(self, spec: IndicatorSpec, candles: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...


class OptionsDataReader(Protocol):
    async def read(self, spec: OptionReadSpec) -> Dict[str, Any]: ...


class PositionsDataReader(Protocol):
    async def get_positions(self, account_id: str) -> Dict[str, Any]: ...


class OrdersDataReader(Protocol):
    async def get_orders(self, account_id: str, order_scope: OrderScope, limit: int = 20) -> list[Dict[str, Any]]: ...


class RuntimeMarketDataReader:
    def __init__(self, market_runtime: Any) -> None:
        self.market_runtime = market_runtime

    async def get_tick(self, token: int) -> Optional[Dict[str, Any]]:
        return await self.market_runtime.get_tick(token)

    async def get_last_price(self, token: int) -> Optional[float]:
        return await self.market_runtime.get_last_price(token)

    def get_runtime_status(self) -> str:
        return self.market_runtime.get_websocket_status()


class RedisCandleDataReader:
    def __init__(self, *, redis_client: Any, candle_storage: Any, interval_seconds: Dict[str, int]) -> None:
        self.redis = redis_client
        self.candle_storage = candle_storage
        self.interval_seconds = interval_seconds

    async def get_latest_closed(self, token: int, timeframe: str) -> Optional[Dict[str, Any]]:
        raw = await self.redis.get(f"candle:{token}:{timeframe}:latest")
        return self._decode_candle(raw)

    async def get_forming(self, token: int, timeframe: str) -> Optional[Dict[str, Any]]:
        raw = await self.redis.get(f"candle:{token}:{timeframe}:current")
        return self._decode_candle(raw)

    async def get_history(self, spec: CandleSeriesSpec) -> list[Dict[str, Any]]:
        seconds = self.interval_seconds.get(spec.timeframe)
        if seconds is None:
            return []
        to_ts = datetime.now(timezone.utc)
        from_ts = to_ts - timedelta(seconds=seconds * max(spec.lookback, 1))
        return self.candle_storage.query_candles(spec.token, spec.timeframe, from_ts, to_ts, include_oi=True)

    def _decode_candle(self, raw: Any) -> Optional[Dict[str, Any]]:
        if not raw:
            return None
        import json

        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, list) and len(data) >= 6:
            candle = {
                "ts": data[0],
                "open": data[1],
                "high": data[2],
                "low": data[3],
                "close": data[4],
                "volume": data[5],
            }
            if len(data) > 6:
                candle["oi"] = data[6]
            return candle
        return data if isinstance(data, dict) else None


class OptionsSnapshotReader:
    def __init__(self, options_session_manager: Any, strike_selector: Any | None = None) -> None:
        self.options_session_manager = options_session_manager
        self.strike_selector = strike_selector

    async def read(self, spec: OptionReadSpec) -> Dict[str, Any]:
        snapshot = self.options_session_manager.get_snapshot(spec.underlying)
        if not snapshot:
            return {}
        if spec.view == OptionView.SNAPSHOT or self.strike_selector is None:
            return snapshot

        expiry = self._resolve_expiry(snapshot, spec)
        if expiry is None:
            return snapshot
        if spec.view == OptionView.MINI_CHAIN:
            return await self.strike_selector.get_mini_chain(
                spec.underlying,
                expiry,
                count=(spec.strikes_around_atm * 2) + 1,
            )
        return snapshot

    def _resolve_expiry(self, snapshot: Dict[str, Any], spec: OptionReadSpec):
        from datetime import date

        expiries = snapshot.get("expiries") or []
        if not expiries:
            return None
        if spec.expiry_mode == OptionExpiryMode.EXACT and spec.expiry:
            for expiry in expiries:
                if expiry == spec.expiry:
                    try:
                        return date.fromisoformat(expiry)
                    except ValueError:
                        return None
            return None
        try:
            return date.fromisoformat(expiries[0])
        except ValueError:
            return None


class PositionsSnapshotReader:
    def __init__(self, positions_service: Any, *, corr_id: str = "algo_runtime") -> None:
        self.positions_service = positions_service
        self.corr_id = corr_id

    async def get_positions(self, account_id: str) -> Dict[str, Any]:
        return await self.positions_service.get_positions(account_id, self.corr_id)


class OrderProjectionReader:
    def __init__(self, session_factory: sessionmaker | Any = SessionLocal) -> None:
        self.session_factory = session_factory

    async def get_orders(self, account_id: str, order_scope: OrderScope, limit: int = 20) -> list[Dict[str, Any]]:
        if order_scope == OrderScope.INSTANCE_RELEVANT:
            raise ValueError("INSTANCE_RELEVANT order scope is not implemented yet for order projections")
        return await self._fetch_orders(account_id, order_scope, limit)

    async def _fetch_orders(self, account_id: str, order_scope: OrderScope, limit: int) -> list[Dict[str, Any]]:
        import asyncio

        return await asyncio.to_thread(self._fetch_orders_sync, account_id, order_scope, limit)

    def _fetch_orders_sync(self, account_id: str, order_scope: OrderScope, limit: int) -> list[Dict[str, Any]]:
        db: Session = self.session_factory()
        try:
            rows = db.execute(
                text(
                    """
                    SELECT order_id, latest_status, latest_event_timestamp, last_seen_filled_quantity,
                           terminal, exchange, tradingsymbol, instrument_token, product, transaction_type, updated_at
                    FROM public.order_state_projection
                    WHERE account_id = :account_id
                    ORDER BY updated_at DESC
                    LIMIT :limit
                    """
                ),
                {"account_id": account_id, "limit": limit},
            ).fetchall()
            return [dict(row._mapping) if hasattr(row, "_mapping") else dict(row) for row in rows]
        finally:
            db.close()


class DependencyFilteredSnapshotBuilder:
    def __init__(
        self,
        *,
        market_reader: Optional[MarketDataReader] = None,
        candle_reader: Optional[CandleDataReader] = None,
        indicator_reader: Optional[IndicatorDataReader] = None,
        options_reader: Optional[OptionsDataReader] = None,
        positions_reader: Optional[PositionsDataReader] = None,
        orders_reader: Optional[OrdersDataReader] = None,
    ) -> None:
        self.market_reader = market_reader
        self.candle_reader = candle_reader
        self.indicator_reader = indicator_reader
        self.options_reader = options_reader
        self.positions_reader = positions_reader
        self.orders_reader = orders_reader

    async def build_for_instance(self, instance: AlgoInstance, trigger: TriggerEvent) -> Snapshot:
        dependency_spec = instance.dependency_spec

        market = await self._build_market_section(dependency_spec)
        candles = await self._build_candle_section(dependency_spec)
        indicators = await self._build_indicator_section(dependency_spec, candles)
        options = await self._build_options_section(dependency_spec)
        positions = await self._build_positions_section(dependency_spec)
        orders = await self._build_orders_section(dependency_spec)

        return Snapshot(
            algo_instance_id=instance.instance_id,
            algo_type=instance.algo_type,
            trigger=trigger,
            meta={
                "account_id": dependency_spec.account_scope,
                "runtime_status": self.market_reader.get_runtime_status() if self.market_reader else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            market=market,
            candles=candles,
            indicators=indicators,
            options=options,
            positions=positions,
            orders=orders,
        )

    async def _build_market_section(self, dependency_spec: DependencySpec) -> Dict[str, Any]:
        if not self.market_reader or not dependency_spec.market_tokens:
            return {}
        ticks: Dict[str, Any] = {}
        ltp: Dict[str, Any] = {}
        freshness: Dict[str, Any] = {}
        for token in dependency_spec.market_tokens:
            tick = await self.market_reader.get_tick(token)
            if tick is not None:
                ticks[str(token)] = tick
                freshness[str(token)] = _staleness_payload(tick)
            last_price = await self.market_reader.get_last_price(token)
            if last_price is not None:
                ltp[str(token)] = last_price
        return {"ticks": ticks, "ltp": ltp, "freshness": freshness}

    async def _build_candle_section(self, dependency_spec: DependencySpec) -> Dict[str, Any]:
        if not self.candle_reader or not dependency_spec.candle_series:
            return {}
        payload: Dict[str, Any] = {}
        for spec in dependency_spec.candle_series:
            payload[spec.key] = {
                "latest_closed": await self.candle_reader.get_latest_closed(spec.token, spec.timeframe),
                "forming": await self.candle_reader.get_forming(spec.token, spec.timeframe) if spec.include_forming else None,
                "history": await self.candle_reader.get_history(spec),
            }
        return payload

    async def _build_indicator_section(self, dependency_spec: DependencySpec, candles: Dict[str, Any]) -> Dict[str, Any]:
        if not self.indicator_reader or not dependency_spec.indicators:
            return {}
        payload: Dict[str, Any] = {}
        candle_index = {spec.key: candles.get(spec.key) for spec in dependency_spec.candle_series}
        for spec in dependency_spec.indicators:
            matching_specs = [
                candle_spec
                for candle_spec in dependency_spec.candle_series
                if candle_spec.token == spec.token and candle_spec.timeframe == spec.timeframe
            ]
            matching_specs.sort(key=lambda candle_spec: (candle_spec.lookback, candle_spec.include_forming), reverse=True)
            related_candle = candle_index.get(matching_specs[0].key) if matching_specs else None
            payload[spec.key] = await self.indicator_reader.get_indicator(spec, related_candle)
        return payload

    async def _build_options_section(self, dependency_spec: DependencySpec) -> Dict[str, Any]:
        if not self.options_reader or not dependency_spec.option_reads:
            return {}
        payload: Dict[str, Any] = {}
        for spec in dependency_spec.option_reads:
            key = f"{spec.underlying}:{spec.expiry_mode.value}:{spec.view.value}:{spec.strikes_around_atm}:{spec.expiry or ''}"
            payload[key] = await self.options_reader.read(spec)
        return payload

    async def _build_positions_section(self, dependency_spec: DependencySpec) -> Dict[str, Any]:
        if not self.positions_reader or not dependency_spec.account_scope:
            return {}
        all_positions = await self.positions_reader.get_positions(dependency_spec.account_scope)
        filtered_positions = {
            key: value
            for key, value in all_positions.items()
            if self._position_matches_filters(value, dependency_spec.position_filters)
        }
        totals = {
            "position_count": len(filtered_positions),
            "total_pnl": sum(
                float(pos.get("pnl", 0.0)) if isinstance(pos, dict) else float(getattr(pos, "pnl", 0.0))
                for pos in filtered_positions.values()
            ),
        }
        return {"all": all_positions, "filtered": filtered_positions, "totals": totals}

    async def _build_orders_section(self, dependency_spec: DependencySpec) -> Dict[str, Any]:
        if not self.orders_reader or not dependency_spec.account_scope or dependency_spec.order_scope == OrderScope.NONE:
            return {}
        relevant = await self.orders_reader.get_orders(dependency_spec.account_scope, dependency_spec.order_scope)
        return {"relevant": relevant, "recent_updates": relevant[:5]}

    def _position_matches_filters(self, position: Any, filters: Iterable[Any]) -> bool:
        filters = list(filters)
        if not filters:
            return True
        payload = position if isinstance(position, dict) else position.model_dump()
        for filter_spec in filters:
            if filter_spec.exchange and payload.get("exchange") != filter_spec.exchange:
                continue
            if filter_spec.product and payload.get("product") != filter_spec.product:
                continue
            if filter_spec.tradingsymbol and payload.get("tradingsymbol") != filter_spec.tradingsymbol:
                continue
            if filter_spec.instrument_tokens and int(payload.get("instrument_token", 0)) not in filter_spec.instrument_tokens:
                continue
            return True
        return False


class SnapshotBuilder(DependencyFilteredSnapshotBuilder):
    def build(
        self,
        instance: AlgoInstance,
        trigger: TriggerEvent,
        *,
        meta: Optional[Dict[str, Any]] = None,
        market: Optional[Dict[str, Any]] = None,
        candles: Optional[Dict[str, Any]] = None,
        indicators: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        positions: Optional[Dict[str, Any]] = None,
        orders: Optional[Dict[str, Any]] = None,
    ) -> Snapshot:
        return Snapshot(
            algo_instance_id=instance.instance_id,
            algo_type=instance.algo_type,
            trigger=trigger,
            meta=meta or {},
            market=market or {},
            candles=candles or {},
            indicators=indicators or {},
            options=options or {},
            positions=positions or {},
            orders=orders or {},
        )
