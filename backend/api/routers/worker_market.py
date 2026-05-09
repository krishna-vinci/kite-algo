from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from backend.api.services.market_data import WorkerInstrumentResolveRequest, WorkerMarketSnapshotRequest, WorkerQuoteRequest
from backend.api.routers.worker_shared import *

router = APIRouter(prefix='/algo-workers', tags=['Algo Workers'])

async def resolve_worker_market_ticker(request: Request, symbol: str):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).resolve_ticker(symbol)

async def search_worker_market_tickers(request: Request, query: str, exchange: Optional[str] = None, limit: int = Query(20, ge=1, le=50)):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).search_tickers(query, exchange=exchange, limit=limit)

async def resolve_worker_market_tickers(request: Request, payload: WorkerInstrumentResolveRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).resolve_many(symbols=payload.symbols, instrument_tokens=payload.instrument_tokens)

async def get_worker_market_quotes(request: Request, payload: WorkerQuoteRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_quotes(payload)

async def stream_worker_market_ticks(request: Request, symbols: Optional[str] = None, tokens: Optional[str] = None, mode: str = "quote"):
    token = await require_worker_token(request)
    _require_action(token, "market:stream")
    parsed_symbols = _parse_csv_values(symbols)
    parsed_tokens = _parse_csv_int_values(tokens, field_name="tokens")
    return StreamingResponse(
        _market_data_service(request).stream_ticks(
            request,
            token,
            symbols=parsed_symbols,
            instrument_tokens=parsed_tokens,
            mode=mode,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

async def get_worker_market_candles(
    request: Request,
    symbol: Optional[str] = None,
    instrument_token: Optional[int] = None,
    interval: str = "5minute",
    lookback: int = Query(50, ge=1, le=500),
):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_candles(
        symbol=symbol,
        instrument_token=instrument_token,
        interval=interval,
        lookback=lookback,
    )

async def get_worker_market_history(
    request: Request,
    background_tasks: BackgroundTasks,
    symbol: Optional[str] = None,
    instrument_token: Optional[int] = None,
    timeframe: str = "day",
    from_ts: Optional[datetime] = Query(None, alias="from"),
    to_ts: Optional[datetime] = Query(None, alias="to"),
    ingest: bool = True,
    passthrough: bool = False,
):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_historical_candles(
        symbol=symbol,
        instrument_token=instrument_token,
        timeframe=timeframe,
        from_date=from_ts,
        to_date=to_ts,
        ingest=ingest,
        passthrough=passthrough,
        background_tasks=background_tasks,
    )

async def stream_worker_market_candles(
    request: Request,
    symbol: Optional[str] = None,
    instrument_token: Optional[int] = None,
    interval: str = "5minute",
):
    token = await require_worker_token(request)
    _require_action(token, "market:stream")
    return StreamingResponse(
        _market_data_service(request).stream_candles(
            request,
            symbol=symbol,
            instrument_token=instrument_token,
            interval=interval,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

async def get_worker_market_snapshot(request: Request, payload: WorkerMarketSnapshotRequest):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    return await _market_data_service(request).get_market_snapshot(payload)

async def get_worker_funds(request: Request, mode: str = Query("paper"), account_scope: Optional[str] = None):
    token = await require_worker_token(request)
    _require_action(token, "funds:read")
    normalized_mode = str(mode or "paper").strip().lower()
    _require_v1_mode(normalized_mode)
    if normalized_mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot read funds for this execution mode")
    scope = str(account_scope or token.account_scope or "").strip()
    if not scope:
        raise HTTPException(status_code=400, detail="account_scope is required for worker funds")
    if not _token_allows_account_scope(token, scope):
        raise HTTPException(status_code=403, detail="Worker token cannot read this account scope")
    return await _build_worker_funds_snapshot(request, account_scope=scope, mode=normalized_mode)

async def get_worker_run_funds(request: Request, strategy_run_id: str):
    token = await require_worker_token(request)
    _require_action(token, "funds:read")
    run = await _repo(request).get_run(strategy_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Strategy run not found")
    _assert_run_access(token, run)
    mode = str(run.get("execution_mode") or "paper").lower()
    if mode not in token.allowed_modes:
        raise HTTPException(status_code=403, detail="Worker token cannot read funds for this execution mode")
    return await _build_worker_run_funds_snapshot(request, run)

async def create_worker_gtt_trigger(request: Request, payload: Dict[str, Any]):
    token = await require_worker_token(request)
    _require_worker_gtt_action(token, "gtt:write")
    account_scope = _require_worker_live_account_scope(token)
    from backend.broker_api.orders import PlaceGTTRequest, gtt_service

    request_payload = payload if isinstance(payload, PlaceGTTRequest) else PlaceGTTRequest.model_validate(payload)

    try:
        kite = await _load_live_kite_for_worker_account_scope(account_scope)
        result = await gtt_service.place_gtt(kite, request_payload, _worker_request_correlation_id(request, "algo-worker-gtt-place"))
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    except Exception as exc:
        raise _normalize_worker_gtt_error(exc) from exc

async def list_worker_gtts(request: Request):
    token = await require_worker_token(request)
    _require_worker_gtt_action(token, "gtt:read")
    account_scope = _require_worker_live_account_scope(token)
    from backend.broker_api.orders import gtt_service

    try:
        kite = await _load_live_kite_for_worker_account_scope(account_scope)
        result = await asyncio.to_thread(gtt_service.get_gtts, kite, _worker_request_correlation_id(request, "algo-worker-gtt-list"))
        return [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in list(result or [])]
    except Exception as exc:
        raise _normalize_worker_gtt_error(exc) from exc

async def get_worker_gtt(request: Request, trigger_id: int):
    token = await require_worker_token(request)
    _require_worker_gtt_action(token, "gtt:read")
    account_scope = _require_worker_live_account_scope(token)
    from backend.broker_api.orders import gtt_service

    try:
        kite = await _load_live_kite_for_worker_account_scope(account_scope)
        result = await asyncio.to_thread(gtt_service.get_gtt, kite, int(trigger_id), _worker_request_correlation_id(request, "algo-worker-gtt-get"))
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    except Exception as exc:
        raise _normalize_worker_gtt_error(exc) from exc

async def modify_worker_gtt_trigger(request: Request, trigger_id: int, payload: Dict[str, Any]):
    token = await require_worker_token(request)
    _require_worker_gtt_action(token, "gtt:write")
    account_scope = _require_worker_live_account_scope(token)
    from backend.broker_api.orders import ModifyGTTRequest, gtt_service

    request_payload = payload if isinstance(payload, ModifyGTTRequest) else ModifyGTTRequest.model_validate(payload)

    try:
        kite = await _load_live_kite_for_worker_account_scope(account_scope)
        result = await gtt_service.modify_gtt(kite, int(trigger_id), request_payload, _worker_request_correlation_id(request, "algo-worker-gtt-modify"))
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    except Exception as exc:
        raise _normalize_worker_gtt_error(exc) from exc

async def delete_worker_gtt_trigger(request: Request, trigger_id: int):
    token = await require_worker_token(request)
    _require_worker_gtt_action(token, "gtt:write")
    account_scope = _require_worker_live_account_scope(token)
    from backend.broker_api.orders import gtt_service

    try:
        kite = await _load_live_kite_for_worker_account_scope(account_scope)
        result = await gtt_service.delete_gtt(kite, int(trigger_id), _worker_request_correlation_id(request, "algo-worker-gtt-delete"))
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    except Exception as exc:
        raise _normalize_worker_gtt_error(exc) from exc
