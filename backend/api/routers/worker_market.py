from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Dict, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from backend.api.schemas.investment_worker import CalendarResponse, IndexSnapshotResponse, IndexStatusResponse, PortfolioSnapshotResponse
from backend.api.services.market_data import WorkerInstrumentResolveRequest, WorkerMarketSnapshotRequest, WorkerQuoteRequest
from backend.api.routers.worker_shared import *

router = APIRouter(prefix='/algo-workers', tags=['Algo Workers'])

def _optional_query_datetime(value: Any) -> Optional[datetime]:
    # FastAPI injects real datetimes during requests, but direct unit calls that
    # omit Query(...) parameters receive the Query object default.
    return value if isinstance(value, datetime) else None

def _require_schema_version(value: int) -> None:
    if value != 1:
        raise HTTPException(status_code=422, detail={"rejection_reason": "UNSUPPORTED_SCHEMA_VERSION", "supported": [1]})

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
    from_date: Optional[datetime] = Query(None, alias="from_date"),
    to_date: Optional[datetime] = Query(None, alias="to_date"),
    ingest: bool = True,
    passthrough: bool = False,
):
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    from_ts_value = _optional_query_datetime(from_ts)
    to_ts_value = _optional_query_datetime(to_ts)
    from_date_value = _optional_query_datetime(from_date)
    to_date_value = _optional_query_datetime(to_date)
    resolved_from = from_ts_value or from_date_value
    resolved_to = to_ts_value or to_date_value
    if from_ts_value is not None and from_date_value is not None and from_ts_value != from_date_value:
        raise HTTPException(status_code=422, detail="Use only one of from or from_date")
    if to_ts_value is not None and to_date_value is not None and to_ts_value != to_date_value:
        raise HTTPException(status_code=422, detail="Use only one of to or to_date")
    response = await _market_data_service(request).get_historical_candles(
        symbol=symbol,
        instrument_token=instrument_token,
        timeframe=timeframe,
        from_date=resolved_from,
        to_date=resolved_to,
        ingest=ingest,
        passthrough=passthrough,
        background_tasks=background_tasks,
    )
    if str(response.get("timeframe") or "").lower() != "day":
        return response
    from backend.app.database import get_db_connection
    from backend.broker_api.market.exchange_calendar import CalendarUnavailable, assess_daily_completeness, get_calendar_sessions
    start_day = resolved_from.date() if resolved_from else datetime.fromisoformat(str(response["from"])).date()
    end_day = resolved_to.date() if resolved_to else datetime.fromisoformat(str(response["to"])).date()
    conn = get_db_connection()
    try:
        calendar = await asyncio.to_thread(get_calendar_sessions, conn, exchange="NSE", segment="CM", from_date=start_day, to_date=end_day)
    except CalendarUnavailable as exc:
        raise HTTPException(status_code=503, detail={"rejection_reason": str(exc)}) from exc
    finally:
        conn.close()
    ingestion = response.get("ingestion") or {}
    response.update(assess_daily_completeness(response.get("candles") or [], calendar, ingestion_status=str(ingestion.get("status") or "unknown")))
    return response

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
    from backend.api.routers.worker_protection import _build_worker_funds_snapshot

    return await _build_worker_funds_snapshot(request, account_scope=scope, mode=normalized_mode)

async def get_worker_account_portfolio(request: Request, account_scope: Optional[str] = None, schema_version: int = Query(1, ge=1)):
    _require_schema_version(schema_version)
    token = await require_worker_token(request)
    _require_action(token, "funds:read")
    scope = str(account_scope or token.account_scope or "").strip()
    if not scope or not _token_allows_account_scope(token, scope):
        raise HTTPException(status_code=403, detail={"rejection_reason": "WORKER_ACCOUNT_SCOPE_NOT_ALLOWED"})
    from backend.broker_api.account.portfolio_snapshot import PortfolioSnapshotUnavailable, build_portfolio_snapshot
    try:
        kite = await _load_live_kite_for_worker_account_scope(scope)
        return await asyncio.to_thread(build_portfolio_snapshot, kite, scope)
    except PortfolioSnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail={"rejection_reason": "PORTFOLIO_SNAPSHOT_UNAVAILABLE", "reason": str(exc)}) from exc

async def get_worker_index_constituents(request: Request, source_list: str, schema_version: int = Query(1, ge=1)):
    _require_schema_version(schema_version)
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    from backend.broker_api.instruments.index_ingestion import get_worker_index_snapshot
    try:
        return await asyncio.to_thread(get_worker_index_snapshot, source_list)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"rejection_reason": str(exc)}) from exc

async def get_worker_index_status(request: Request, source_list: str, schema_version: int = Query(1, ge=1)):
    _require_schema_version(schema_version)
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    from backend.broker_api.instruments.index_ingestion import get_worker_index_status
    try:
        return await asyncio.to_thread(get_worker_index_status, source_list)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

async def get_worker_market_calendar(request: Request, exchange: str = "NSE", segment: str = "CM", from_date: date = Query(..., alias="from"), to_date: date = Query(..., alias="to"), schema_version: int = Query(1, ge=1)):
    _require_schema_version(schema_version)
    token = await require_worker_token(request)
    _require_action(token, "market:read")
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="from must not be after to")
    from backend.app.database import get_db_connection
    from backend.broker_api.market.exchange_calendar import CalendarUnavailable, get_calendar_sessions
    conn = get_db_connection()
    try:
        return await asyncio.to_thread(get_calendar_sessions, conn, exchange=exchange.upper(), segment=segment.upper(), from_date=from_date, to_date=to_date)
    except CalendarUnavailable as exc:
        raise HTTPException(status_code=503, detail={"rejection_reason": str(exc)}) from exc
    finally:
        conn.close()

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
    from backend.api.routers.worker_protection import _build_worker_run_funds_snapshot

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


router.add_api_route("/worker/market/instruments/resolve", resolve_worker_market_ticker, methods=["GET"])
router.add_api_route("/worker/market/instruments/search", search_worker_market_tickers, methods=["GET"])
router.add_api_route("/worker/market/instruments/resolve", resolve_worker_market_tickers, methods=["POST"])
router.add_api_route("/worker/market/quotes", get_worker_market_quotes, methods=["POST"])
router.add_api_route("/worker/market/ticks/stream", stream_worker_market_ticks, methods=["GET"])
router.add_api_route("/worker/market/candles", get_worker_market_candles, methods=["GET"])
router.add_api_route("/worker/market/history", get_worker_market_history, methods=["GET"])
router.add_api_route("/worker/market/candles/stream", stream_worker_market_candles, methods=["GET"])
router.add_api_route("/worker/market/snapshot", get_worker_market_snapshot, methods=["POST"])
router.add_api_route("/worker/funds", get_worker_funds, methods=["GET"])
router.add_api_route("/worker/account/portfolio", get_worker_account_portfolio, methods=["GET"], response_model=PortfolioSnapshotResponse)
router.add_api_route("/worker/market/indices/{source_list}", get_worker_index_constituents, methods=["GET"], response_model=IndexSnapshotResponse)
router.add_api_route("/worker/market/indices/{source_list}/status", get_worker_index_status, methods=["GET"], response_model=IndexStatusResponse)
router.add_api_route("/worker/market/calendar", get_worker_market_calendar, methods=["GET"], response_model=CalendarResponse)
router.add_api_route("/worker/runs/{strategy_run_id}/funds", get_worker_run_funds, methods=["GET"])
router.add_api_route("/worker/gtt/triggers", create_worker_gtt_trigger, methods=["POST"])
router.add_api_route("/worker/gtt/triggers", list_worker_gtts, methods=["GET"])
router.add_api_route("/worker/gtt/triggers/{trigger_id}", get_worker_gtt, methods=["GET"])
router.add_api_route("/worker/gtt/triggers/{trigger_id}", modify_worker_gtt_trigger, methods=["PUT"])
router.add_api_route("/worker/gtt/triggers/{trigger_id}", delete_worker_gtt_trigger, methods=["DELETE"])
