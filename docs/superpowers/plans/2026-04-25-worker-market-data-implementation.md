# Worker Market Data V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add worker-authenticated runtime-backed market data primitives so external algo workers can build robust non-option realtime strategies using only the SDK.

**Architecture:** Add a focused worker market-data service that resolves instruments, reads Go runtime tick cache, manages runtime owner subscriptions for streams, and reuses existing candle infrastructure. Expose the service through `/api/algo-workers/worker/market/*` endpoints with the same worker token model, then add thin SDK helpers and examples. Keep option-chain helpers out of v1 but preserve the same SDK package for a later options module.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy, Redis pub/sub via existing runtime bridge, Go market-runtime contracts, Python `requests` SDK, pytest/unittest.

---

## Current-state notes

- Worker router: `api/routers/algo_workers.py`
- Worker SDK: `sdk/python/kite_algo_worker/client.py`
- Runtime tick bridge: `broker_api/market_runtime_client.py`
- Runtime tick channel/key: `market:ticks`, `market:tick:{instrument_token}`
- Instrument repository: `broker_api/instruments_repository.py`
- Existing candle code: `broker_api/candles_api.py`, `broker_api/candle_aggregator.py`, `algo_runtime/snapshot_builder.py`
- Canonical design spec: `docs/superpowers/specs/2026-04-25-worker-market-data-design.md`

## File structure

- Create: `api/worker_market_data.py`
  - Pydantic request/response models for worker market data.
  - `WorkerMarketDataService` for instrument resolution, quote normalization, runtime subscriptions, and stream generators.
  - Small helper functions for symbol parsing, stale detection, and SSE event formatting.
- Modify: `api/routers/algo_workers.py`
  - Add `market:read` and `market:stream` actions to `DEFAULT_WORKER_ACTIONS`.
  - Import models/service from `api.worker_market_data`.
  - Add `/worker/market/*` endpoints using existing `require_worker_token` and `_require_action`.
- Modify: `broker_api/instruments_repository.py`
  - Add generic lookup/search methods needed by worker market data, without adding option-chain logic.
- Modify: `sdk/python/kite_algo_worker/client.py`
  - Add `resolve_ticker`, `resolve_tickers`, `search_tickers`, `get_quotes`, `stream_ticks`, `get_candles`, `get_current_candle`, `stream_candles`, `get_market_snapshot`.
  - Reuse the existing SSE parser style from `stream_run_pnl`.
- Modify: `sdk/python/kite_algo_worker/__init__.py`
  - Export any new public helper type aliases only if needed; keep the SDK thin.
- Modify: `sdk/python/README.md`
  - Document market-data SDK methods and worker-off behavior.
- Modify: `docs/algo-worker-development-guide.md`
  - Add market-data lifecycle, examples, and restart guidance.
- Modify: `features-doc/algo-worker-sdk/progress.md`
  - Track progress, verification, and known gaps.
- Modify: `documents/kite-backend-progress.md`
  - Update backend progress after implementation.
- Test: `tests/test_algo_worker_api.py`
  - Add worker market-data API tests with fake runtime/candle/instrument services.
- Test: `tests/test_worker_sdk.py`
  - Add SDK request and stream parser tests.
- Optional create: `sdk/python/examples/realtime_market_data_worker.py`
  - Small paper/dry-run example using resolver + candles + tick stream + grouped P&L.

---

## Task 1: Add generic instrument resolver methods

**Files:**
- Modify: `broker_api/instruments_repository.py`
- Test: `tests/test_algo_worker_api.py`

- [ ] **Step 1: Add failing repository-focused tests through the worker API test module**

Add tests near the existing `AlgoWorkerApiTests` helpers in `tests/test_algo_worker_api.py`. These tests can initially call the future service helpers after Task 2; if implementing Task 1 alone, add the expected fake repository methods to a local fake and verify intended shapes in Task 2. The target behavior is:

```python
def test_worker_market_symbol_contract_shape():
    instrument = {
        "instrument_token": 408065,
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "name": "INFOSYS",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": 0.05,
        "lot_size": 1,
        "expiry": None,
        "strike": None,
    }
    assert instrument["exchange"] == "NSE"
    assert instrument["tradingsymbol"] == "INFY"
    assert instrument["instrument_token"] == 408065
```

- [ ] **Step 2: Add generic repository methods**

In `broker_api/instruments_repository.py`, add methods after `get_instrument_by_exchange_symbol`:

```python
    def get_instrument_by_token(self, instrument_token: int) -> Optional[Dict[str, object]]:
        query = text(
            """
            SELECT instrument_token, exchange, tradingsymbol, name, instrument_type,
                   segment, tick_size, lot_size, expiry, strike
            FROM kite_instruments
            WHERE instrument_token = :instrument_token
            LIMIT 1
            """
        )
        with self._session_scope() as db:
            row = db.execute(query, {"instrument_token": int(instrument_token)}).mappings().first()
            return dict(row) if row else None

    def resolve_market_symbol(self, symbol: str) -> Optional[Dict[str, object]]:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return None
        if raw.isdigit():
            return self.get_instrument_by_token(int(raw))
        if ":" not in raw:
            return None
        exchange, tradingsymbol = raw.split(":", 1)
        if not exchange.strip() or not tradingsymbol.strip():
            return None
        return self.get_instrument_by_exchange_symbol(exchange.strip(), tradingsymbol.strip())

    def search_market_instruments(self, query: str, *, exchange: Optional[str] = None, limit: int = 20) -> List[Dict[str, object]]:
        normalized_query = f"%{str(query or '').strip().upper()}%"
        normalized_exchange = str(exchange or "").strip().upper() or None
        safe_limit = max(1, min(int(limit or 20), 50))
        sql = text(
            """
            SELECT instrument_token, exchange, tradingsymbol, name, instrument_type,
                   segment, tick_size, lot_size, expiry, strike
            FROM kite_instruments
            WHERE (:exchange IS NULL OR exchange = :exchange)
              AND (
                upper(tradingsymbol) LIKE :query
                OR upper(coalesce(name, '')) LIKE :query
              )
            ORDER BY
              CASE WHEN upper(tradingsymbol) = replace(:exact_query, '%', '') THEN 0 ELSE 1 END,
              tradingsymbol
            LIMIT :limit
            """
        )
        with self._session_scope() as db:
            rows = db.execute(
                sql,
                {
                    "query": normalized_query,
                    "exact_query": normalized_query,
                    "exchange": normalized_exchange,
                    "limit": safe_limit,
                },
            ).mappings().all()
            return [dict(row) for row in rows]
```

- [ ] **Step 3: Run a syntax/import check**

Run:

```bash
python3 - <<'PY'
import ast
from pathlib import Path
ast.parse(Path('broker_api/instruments_repository.py').read_text())
print('ok')
PY
```

Expected: `ok`.

- [ ] **Step 4: Commit Task 1**

```bash
git add broker_api/instruments_repository.py tests/test_algo_worker_api.py
git commit -m "feat: add worker instrument lookup primitives"
```

If GPG signing fails in this environment and the owner has approved unsigned commits, use:

```bash
git commit --no-gpg-sign -m "feat: add worker instrument lookup primitives"
```

---

## Task 2: Create worker market-data service models and quote snapshots

**Files:**
- Create: `api/worker_market_data.py`
- Modify: `api/routers/algo_workers.py`
- Test: `tests/test_algo_worker_api.py`

- [ ] **Step 1: Write failing API tests for resolve/search/quotes**

Add tests to `tests/test_algo_worker_api.py` that import the future endpoints from `api.routers.algo_workers`:

```python
async def test_worker_market_resolve_ticker_returns_instrument(self):
    repo = _FakeWorkerRepository()
    request = self._request(repo)
    request.app.state.worker_market_data_service = SimpleNamespace(
        resolve_ticker=AsyncMock(return_value={
            "symbol": "NSE:INFY",
            "instrument_token": 408065,
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "name": "INFOSYS",
            "instrument_type": "EQ",
            "segment": "NSE",
            "tick_size": 0.05,
            "lot_size": 1,
            "expiry": None,
            "strike": None,
        })
    )

    response = await resolve_worker_market_ticker(request, symbol="NSE:INFY")

    self.assertEqual(response["instrument_token"], 408065)
    self.assertEqual(response["symbol"], "NSE:INFY")


async def test_worker_market_quotes_require_market_read_action(self):
    token = WorkerToken(
        token_id="worker-1",
        name="limited",
        account_scope="kite:paper-a",
        allowed_modes=["paper"],
        allowed_actions=["runs:read"],
        allowed_templates=[],
    )
    repo = _FakeWorkerRepository(token=token)
    request = self._request(repo)

    with self.assertRaises(HTTPException) as ctx:
        await get_worker_market_quotes(request, WorkerQuoteRequest(symbols=["NSE:INFY"]))

    self.assertEqual(ctx.exception.status_code, 403)
```

Also update `_FakeWorkerRepository` default token actions to include new market actions once Task 2 implements them.

- [ ] **Step 2: Create `api/worker_market_data.py` with models and quote logic**

Create the file with this starting implementation:

```python
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from broker_api.instruments_repository import InstrumentsRepository
from broker_api.market_runtime_client import RUNTIME_TICKS_CHANNEL


VALID_MARKET_MODES = {"ltp", "quote", "full"}
DEFAULT_TICK_STALE_MS = 15_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def normalize_mode(value: str | None) -> str:
    mode = str(value or "quote").strip().lower()
    if mode not in VALID_MARKET_MODES:
        raise HTTPException(status_code=422, detail="mode must be one of ltp, quote, full")
    return mode


class WorkerInstrument(BaseModel):
    symbol: str
    instrument_token: int
    exchange: str
    tradingsymbol: str
    name: Optional[str] = None
    instrument_type: Optional[str] = None
    segment: Optional[str] = None
    tick_size: Optional[float] = None
    lot_size: Optional[int] = None
    expiry: Optional[Any] = None
    strike: Optional[float] = None


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
    def __init__(self, *, instruments_repository: Optional[InstrumentsRepository] = None, market_data_runtime: Any = None, redis: Any = None) -> None:
        self.instruments = instruments_repository or InstrumentsRepository()
        self.market_data_runtime = market_data_runtime
        self.redis = redis

    def _instrument_payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        exchange = str(row.get("exchange") or "").upper()
        tradingsymbol = str(row.get("tradingsymbol") or "").upper()
        payload = {
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
        return WorkerInstrument(**payload).model_dump(mode="json")

    async def resolve_ticker(self, symbol: str) -> Dict[str, Any]:
        row = await asyncio.to_thread(self.instruments.resolve_market_symbol, symbol)
        if not row:
            raise HTTPException(status_code=404, detail=f"Instrument not found for symbol {symbol}")
        return self._instrument_payload(row)

    async def resolve_token(self, instrument_token: int) -> Dict[str, Any]:
        row = await asyncio.to_thread(self.instruments.get_instrument_by_token, int(instrument_token))
        if not row:
            raise HTTPException(status_code=404, detail=f"Instrument not found for token {instrument_token}")
        return self._instrument_payload(row)

    async def search_tickers(self, query: str, *, exchange: Optional[str], limit: int) -> Dict[str, Any]:
        rows = await asyncio.to_thread(self.instruments.search_market_instruments, query, exchange=exchange, limit=limit)
        return {"results": [self._instrument_payload(row) for row in rows]}

    async def resolve_many(self, *, symbols: Iterable[str], instrument_tokens: Iterable[int]) -> Dict[str, Any]:
        resolved: List[Dict[str, Any]] = []
        missing: List[Any] = []
        for symbol in symbols:
            try:
                resolved.append(await self.resolve_ticker(symbol))
            except HTTPException:
                missing.append(symbol)
        for token in instrument_tokens:
            try:
                resolved.append(await self.resolve_token(int(token)))
            except HTTPException:
                missing.append(token)
        deduped = {item["instrument_token"]: item for item in resolved}
        return {"instruments": list(deduped.values()), "missing": missing}

    async def get_quotes(self, request: WorkerQuoteRequest) -> Dict[str, Any]:
        mode = normalize_mode(request.mode)
        resolved = await self.resolve_many(symbols=request.symbols, instrument_tokens=request.instrument_tokens)
        quotes = []
        for instrument in resolved["instruments"]:
            tick = await self._get_tick(int(instrument["instrument_token"]))
            if not tick:
                resolved["missing"].append(instrument["symbol"])
                continue
            quotes.append(self._quote_payload(instrument, tick, mode=mode))
        return {"quotes": quotes, "missing": resolved["missing"]}

    async def _get_tick(self, instrument_token: int) -> Optional[Dict[str, Any]]:
        runtime = self.market_data_runtime
        if runtime is None:
            return None
        return await runtime.get_tick(int(instrument_token))

    def _quote_payload(self, instrument: Dict[str, Any], tick: Dict[str, Any], *, mode: str) -> Dict[str, Any]:
        received_at = parse_iso(tick.get("received_at"))
        age_ms = None
        if received_at:
            age_ms = int((utcnow() - received_at.astimezone(timezone.utc)).total_seconds() * 1000)
        return {
            **instrument,
            "mode": tick.get("mode") or mode,
            "last_price": tick.get("last_price"),
            "change": tick.get("change") or tick.get("change_percent"),
            "ohlc": tick.get("ohlc"),
            "volume": tick.get("volume"),
            "last_quantity": tick.get("last_quantity"),
            "average_price": tick.get("average_price"),
            "buy_quantity": tick.get("buy_quantity"),
            "sell_quantity": tick.get("sell_quantity"),
            "last_trade_time": tick.get("last_trade_time"),
            "exchange_timestamp": tick.get("exchange_timestamp"),
            "received_at": tick.get("received_at"),
            "age_ms": age_ms,
            "is_stale": age_ms is None or age_ms > DEFAULT_TICK_STALE_MS,
        }
```

- [ ] **Step 3: Wire resolve/search/quotes endpoints**

Modify `api/routers/algo_workers.py`:

```python
from api.worker_market_data import (
    WorkerInstrumentResolveRequest,
    WorkerMarketDataService,
    WorkerMarketSnapshotRequest,
    WorkerQuoteRequest,
)
```

Update `DEFAULT_WORKER_ACTIONS`:

```python
DEFAULT_WORKER_ACTIONS = {
    "runs:create",
    "runs:read",
    "intents:submit",
    "risk:update",
    "runs:exit",
    "heartbeat",
    "market:read",
    "market:stream",
}
```

Add helper near `_repo` helpers:

```python
def _market_data_service(request: Request) -> WorkerMarketDataService:
    service = getattr(request.app.state, "worker_market_data_service", None)
    if service is not None:
        return service
    return WorkerMarketDataService(market_data_runtime=getattr(request.app.state, "market_data_runtime", None))
```

Add endpoints near worker P&L endpoints:

```python
@router.get("/worker/market/instruments/resolve")
async def resolve_worker_market_ticker(request: Request, symbol: str):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).resolve_ticker(symbol)


@router.get("/worker/market/instruments/search")
async def search_worker_market_tickers(request: Request, query: str, exchange: Optional[str] = None, limit: int = Query(20, ge=1, le=50)):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).search_tickers(query, exchange=exchange, limit=limit)


@router.post("/worker/market/instruments/resolve")
async def resolve_worker_market_tickers(request: Request, payload: WorkerInstrumentResolveRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).resolve_many(symbols=payload.symbols, instrument_tokens=payload.instrument_tokens)


@router.post("/worker/market/quotes")
async def get_worker_market_quotes(request: Request, payload: WorkerQuoteRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_quotes(payload)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_algo_worker_api.py -q
```

Expected: all existing tests pass plus new resolve/quote tests.

- [ ] **Step 5: Commit Task 2**

```bash
git add api/worker_market_data.py api/routers/algo_workers.py tests/test_algo_worker_api.py
git commit -m "feat: expose worker quote snapshots"
```

---

## Task 3: Add runtime-backed tick streaming

**Files:**
- Modify: `api/worker_market_data.py`
- Modify: `api/routers/algo_workers.py`
- Test: `tests/test_algo_worker_api.py`

- [ ] **Step 1: Write failing stream tests**

Add tests that verify SSE media type, initial snapshot, and market action gating:

```python
async def test_worker_market_tick_stream_returns_snapshot_event(self):
    repo = _FakeWorkerRepository()
    request = self._request(repo)
    request.app.state.worker_market_data_service = SimpleNamespace(
        stream_ticks=lambda request, token, symbols, instrument_tokens, mode: _single_sse('{"ticks": [], "missing": []}')
    )

    response = await stream_worker_market_ticks(request, symbols="NSE:INFY", tokens=None, mode="quote")
    chunk = await response.body_iterator.__anext__()

    self.assertEqual(response.media_type, "text/event-stream")
    self.assertIn("event: snapshot", chunk)


async def _single_sse(payload: str):
    yield f"event: snapshot\ndata: {payload}\n\n"
```

- [ ] **Step 2: Implement stream generator**

Add to `WorkerMarketDataService`:

```python
    async def stream_ticks(
        self,
        request: Request,
        token: Any,
        *,
        symbols: Iterable[str],
        instrument_tokens: Iterable[int],
        mode: str,
    ) -> AsyncGenerator[str, None]:
        mode = normalize_mode(mode)
        resolved = await self.resolve_many(symbols=symbols, instrument_tokens=instrument_tokens)
        token_map = {int(item["instrument_token"]): item for item in resolved["instruments"]}
        owner_id = f"worker:{getattr(token, 'token_id', 'unknown')}:market:{uuid.uuid4()}"
        runtime = self.market_data_runtime
        if runtime is None:
            yield "event: error\ndata: {\"detail\": \"Market runtime is not available\"}\n\n"
            return
        try:
            await runtime.set_owner_subscriptions(owner_id, {token_id: mode for token_id in token_map})
            snapshot = await self.get_quotes(WorkerQuoteRequest(instrument_tokens=list(token_map), mode=mode))
            yield f"event: snapshot\ndata: {json.dumps(snapshot, default=str)}\n\n"

            redis = self.redis or getattr(runtime, "redis", None)
            if redis is None:
                yield "event: error\ndata: {\"detail\": \"Redis runtime channel is not available\"}\n\n"
                return
            pubsub = redis.pubsub()
            await pubsub.subscribe(RUNTIME_TICKS_CHANNEL)
            heartbeat_count = 0
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message:
                        heartbeat_count += 1
                        if heartbeat_count >= 15:
                            yield ": heartbeat\n\n"
                            heartbeat_count = 0
                        continue
                    raw_payload = message.get("data")
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    payloads = payload if isinstance(payload, list) else [payload]
                    ticks = []
                    for item in payloads:
                        if not isinstance(item, dict):
                            continue
                        instrument_token = int(item.get("instrument_token") or 0)
                        if instrument_token not in token_map:
                            continue
                        ticks.append(self._quote_payload(token_map[instrument_token], item, mode=mode))
                    if ticks:
                        yield f"event: ticks\ndata: {json.dumps({'ticks': ticks}, default=str)}\n\n"
            finally:
                try:
                    await pubsub.unsubscribe(RUNTIME_TICKS_CHANNEL)
                    await pubsub.aclose()
                except Exception:
                    pass
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
        finally:
            try:
                await runtime.delete_owner(owner_id)
            except Exception:
                pass
```

- [ ] **Step 3: Wire stream endpoint**

Add to `api/routers/algo_workers.py`:

```python
def _parse_csv_values(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


@router.get("/worker/market/ticks/stream")
async def stream_worker_market_ticks(request: Request, symbols: Optional[str] = None, tokens: Optional[str] = None, mode: str = "quote"):
    token = await require_worker_token(request)
    _require_action(token, "market:stream")
    parsed_symbols = _parse_csv_values(symbols)
    parsed_tokens = [int(value) for value in _parse_csv_values(tokens)]
    return StreamingResponse(
        _market_data_service(request).stream_ticks(request, token, symbols=parsed_symbols, instrument_tokens=parsed_tokens, mode=mode),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Run stream tests**

Run:

```bash
python3 -m pytest tests/test_algo_worker_api.py -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add api/worker_market_data.py api/routers/algo_workers.py tests/test_algo_worker_api.py
git commit -m "feat: stream runtime ticks to workers"
```

---

## Task 4: Add candle snapshots and candle streaming facade

**Files:**
- Modify: `api/worker_market_data.py`
- Modify: `api/routers/algo_workers.py`
- Test: `tests/test_algo_worker_api.py`

- [ ] **Step 1: Write failing candle API tests**

Add tests for `get_worker_market_candles` and `stream_worker_market_candles` with a fake service:

```python
async def test_worker_market_candles_returns_history_and_current(self):
    repo = _FakeWorkerRepository()
    request = self._request(repo)
    request.app.state.worker_market_data_service = SimpleNamespace(
        get_candles=AsyncMock(return_value={
            "symbol": "NSE:INFY",
            "instrument_token": 408065,
            "interval": "5minute",
            "candles": [],
            "current": None,
        })
    )

    response = await get_worker_market_candles(request, symbol="NSE:INFY", instrument_token=None, interval="5minute", lookback=50)

    self.assertEqual(response["symbol"], "NSE:INFY")
    self.assertEqual(response["interval"], "5minute")
```

- [ ] **Step 2: Implement minimal candle methods with dependency injection**

In `WorkerMarketDataService.__init__`, add optional `candle_reader: Any = None` and assign `self.candle_reader = candle_reader`.

Add methods:

```python
    async def get_candles(self, *, symbol: Optional[str], instrument_token: Optional[int], interval: str, lookback: int) -> Dict[str, Any]:
        instrument = await self._resolve_one(symbol=symbol, instrument_token=instrument_token)
        reader = self.candle_reader
        if reader is None:
            return {
                "symbol": instrument["symbol"],
                "instrument_token": instrument["instrument_token"],
                "interval": interval,
                "candles": [],
                "current": None,
                "is_stale": True,
            }
        candles = await reader.get_candles(int(instrument["instrument_token"]), interval, int(lookback))
        current = await reader.get_current_candle(int(instrument["instrument_token"]), interval)
        return {
            "symbol": instrument["symbol"],
            "instrument_token": instrument["instrument_token"],
            "interval": interval,
            "candles": candles,
            "current": current,
        }

    async def _resolve_one(self, *, symbol: Optional[str], instrument_token: Optional[int]) -> Dict[str, Any]:
        if instrument_token is not None:
            return await self.resolve_token(int(instrument_token))
        if symbol:
            return await self.resolve_ticker(symbol)
        raise HTTPException(status_code=422, detail="symbol or instrument_token is required")

    async def stream_candles(self, request: Request, *, symbol: Optional[str], instrument_token: Optional[int], interval: str) -> AsyncGenerator[str, None]:
        snapshot = await self.get_candles(symbol=symbol, instrument_token=instrument_token, interval=interval, lookback=1)
        yield f"event: snapshot\ndata: {json.dumps(snapshot, default=str)}\n\n"
        reader = self.candle_reader
        if reader is None or not hasattr(reader, "stream_candles"):
            yield "event: error\ndata: {\"detail\": \"Candle stream is not available\"}\n\n"
            return
        async for payload in reader.stream_candles(int(snapshot["instrument_token"]), interval):
            if await request.is_disconnected():
                break
            yield f"event: candle\ndata: {json.dumps(payload, default=str)}\n\n"
```

If no existing reader exactly supports this shape, create a small adapter in this file using existing Redis candle keys and DB-backed query helpers. Keep it behind the `candle_reader` interface so tests remain simple.

- [ ] **Step 3: Wire candle endpoints**

Add to `api/routers/algo_workers.py`:

```python
@router.get("/worker/market/candles")
async def get_worker_market_candles(request: Request, symbol: Optional[str] = None, instrument_token: Optional[int] = None, interval: str = "5minute", lookback: int = Query(50, ge=1, le=500)):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_candles(symbol=symbol, instrument_token=instrument_token, interval=interval, lookback=lookback)


@router.get("/worker/market/candles/stream")
async def stream_worker_market_candles(request: Request, symbol: Optional[str] = None, instrument_token: Optional[int] = None, interval: str = "5minute"):
    token = await require_worker_token(request)
    _require_action(token, "market:stream")
    return StreamingResponse(
        _market_data_service(request).stream_candles(request, symbol=symbol, instrument_token=instrument_token, interval=interval),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Run candle tests**

Run:

```bash
python3 -m pytest tests/test_algo_worker_api.py -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add api/worker_market_data.py api/routers/algo_workers.py tests/test_algo_worker_api.py
git commit -m "feat: expose worker candle data"
```

---

## Task 5: Add combined market snapshot endpoint

**Files:**
- Modify: `api/worker_market_data.py`
- Modify: `api/routers/algo_workers.py`
- Test: `tests/test_algo_worker_api.py`

- [ ] **Step 1: Write failing snapshot test**

```python
async def test_worker_market_snapshot_combines_quotes_and_candles(self):
    repo = _FakeWorkerRepository()
    request = self._request(repo)
    request.app.state.worker_market_data_service = SimpleNamespace(
        get_market_snapshot=AsyncMock(return_value={"quotes": [], "candles": [], "missing": [], "updated_at": "2026-04-25T00:00:00+00:00"})
    )

    response = await get_worker_market_snapshot(request, WorkerMarketSnapshotRequest(symbols=["NSE:INFY"]))

    self.assertIn("quotes", response)
    self.assertIn("candles", response)
```

- [ ] **Step 2: Implement service method**

Add to `WorkerMarketDataService`:

```python
    async def get_market_snapshot(self, payload: WorkerMarketSnapshotRequest) -> Dict[str, Any]:
        quote_response = await self.get_quotes(
            WorkerQuoteRequest(symbols=payload.symbols, instrument_tokens=payload.instrument_tokens, mode=payload.mode)
        )
        candle_responses = []
        for candle in payload.candles:
            candle_responses.append(
                await self.get_candles(
                    symbol=candle.symbol,
                    instrument_token=candle.instrument_token,
                    interval=candle.interval,
                    lookback=candle.lookback,
                )
            )
        return {
            "quotes": quote_response["quotes"],
            "candles": candle_responses,
            "missing": quote_response["missing"],
            "updated_at": utcnow().isoformat(),
        }
```

- [ ] **Step 3: Wire endpoint**

Add to `api/routers/algo_workers.py`:

```python
@router.post("/worker/market/snapshot")
async def get_worker_market_snapshot(request: Request, payload: WorkerMarketSnapshotRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_market_snapshot(payload)
```

- [ ] **Step 4: Run worker API tests**

```bash
python3 -m pytest tests/test_algo_worker_api.py -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add api/worker_market_data.py api/routers/algo_workers.py tests/test_algo_worker_api.py
git commit -m "feat: add worker market snapshot bundle"
```

---

## Task 6: Add SDK methods and SSE parsing coverage

**Files:**
- Modify: `sdk/python/kite_algo_worker/client.py`
- Test: `tests/test_worker_sdk.py`

- [ ] **Step 1: Write failing SDK tests**

Add tests to `tests/test_worker_sdk.py`:

```python
def test_get_quotes_uses_worker_market_endpoint(captured_requests):
    client().get_quotes(["NSE:INFY", 408065], mode="quote")

    request = captured_requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "http://localhost:8000/api/algo-workers/worker/market/quotes"
    assert request["json"] == {"symbols": ["NSE:INFY"], "instrument_tokens": [408065], "mode": "quote"}


def test_stream_ticks_parses_sse_events(monkeypatch):
    response = FakeResponse(lines=["event: snapshot", "data: {\"ticks\": [], \"missing\": []}", "event: ticks", "data: {\"ticks\": [{\"instrument_token\": 408065}]}" ])

    def fake_request(self, method, url, **kwargs):
        assert method == "GET"
        assert kwargs["stream"] is True
        assert kwargs["params"] == {"symbols": "NSE:INFY", "tokens": "408065", "mode": "quote"}
        return response

    monkeypatch.setattr("requests.Session.request", fake_request)

    events = list(client().stream_ticks(["NSE:INFY", 408065], mode="quote"))

    assert events[0] == {"ticks": [], "missing": []}
    assert events[1] == {"ticks": [{"instrument_token": 408065}]}
    assert response.closed is True
```

- [ ] **Step 2: Add a shared SSE iterator helper**

Refactor `stream_run_pnl` in `sdk/python/kite_algo_worker/client.py` to reuse a private helper:

```python
    def _stream_sse(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Iterator[JsonDict]:
        response = self.session.request(
            method,
            self._url(path),
            timeout=(self.config.timeout, None),
            stream=True,
            params=dict(params or {}),
        )
        if not 200 <= response.status_code < 300:
            try:
                self._raise_response_error(response, method, path)
            finally:
                response.close()

        def _events() -> Iterator[JsonDict]:
            current_event = "message"
            try:
                for line in response.iter_lines(decode_unicode=True):
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip() or "message"
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line.split(":", 1)[1].strip()
                    if not payload:
                        continue
                    decoded = json.loads(payload)
                    if current_event == "error":
                        raise KiteAlgoWorkerError(
                            f"Worker API stream error at {path}: {decoded.get('detail') if isinstance(decoded, dict) else decoded}",
                            status_code=0,
                            response_body=decoded,
                        )
                    if current_event == "end":
                        break
                    yield decoded
                    current_event = "message"
            finally:
                response.close()

        return _events()
```

- [ ] **Step 3: Add market SDK methods**

Add methods to `KiteAlgoWorkerClient`:

```python
    def resolve_ticker(self, symbol: str) -> JsonDict:
        return self._request("GET", "/worker/market/instruments/resolve", params={"symbol": symbol})

    def resolve_tickers(self, instruments: Iterable[str | int]) -> JsonDict:
        symbols, tokens = self._split_instruments(instruments)
        return self._request("POST", "/worker/market/instruments/resolve", json={"symbols": symbols, "instrument_tokens": tokens})

    def search_tickers(self, query: str, *, exchange: Optional[str] = None, limit: int = 20) -> JsonDict:
        params: Dict[str, Any] = {"query": query, "limit": limit}
        if exchange:
            params["exchange"] = exchange
        return self._request("GET", "/worker/market/instruments/search", params=params)

    def get_quotes(self, instruments: Iterable[str | int], *, mode: str = "quote") -> JsonDict:
        symbols, tokens = self._split_instruments(instruments)
        return self._request("POST", "/worker/market/quotes", json={"symbols": symbols, "instrument_tokens": tokens, "mode": mode})

    def stream_ticks(self, instruments: Iterable[str | int], *, mode: str = "quote") -> Iterator[JsonDict]:
        symbols, tokens = self._split_instruments(instruments)
        return self._stream_sse("GET", "/worker/market/ticks/stream", params={"symbols": ",".join(symbols), "tokens": ",".join(str(token) for token in tokens), "mode": mode})

    def get_candles(self, instrument: str | int, *, interval: str, lookback: int = 50) -> JsonDict:
        params: Dict[str, Any] = {"interval": interval, "lookback": lookback}
        if isinstance(instrument, int):
            params["instrument_token"] = instrument
        else:
            params["symbol"] = instrument
        return self._request("GET", "/worker/market/candles", params=params)

    def get_current_candle(self, instrument: str | int, *, interval: str) -> JsonDict:
        return self.get_candles(instrument, interval=interval, lookback=1).get("current")

    def stream_candles(self, instrument: str | int, *, interval: str) -> Iterator[JsonDict]:
        params: Dict[str, Any] = {"interval": interval}
        if isinstance(instrument, int):
            params["instrument_token"] = instrument
        else:
            params["symbol"] = instrument
        return self._stream_sse("GET", "/worker/market/candles/stream", params=params)

    def get_market_snapshot(self, *, symbols: Optional[List[str]] = None, instrument_tokens: Optional[List[int]] = None, candles: Optional[List[Mapping[str, Any]]] = None, mode: str = "quote") -> JsonDict:
        return self._request("POST", "/worker/market/snapshot", json={"symbols": symbols or [], "instrument_tokens": instrument_tokens or [], "candles": list(candles or []), "mode": mode})

    @staticmethod
    def _split_instruments(instruments: Iterable[str | int]) -> tuple[List[str], List[int]]:
        symbols: List[str] = []
        tokens: List[int] = []
        for item in instruments:
            if isinstance(item, int) or str(item).isdigit():
                tokens.append(int(item))
            else:
                symbols.append(str(item))
        return symbols, tokens
```

Update `stream_run_pnl` to return:

```python
        return self._stream_sse("GET", f"/worker/runs/{strategy_run_id}/pnl/stream", params={"interval_seconds": interval_seconds})
```

- [ ] **Step 4: Run SDK tests**

```bash
python3 -m pytest tests/test_worker_sdk.py -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add sdk/python/kite_algo_worker/client.py tests/test_worker_sdk.py
git commit -m "feat: add worker market data SDK helpers"
```

---

## Task 7: Document worker market data, restart behavior, and option-layer boundary

**Files:**
- Modify: `docs/algo-worker-development-guide.md`
- Modify: `sdk/python/README.md`
- Modify: `features-doc/algo-worker-sdk/progress.md`
- Modify: `documents/kite-backend-progress.md`
- Optional create: `sdk/python/examples/realtime_market_data_worker.py`

- [ ] **Step 1: Add SDK README section**

Add this section to `sdk/python/README.md`:

```markdown
## Runtime-backed market data

The SDK exposes worker-safe market-data helpers backed by Kite Algo's Go market-runtime. Workers do not connect to broker websockets directly.

```python
instrument = client.resolve_ticker("NSE:INFY")
quotes = client.get_quotes(["NSE:INFY"], mode="quote")
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=50)

for event in client.stream_ticks(["NSE:INFY"], mode="quote"):
    for tick in event.get("ticks", []):
        print(tick["last_price"])
```

If a worker stops, strategy decisions stop. Existing broker orders and positions remain with broker/backend accounting. Restart workers with the same `strategy_run_id`, call `get_run`, `get_run_pnl`, rebuild local indicator state from candles, and reconnect streams.

Options-specific helpers are intentionally deferred to a later `kite_algo_worker.options` layer inside the same SDK package.
```

- [ ] **Step 2: Update development guide lifecycle**

In `docs/algo-worker-development-guide.md`, add market data to the endpoint table:

```markdown
| `resolve_ticker(...)` / `search_tickers(...)` | `/api/algo-workers/worker/market/instruments/*` |
| `get_quotes(...)` / `stream_ticks(...)` | `/api/algo-workers/worker/market/quotes`, `/api/algo-workers/worker/market/ticks/stream` |
| `get_candles(...)` / `stream_candles(...)` | `/api/algo-workers/worker/market/candles*` |
| `get_market_snapshot(...)` | `POST /api/algo-workers/worker/market/snapshot` |
```

Add a section titled `Worker disconnects and restart recovery` with:

```markdown
External workers own strategy decisions. If a worker goes offline, new decisions stop. The backend still owns accounting, fills, live positions, grouped P&L, and grouped exits when requested.

Production workers should restart with the same `strategy_run_id`, call `get_run(...)`, call `get_run_pnl(...)`, rebuild indicators from `get_candles(...)`, reconnect market streams, and then resume decisions.

Do not assume the backend will auto-exit positions on worker disconnect unless a future explicit failover policy is configured.
```

- [ ] **Step 3: Add example worker**

Create `sdk/python/examples/realtime_market_data_worker.py`:

```python
import os

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, equity_market_order


def main() -> None:
    client = KiteAlgoWorkerClient(AlgoWorkerConfig(
        base_url=os.getenv("KITE_ALGO_API_BASE", "http://localhost:8000"),
        token=os.environ["KITE_ALGO_WORKER_TOKEN"],
    ))
    symbol = os.getenv("KITE_ALGO_SYMBOL", "NSE:INFY")
    run_id = os.getenv("KITE_ALGO_RUN_ID", "run_realtime_market_data_demo")
    mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run")

    client.create_run(
        strategy_run_id=run_id,
        template_id="realtime-market-data-demo",
        account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
        execution_mode=mode,
        metadata={"strategy_family": "indicator_strategy", "strategy_name": "Realtime Market Data Demo"},
    )

    candles = client.get_candles(symbol, interval="5minute", lookback=20)
    print(f"loaded {len(candles.get('candles', []))} candles for {symbol}")

    for event in client.stream_ticks([symbol], mode="quote"):
        ticks = event.get("ticks", [])
        if not ticks:
            continue
        last_price = ticks[0].get("last_price")
        print(f"{symbol} last_price={last_price}")
        if mode == "dry_run":
            break
        client.place_order(run_id, equity_market_order(symbol.split(":", 1)[1], "BUY", 1), f"{run_id}:demo-entry:001")
        break


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Update progress docs**

Update `features-doc/algo-worker-sdk/progress.md` with:

```markdown
- Added worker market-data design/implementation for generic primitives: ticker resolution, quote snapshots, tick streams, candle snapshots, candle streams, and market snapshot bundles.
- Options-specific chain/strike/expiry helpers remain intentionally deferred to a later namespaced option worker layer in the same SDK package.
```

Update `documents/kite-backend-progress.md` with a short bullet under “Newly implemented in current branch” after implementation is complete.

- [ ] **Step 5: Run docs/example syntax check**

```bash
python3 - <<'PY'
import ast
from pathlib import Path
for path in [Path('sdk/python/examples/realtime_market_data_worker.py')]:
    ast.parse(path.read_text())
print('ok')
PY
```

Expected: `ok`.

- [ ] **Step 6: Commit Task 7**

```bash
git add docs/algo-worker-development-guide.md sdk/python/README.md sdk/python/examples/realtime_market_data_worker.py
git add -f features-doc/algo-worker-sdk/progress.md documents/kite-backend-progress.md
git commit -m "docs: document worker market data"
```

---

## Task 8: Add worker disconnect visibility hardening

**Files:**
- Modify: `api/routers/algo_workers.py`
- Modify: `docs/algo-worker-development-guide.md`
- Test: `tests/test_algo_worker_api.py`

- [ ] **Step 1: Write failing heartbeat stale-state test**

Add a test that verifies heartbeat still accepts strategy run context in metrics and stores it through existing repository heartbeat behavior. If the repository does not persist heartbeat state per run yet, document the current limitation and add only docs in this task.

Target request shape for workers:

```python
await heartbeat_worker(request, WorkerHeartbeatRequest(
    worker_id="worker-process-1",
    status="healthy",
    metrics={"strategy_run_id": "run-1", "market_streams": 1},
))
```

- [ ] **Step 2: Keep v1 behavior explicit**

If adding durable heartbeat stale detection requires schema changes, do not include that schema in this market-data v1. Instead, add a clear guide section and create a follow-up note in `features-doc/algo-worker-sdk/progress.md`:

```markdown
Known gap: worker heartbeat stale detection is token-level today. A later safety feature should persist per-run heartbeat timestamps and optional failover policy (`observe_only`, `cancel_open_orders`, `exit_positions`, `backend_stop_rules`).
```

- [ ] **Step 3: Commit Task 8**

```bash
git add api/routers/algo_workers.py tests/test_algo_worker_api.py docs/algo-worker-development-guide.md
git add -f features-doc/algo-worker-sdk/progress.md
git commit -m "docs: clarify worker disconnect behavior"
```

If no code changes are needed after inspection, commit only docs/progress files with the same message.

---

## Task 9: Final verification and release notes

**Files:**
- No new source files unless fixing issues found by tests.

- [ ] **Step 1: Run focused worker suites**

```bash
python3 -m pytest tests/test_algo_worker_api.py tests/test_worker_sdk.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run related live regression suites**

```bash
python3 -m pytest tests/test_live_order_attribution_gate.py tests/test_live_journal_projector.py tests/test_live_external_exit_recovery.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run syntax check for touched Python files**

```bash
python3 - <<'PY'
import ast
from pathlib import Path
paths = [
    'api/worker_market_data.py',
    'api/routers/algo_workers.py',
    'broker_api/instruments_repository.py',
    'sdk/python/kite_algo_worker/client.py',
    'sdk/python/examples/realtime_market_data_worker.py',
]
for raw in paths:
    path = Path(raw)
    ast.parse(path.read_text())
print('ok')
PY
```

Expected: `ok`.

- [ ] **Step 4: Review git diff for unrelated files**

```bash
git status --short
git diff --stat
```

Expected: only worker market-data files and docs are modified. Existing unrelated untracked files under `.codex`, older `docs/superpowers/*`, and `journal/` remain untouched unless intentionally handled separately.

- [ ] **Step 5: Final commit if any verification fixes were required**

```bash
git add <fixed-files>
git commit -m "fix: harden worker market data"
```

Skip this commit if no files changed during final verification.

---

## Implementation delegation guidance

This plan is suitable for implementer subagents. Recommended delegation:

1. Implementer A: Tasks 1-2, instrument resolution and quote snapshots.
2. Implementer B: Task 3, tick streaming.
3. Implementer C: Tasks 4-5, candles and snapshot bundle.
4. Implementer D: Task 6, SDK helpers and parser refactor.
5. Main agent: Tasks 7-9, docs, disconnect behavior, final verification, review.

Use reviewer after each implementation chunk that touches streaming or runtime owner cleanup.

## Self-review checklist

- Spec coverage: instrument resolution, quotes, tick stream, candles, candle stream, snapshot bundle, docs, option deferral, and worker disconnect behavior are all mapped to tasks.
- Placeholder scan: no `TBD` or unbounded “handle edge cases” steps remain; every task has specific files, code shape, commands, and expected results.
- Type consistency: SDK method names and endpoint paths match the design spec and router plan.
- Scope check: option-chain helpers are explicitly deferred; no option-specific implementation is included in v1.
