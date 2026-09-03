from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional

from fastapi import HTTPException, Request, WebSocket
from pydantic import BaseModel, Field, field_validator

from backend.algo_runtime.models import CandleSeriesSpec
from backend.broker_api.instruments.instruments_repository import InstrumentsRepository
from backend.broker_api.orders.market_runtime_client import RUNTIME_TICKS_CHANNEL


VALID_MARKET_MODES = {"ltp", "quote", "full"}
DEFAULT_TICK_STALE_MS = 15_000
MAX_INSTRUMENT_TOKEN = 9_999_999_999
MAX_HISTORY_DAYS_BY_INTERVAL = {
    "minute": 90,
    "3minute": 180,
    "5minute": 180,
    "10minute": 240,
    "15minute": 365,
    "30minute": 365,
    "60minute": 730,
    "day": 3650,
}
MAX_PASSTHROUGH_HISTORY_DAYS_BY_INTERVAL = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}
TIMEFRAME_ALIASES = {
    "1m": "minute",
    "min": "minute",
    "minute": "minute",
    "3m": "3minute",
    "3minute": "3minute",
    "5m": "5minute",
    "5minute": "5minute",
    "10m": "10minute",
    "10minute": "10minute",
    "15m": "15minute",
    "15minute": "15minute",
    "30m": "30minute",
    "30minute": "30minute",
    "60m": "60minute",
    "1h": "60minute",
    "60minute": "60minute",
    "1d": "day",
    "day": "day",
}


def normalize_timeframe(value: str) -> str:
    canonical = TIMEFRAME_ALIASES.get(str(value or "").strip().lower())
    if not canonical:
        raise HTTPException(status_code=400, detail=f"Invalid timeframe '{value}'")
    return canonical


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def normalize_mode(value: str | None) -> str:
    mode = str(value or "quote").strip().lower()
    if mode not in VALID_MARKET_MODES:
        raise HTTPException(status_code=422, detail="mode must be one of ltp, quote, full")
    return mode


def normalize_instrument_token(value: Any) -> int:
    try:
        token = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="instrument_token must be an integer") from None
    if token <= 0 or token > MAX_INSTRUMENT_TOKEN:
        raise HTTPException(status_code=422, detail="instrument_token is out of supported range")
    return token


class WorkerInstrumentResolveRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    instrument_tokens: List[int] = Field(default_factory=list)


class WorkerQuoteRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    instrument_tokens: List[int] = Field(default_factory=list)
    mode: str = "quote"

    @field_validator("mode")
    @classmethod
    def clean_mode(cls, value: str) -> str:
        return normalize_mode(value)


class WorkerMarketSnapshotCandleRequest(BaseModel):
    symbol: Optional[str] = None
    instrument_token: Optional[int] = None
    interval: str
    lookback: int = Field(default=50, ge=1, le=500)


class WorkerMarketSnapshotRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    instrument_tokens: List[int] = Field(default_factory=list)
    candles: List[WorkerMarketSnapshotCandleRequest] = Field(default_factory=list)
    mode: str = "quote"

    @field_validator("mode")
    @classmethod
    def clean_mode(cls, value: str) -> str:
        return normalize_mode(value)


class WorkerMarketDataService:
    def __init__(
        self,
        *,
        instruments_repository: Optional[InstrumentsRepository] = None,
        market_data_runtime: Any = None,
        redis: Any = None,
        candle_reader: Any = None,
    ) -> None:
        self.instruments = instruments_repository or InstrumentsRepository()
        self.market_data_runtime = market_data_runtime
        self.redis = redis
        self.candle_reader = candle_reader

    def _instrument_payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        exchange = str(row.get("exchange") or "").strip().upper()
        tradingsymbol = str(row.get("tradingsymbol") or "").strip().upper()
        return {
            "symbol": f"{exchange}:{tradingsymbol}",
            "instrument_token": int(row["instrument_token"]),
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "name": row.get("name"),
            "instrument_type": row.get("instrument_type"),
            "segment": row.get("segment"),
            "tick_size": float(row["tick_size"]) if row.get("tick_size") is not None else None,
            "lot_size": int(row["lot_size"]) if row.get("lot_size") is not None else None,
            "expiry": row.get("expiry"),
            "strike": float(row["strike"]) if row.get("strike") is not None else None,
        }

    async def resolve_ticker(self, symbol: str) -> Dict[str, Any]:
        row = await asyncio.to_thread(self.instruments.resolve_market_symbol, symbol)
        if not row:
            raise HTTPException(status_code=404, detail=f"Instrument not found for symbol {symbol}")
        return self._instrument_payload(row)

    async def resolve_token(self, instrument_token: int) -> Dict[str, Any]:
        normalized_token = normalize_instrument_token(instrument_token)
        row = await asyncio.to_thread(self.instruments.get_instrument_by_token, normalized_token)
        if not row:
            raise HTTPException(status_code=404, detail=f"Instrument not found for token {normalized_token}")
        return self._instrument_payload(row)

    async def search_tickers(self, query: str, *, exchange: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        rows = await asyncio.to_thread(self.instruments.search_market_instruments, query, exchange=exchange, limit=limit)
        return {"results": [self._instrument_payload(row) for row in rows]}

    async def resolve_many(self, *, symbols: Iterable[str], instrument_tokens: Iterable[int]) -> Dict[str, Any]:
        instruments: List[Dict[str, Any]] = []
        missing: List[Any] = []
        for symbol in symbols:
            try:
                instruments.append(await self.resolve_ticker(symbol))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                missing.append(symbol)
        for instrument_token in instrument_tokens:
            try:
                instruments.append(await self.resolve_token(normalize_instrument_token(instrument_token)))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                missing.append(instrument_token)
        deduped = {int(item["instrument_token"]): item for item in instruments}
        return {"instruments": list(deduped.values()), "missing": missing}

    async def _resolve_one(self, *, symbol: Optional[str] = None, instrument_token: Optional[int] = None) -> Dict[str, Any]:
        if instrument_token is not None:
            return await self.resolve_token(int(instrument_token))
        if str(symbol or "").strip():
            return await self.resolve_ticker(str(symbol))
        raise HTTPException(status_code=422, detail="symbol or instrument_token is required")

    async def get_quotes(self, request: WorkerQuoteRequest) -> Dict[str, Any]:
        resolved = await self.resolve_many(symbols=request.symbols, instrument_tokens=request.instrument_tokens)
        quotes: List[Dict[str, Any]] = []
        missing = list(resolved["missing"])
        mode = normalize_mode(request.mode)
        missing_instruments: List[Dict[str, Any]] = []
        for instrument in resolved["instruments"]:
            tick = await self._get_tick(int(instrument["instrument_token"]))
            if not tick:
                missing_instruments.append(instrument)
                continue
            quotes.append(self._quote_payload(instrument, tick, mode=mode))

        if missing_instruments:
            fallback_quotes = await self._get_broker_quotes(missing_instruments, mode=mode)
            quoted_tokens = {int(item.get("instrument_token") or 0) for item in fallback_quotes}
            quotes.extend(fallback_quotes)
            for instrument in missing_instruments:
                if int(instrument["instrument_token"]) not in quoted_tokens:
                    missing.append(instrument["symbol"])
        return {"quotes": quotes, "missing": missing}

    async def get_candles(
        self,
        *,
        symbol: Optional[str] = None,
        instrument_token: Optional[int] = None,
        interval: str = "5minute",
        lookback: int = 50,
    ) -> Dict[str, Any]:
        instrument = await self._resolve_one(symbol=symbol, instrument_token=instrument_token)
        reader = self.candle_reader
        if reader is None:
            history_raw, current_raw = [], None
        else:
            history_raw, current_raw = await self._read_candles(reader, int(instrument["instrument_token"]), interval, int(lookback))
        candles = [
            normalized
            for item in history_raw
            if (normalized := self._normalize_candle(item, is_complete=True)) is not None
        ]
        current = self._normalize_candle(current_raw, is_complete=False) if current_raw is not None else None
        if current is None and candles:
            current = {**candles[-1], "is_complete": True, "source": "latest_cached_candle"}
        if current is None and normalize_timeframe(interval) == "day":
            current = await self._latest_daily_quote_candle(instrument)
        if current is None and normalize_timeframe(interval) == "day":
            current = await self._latest_historical_day_candle(int(instrument["instrument_token"]))
        return {
            "symbol": instrument["symbol"],
            "instrument_token": instrument["instrument_token"],
            "interval": interval,
            "candles": candles,
            "current": current,
            "is_stale": not candles and current is None,
        }

    async def get_historical_candles(
        self,
        *,
        symbol: Optional[str] = None,
        instrument_token: Optional[int] = None,
        timeframe: str = "day",
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        ingest: bool = True,
        passthrough: bool = False,
        background_tasks: Any = None,
    ) -> Dict[str, Any]:
        """Return historical candles through the backend-owned candle facade.

        This deliberately wraps the app's robust candle storage/ingestion path instead
        of letting external workers call broker APIs or database internals directly.
        """

        from backend.broker_api.market.candle_ingestion import CandleIngestion
        from backend.broker_api.market.candle_storage import CandleStorage, IST

        instrument = await self._resolve_one(symbol=symbol, instrument_token=instrument_token)
        interval = normalize_timeframe(timeframe)
        to_utc = to_date or utcnow()
        if to_utc.tzinfo is None:
            raise HTTPException(status_code=422, detail="to must include timezone information")
        to_utc = to_utc.astimezone(timezone.utc)

        if from_date is None:
            lookback_days = {
                "minute": 7,
                "3minute": 14,
                "5minute": 30,
                "10minute": 45,
                "15minute": 60,
                "30minute": 90,
                "60minute": 180,
                "day": 365,
            }
            from_utc = to_utc - timedelta(days=lookback_days.get(interval, 30))
        else:
            if from_date.tzinfo is None:
                raise HTTPException(status_code=422, detail="from must include timezone information")
            from_utc = from_date
            from_utc = from_utc.astimezone(timezone.utc)

        if from_utc >= to_utc:
            raise HTTPException(status_code=422, detail="from must be earlier than to")

        span_days = (to_utc - from_utc).total_seconds() / 86400
        max_days = (
            MAX_PASSTHROUGH_HISTORY_DAYS_BY_INTERVAL if passthrough else MAX_HISTORY_DAYS_BY_INTERVAL
        ).get(interval, 30)
        if span_days > max_days:
            raise HTTPException(
                status_code=422,
                detail=f"historical range for {interval} is limited to {max_days} days{' in passthrough mode' if passthrough else ''}",
            )

        ingestion = {"status": "disabled", "message": None}
        raw_candles: List[Any]
        source = "historical_db"

        if passthrough:
            kite = await self._get_system_kite_client()
            ingestion_service = CandleIngestion(kite)
            records = await ingestion_service.fetch_raw_records(
                int(instrument["instrument_token"]),
                interval,
                from_utc,
                to_utc,
            )
            raw_candles = [
                {
                    "ts": self._normalize_kite_history_ts(rec.get("date"), IST),
                    "open": rec.get("open"),
                    "high": rec.get("high"),
                    "low": rec.get("low"),
                    "close": rec.get("close"),
                    "volume": rec.get("volume", 0) or 0,
                    "oi": rec.get("oi"),
                }
                for rec in records
            ]
            source = "kite_passthrough"
            ingestion = {
                "status": "completed",
                "message": "Synchronous broker passthrough completed without cache ingestion",
            }
        else:
            if ingest:
                try:
                    kite = await self._get_system_kite_client()
                    ingestion_service = CandleIngestion(kite)
                    if background_tasks is not None:
                        background_tasks.add_task(
                            ingestion_service.ingest_historical_data,
                            int(instrument["instrument_token"]),
                            interval,
                            from_utc,
                            to_utc,
                            force_refresh=False,
                        )
                    else:
                        asyncio.create_task(
                            ingestion_service.ingest_historical_data(
                                int(instrument["instrument_token"]),
                                interval,
                                from_utc,
                                to_utc,
                                force_refresh=False,
                            )
                        )
                    ingestion = {"status": "triggered", "message": "Background ingestion started"}
                except HTTPException:
                    raise
                except Exception as exc:
                    ingestion = {"status": "error", "message": str(exc)}

            try:
                raw_candles = await asyncio.to_thread(
                    CandleStorage.query_candles,
                    int(instrument["instrument_token"]),
                    interval,
                    from_utc,
                    to_utc,
                    include_oi=True,
                    raise_errors=True,
                )
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"historical candle storage query failed: {exc}") from exc

            if ingest and raw_candles and ingestion["status"] == "triggered":
                latest_ts = raw_candles[-1].get("ts") if isinstance(raw_candles[-1], dict) else None
                latest = parse_iso(latest_ts)
                interval_seconds = {
                    "minute": 60,
                    "3minute": 180,
                    "5minute": 300,
                    "10minute": 600,
                    "15minute": 900,
                    "30minute": 1800,
                    "60minute": 3600,
                    "day": 86400,
                }
                if latest is not None and (to_utc - latest.astimezone(timezone.utc)).total_seconds() < interval_seconds.get(interval, 300) * 2:
                    ingestion = {"status": "up_to_date", "message": "Data is current"}

        candles = [
            normalized
            for item in raw_candles
            if (normalized := self._normalize_candle(item, is_complete=True)) is not None
        ]
        return {
            "symbol": instrument["symbol"],
            "instrument_token": instrument["instrument_token"],
            "timeframe": interval,
            "interval": interval,
            "from": from_utc.astimezone(IST).isoformat(),
            "to": to_utc.astimezone(IST).isoformat(),
            "count": len(candles),
            "source": source,
            "ingestion": ingestion,
            "candles": candles,
        }

    async def stream_candles(
        self,
        request: Any,
        *,
        symbol: Optional[str] = None,
        instrument_token: Optional[int] = None,
        interval: str = "5minute",
    ) -> AsyncGenerator[str, None]:
        instrument = await self._resolve_one(symbol=symbol, instrument_token=instrument_token)
        snapshot = await self.get_candles(
            symbol=instrument["symbol"],
            instrument_token=int(instrument["instrument_token"]),
            interval=interval,
            lookback=1,
        )
        yield self._sse_event("snapshot", snapshot)

        reader = self.candle_reader
        if reader is None or not hasattr(reader, "stream_candles"):
            async for event in self._stream_candles_from_redis(request, instrument=instrument, interval=interval):
                yield event
            return

        async for payload in reader.stream_candles(int(instrument["instrument_token"]), interval):
            if await request.is_disconnected():
                break
            yield self._sse_event(
                "candle",
                self._normalize_stream_candle_payload(
                    payload,
                    instrument=instrument,
                    interval=interval,
                ),
            )

    async def _stream_candles_from_redis(self, request: Any, *, instrument: Dict[str, Any], interval: str) -> AsyncGenerator[str, None]:
        redis = self.redis or getattr(self.market_data_runtime, "redis", None)
        if redis is None:
            yield self._sse_event("error", {"detail": "Candle stream is not available after snapshot"})
            return

        channel = f"realtime_candles:{int(instrument['instrument_token'])}:{interval}"
        pubsub = None
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(channel)
            idle_cycles = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except Exception as exc:
                    yield self._sse_event("error", {"detail": f"Runtime candle stream failed: {exc}"})
                    break
                if not message or message.get("type") != "message":
                    idle_cycles += 1
                    if idle_cycles >= 15:
                        yield ": heartbeat\n\n"
                        idle_cycles = 0
                    continue
                idle_cycles = 0
                payload = self._decode_pubsub_payload(message.get("data"))
                if payload is None:
                    continue
                yield self._sse_event(
                    "candle",
                    self._normalize_stream_candle_payload(payload, instrument=instrument, interval=interval),
                )
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(channel)
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

    async def get_market_snapshot(self, payload: WorkerMarketSnapshotRequest) -> Dict[str, Any]:
        quotes = await self.get_quotes(
            WorkerQuoteRequest(
                symbols=payload.symbols,
                instrument_tokens=payload.instrument_tokens,
                mode=payload.mode,
            )
        )
        candles = [
            await self.get_candles(
                symbol=item.symbol,
                instrument_token=item.instrument_token,
                interval=item.interval,
                lookback=item.lookback,
            )
            for item in payload.candles
        ]
        return {
            "quotes": quotes["quotes"],
            "candles": candles,
            "missing": quotes["missing"],
            "updated_at": utcnow().isoformat(),
        }

    async def _get_tick(self, instrument_token: int) -> Optional[Dict[str, Any]]:
        if self.market_data_runtime is None:
            return None
        return await self.market_data_runtime.get_tick(int(instrument_token))

    async def _get_broker_quotes(self, instruments: List[Dict[str, Any]], *, mode: str) -> List[Dict[str, Any]]:
        if not instruments:
            return []
        keys = [str(item["symbol"]) for item in instruments]
        by_symbol = {str(item["symbol"]): item for item in instruments}
        try:
            kite = await self._get_system_kite_client()
            raw_quotes = await asyncio.to_thread(kite.quote, keys)
        except Exception:
            return []

        quotes: List[Dict[str, Any]] = []
        for key, raw_tick in dict(raw_quotes or {}).items():
            instrument = by_symbol.get(str(key))
            if instrument is None or not isinstance(raw_tick, dict):
                continue
            tick = dict(raw_tick)
            tick.setdefault("mode", mode)
            tick.setdefault("received_at", utcnow().isoformat())
            quotes.append(self._quote_payload(instrument, tick, mode=mode))
        return quotes

    async def _latest_daily_quote_candle(self, instrument: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tick = await self._get_tick(int(instrument["instrument_token"]))
        if tick:
            quote = self._quote_payload(instrument, tick, mode="quote")
        else:
            quotes = await self._get_broker_quotes([instrument], mode="quote")
            if not quotes:
                return None
            quote = quotes[0]
        ohlc = dict(quote.get("ohlc") or {}) if isinstance(quote.get("ohlc"), dict) else {}
        open_ = ohlc.get("open")
        high = ohlc.get("high")
        low = ohlc.get("low")
        close = quote.get("last_price")
        if open_ is None or high is None or low is None or close is None:
            return None
        ts = quote.get("exchange_timestamp") or quote.get("received_at") or utcnow().isoformat()
        return {
            "ts": str(ts),
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(quote.get("volume") or 0),
            "oi": None,
            "is_complete": False,
            "source": "broker_quote_ohlc",
        }

    async def _latest_historical_day_candle(self, instrument_token: int) -> Optional[Dict[str, Any]]:
        try:
            from backend.broker_api.market.candle_storage import CandleStorage

            to_date = utcnow()
            from_date = to_date - timedelta(days=14)
            raw_candles = await asyncio.to_thread(
                CandleStorage.query_candles,
                int(instrument_token),
                "day",
                from_date,
                to_date,
                True,
                False,
            )
        except Exception:
            return None
        for item in reversed(list(raw_candles or [])):
            candle = self._normalize_candle(item, is_complete=True)
            if candle is not None:
                return {**candle, "source": "latest_historical_day"}
        return None

    def _quote_payload(self, instrument: Dict[str, Any], tick: Dict[str, Any], *, mode: str) -> Dict[str, Any]:
        received_at_raw = tick.get("received_at")
        received_at = parse_iso(received_at_raw)
        age_ms = None
        if received_at is not None:
            age_ms = max(0, int((utcnow() - received_at.astimezone(timezone.utc)).total_seconds() * 1000))
        return {
            **instrument,
            "mode": tick.get("mode") or mode,
            "last_price": tick.get("last_price"),
            "change": tick.get("change") if tick.get("change") is not None else tick.get("change_percent"),
            "ohlc": tick.get("ohlc"),
            "volume": tick.get("volume"),
            "last_quantity": tick.get("last_quantity"),
            "average_price": tick.get("average_price"),
            "buy_quantity": tick.get("buy_quantity"),
            "sell_quantity": tick.get("sell_quantity"),
            "last_trade_time": tick.get("last_trade_time"),
            "exchange_timestamp": tick.get("exchange_timestamp"),
            "received_at": received_at.isoformat() if received_at is not None else received_at_raw,
            "age_ms": age_ms,
            "is_stale": age_ms is None or age_ms > DEFAULT_TICK_STALE_MS,
        }

    async def _read_candles(self, reader: Any, instrument_token: int, interval: str, lookback: int) -> tuple[List[Any], Any]:
        candles: List[Any] = []
        current: Any = None

        if hasattr(reader, "get_candles"):
            raw = await self._maybe_await(reader.get_candles(instrument_token, interval, lookback))
            candles, current = self._unpack_candle_series(raw)
        elif hasattr(reader, "get_history"):
            raw_history = await self._read_candle_history(reader, instrument_token, interval, lookback)
            candles, _ = self._unpack_candle_series(raw_history)

        if current is None:
            if hasattr(reader, "get_current_candle"):
                current = await self._maybe_await(reader.get_current_candle(instrument_token, interval))
            elif hasattr(reader, "get_forming"):
                current = await self._maybe_await(reader.get_forming(instrument_token, interval))

        return candles, current

    async def _read_candle_history(self, reader: Any, instrument_token: int, interval: str, lookback: int) -> Any:
        get_history = getattr(reader, "get_history")
        try:
            return await self._maybe_await(
                get_history(CandleSeriesSpec(token=instrument_token, timeframe=interval, lookback=lookback, include_forming=False))
            )
        except TypeError:
            return await self._maybe_await(get_history(instrument_token, interval, lookback))

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _get_system_kite_client(self) -> Any:
        from backend.app.database import SessionLocal
        from backend.broker_api.session.kite_session import KiteSession, build_kite_client

        db = SessionLocal()
        try:
            system_session = db.query(KiteSession).filter_by(session_id="system").first()
            access_token = (
                str(system_session.access_token).strip()
                if system_session is not None and system_session.access_token
                else ""
            )
        finally:
            db.rollback()
            db.close()

        if not access_token:
            raise HTTPException(status_code=401, detail="Not authenticated; login first")
        return build_kite_client(access_token, session_id="system")

    def _unpack_candle_series(self, raw: Any) -> tuple[List[Any], Any]:
        if isinstance(raw, dict):
            candles = raw.get("candles") or raw.get("history") or raw.get("items") or []
            current = raw.get("current")
            if current is None:
                current = raw.get("forming")
            if current is None:
                current = raw.get("latest_forming")
            return list(candles or []), current
        if isinstance(raw, (list, tuple)):
            return list(raw), None
        return [], None

    def _normalize_kite_history_ts(self, value: Any, ist_tz: Any) -> Any:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            if hasattr(ist_tz, "localize"):
                return ist_tz.localize(value)
            return value.replace(tzinfo=ist_tz)
        return value

    def _normalize_candle(self, candle: Any, *, is_complete: bool) -> Optional[Dict[str, Any]]:
        if candle is None:
            return None

        if isinstance(candle, (list, tuple)):
            if len(candle) < 6:
                return None
            ts, open_, high, low, close, volume = candle[:6]
            oi = candle[6] if len(candle) > 6 else None
        elif isinstance(candle, dict):
            ts = (
                candle.get("ts")
                or candle.get("timestamp")
                or candle.get("time")
                or candle.get("start")
                or candle.get("date")
            )
            open_ = candle.get("open", candle.get("o"))
            high = candle.get("high", candle.get("h"))
            low = candle.get("low", candle.get("l"))
            close = candle.get("close", candle.get("c"))
            volume = candle.get("volume", candle.get("v"))
            oi = candle.get("oi", candle.get("open_interest"))
        else:
            return None

        parsed_ts = parse_iso(ts)
        return {
            "ts": parsed_ts.isoformat() if parsed_ts is not None else ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "oi": oi,
            "is_complete": is_complete,
        }

    def _normalize_stream_candle_payload(self, payload: Any, *, instrument: Dict[str, Any], interval: str) -> Dict[str, Any]:
        instrument_token = int(instrument["instrument_token"])
        if isinstance(payload, dict) and "candle" in payload:
            data = {key: value for key, value in payload.items() if key != "event"}
            data["symbol"] = data.get("symbol") or instrument["symbol"]
            data["instrument_token"] = int(data.get("instrument_token") or instrument_token)
            data["interval"] = data.get("interval") or interval
            data["candle"] = self._normalize_candle(data.get("candle"), is_complete=False)
            return data

        return {
            "symbol": instrument["symbol"],
            "instrument_token": instrument_token,
            "interval": interval,
            "candle": self._normalize_candle(payload, is_complete=False),
        }

    def _decode_pubsub_payload(self, raw_payload: Any) -> Any:
        if isinstance(raw_payload, (bytes, bytearray)):
            raw_payload = raw_payload.decode("utf-8")
        if isinstance(raw_payload, str):
            try:
                return json.loads(raw_payload)
            except json.JSONDecodeError:
                return None
        return raw_payload

    def _sse_event(self, event: str, payload: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"

    async def stream_ticks(
        self,
        request: Any,
        token: Any,
        *,
        symbols: Iterable[str],
        instrument_tokens: Iterable[int],
        mode: str,
    ) -> AsyncGenerator[str, None]:
        mode = normalize_mode(mode)
        requested_symbols = [str(item).strip() for item in symbols if str(item).strip()]
        requested_tokens = [int(item) for item in instrument_tokens]
        resolved = await self.resolve_many(symbols=requested_symbols, instrument_tokens=requested_tokens)
        token_map = {int(item["instrument_token"]): item for item in resolved["instruments"]}
        owner_id = f"worker:{getattr(token, 'token_id', 'unknown')}:market:{uuid.uuid4()}"
        runtime = self.market_data_runtime
        pubsub = None
        owner_registered = False

        if runtime is None:
            yield self._sse_event("error", {"detail": "Market runtime is not available"})
            return

        if not token_map:
            yield self._sse_event("error", {"detail": "At least one valid symbol or instrument token is required"})
            return

        try:
            try:
                await runtime.set_owner_subscriptions(owner_id, {instrument_token: mode for instrument_token in token_map})
                owner_registered = True
            except Exception as exc:
                yield self._sse_event("error", {"detail": f"Unable to register market runtime subscription: {exc}"})
                return

            snapshot = await self.get_quotes(
                WorkerQuoteRequest(symbols=requested_symbols, instrument_tokens=requested_tokens, mode=mode)
            )
            yield self._sse_event("snapshot", snapshot)

            redis = self.redis or getattr(runtime, "redis", None)
            if redis is None:
                yield self._sse_event("error", {"detail": "Redis runtime channel is not available"})
                return

            try:
                pubsub = redis.pubsub()
                await pubsub.subscribe(RUNTIME_TICKS_CHANNEL)
            except Exception as exc:
                yield self._sse_event("error", {"detail": f"Unable to subscribe to runtime ticks: {exc}"})
                return

            idle_cycles = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except Exception as exc:
                    detail = f"Runtime tick stream failed: {exc}"
                    if exc.__class__.__name__ == "ConnectionError":
                        detail = f"Runtime tick stream lost Redis connection: {exc}"
                    yield self._sse_event("error", {"detail": detail})
                    break

                if not message or message.get("type") != "message":
                    idle_cycles += 1
                    if idle_cycles >= 15:
                        yield ": heartbeat\n\n"
                        idle_cycles = 0
                    continue

                idle_cycles = 0
                payload = self._decode_pubsub_payload(message.get("data"))
                if payload is None:
                    continue

                payloads = payload if isinstance(payload, list) else [payload]
                ticks: List[Dict[str, Any]] = []
                for item in payloads:
                    if not isinstance(item, dict):
                        continue
                    try:
                        instrument_token = int(item.get("instrument_token") or 0)
                    except (TypeError, ValueError):
                        continue
                    instrument = token_map.get(instrument_token)
                    if instrument is None:
                        continue
                    ticks.append(self._quote_payload(instrument, item, mode=mode))

                if ticks:
                    yield self._sse_event("ticks", {"ticks": ticks})
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(RUNTIME_TICKS_CHANNEL)
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
            if owner_registered:
                try:
                    await runtime.delete_owner(owner_id)
                except Exception:
                    pass

    async def stream_ticks_ws(
        self,
        websocket: WebSocket,
        token: Any,
        *,
        symbols: Iterable[str],
        instrument_tokens: Iterable[int],
        mode: str,
    ) -> AsyncGenerator[tuple[str, Dict[str, Any]], None]:
        async for payload in self.stream_ticks(
            _WebSocketRequestAdapter(websocket),
            token,
            symbols=symbols,
            instrument_tokens=instrument_tokens,
            mode=mode,
        ):
            parsed = self._decode_sse_payload(payload)
            if parsed is not None:
                yield parsed

    async def stream_candles_ws(
        self,
        websocket: WebSocket,
        *,
        symbol: Optional[str] = None,
        instrument_token: Optional[int] = None,
        interval: str = "5minute",
    ) -> AsyncGenerator[tuple[str, Dict[str, Any]], None]:
        normalized_token = normalize_instrument_token(instrument_token) if instrument_token is not None else None
        async for payload in self.stream_candles(
            _WebSocketRequestAdapter(websocket),
            symbol=symbol,
            instrument_token=normalized_token,
            interval=interval,
        ):
            parsed = self._decode_sse_payload(payload)
            if parsed is not None:
                yield parsed

    def _decode_sse_payload(self, raw_event: str) -> Optional[tuple[str, Dict[str, Any]]]:
        event = "message"
        data_lines: List[str] = []
        for line in str(raw_event).splitlines():
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if not data_lines:
            return None
        payload_text = "\n".join(data_lines)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {"raw": payload_text}
        return event, payload


class _WebSocketRequestAdapter:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self.app = websocket.app

    async def is_disconnected(self) -> bool:
        state = getattr(self._websocket, "client_state", None)
        return state is not None and str(getattr(state, "name", state)).upper() == "DISCONNECTED"
