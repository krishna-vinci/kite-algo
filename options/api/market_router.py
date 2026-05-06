from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from broker_api.instruments_repository import InstrumentsRepository
from broker_api.options_sessions import OptionsSessionManager
from database import SessionLocal
from options.market.service import OptionsMarketService

router = APIRouter(prefix="/api/options", tags=["Options"])


class OptionsSessionItemPayload(BaseModel):
    underlying: str = Field(min_length=1)
    window: int = Field(default=12, ge=1, le=100)
    cadence_sec: int = Field(default=5, ge=1, le=3600)


class OptionsSessionsStartPayload(BaseModel):
    replace: bool = False
    items: list[OptionsSessionItemPayload]


def get_options_session_manager(request: Request):
    if not hasattr(request.app.state, "options_session_manager"):
        market_data_runtime = getattr(request.app.state, "market_data_runtime", None)
        if market_data_runtime is None:
            raise HTTPException(status_code=503, detail="Market runtime not available")
        instrument_repo = InstrumentsRepository(db=SessionLocal)
        request.app.state.options_session_manager = OptionsSessionManager(
            market_data_runtime,
            instrument_repo,
        )
    return request.app.state.options_session_manager


def _raw_session_snapshot(manager: OptionsSessionManager, underlying: str) -> dict:
    normalized, _ = manager.instrument_repo.normalize_underlying_symbol(underlying)
    snapshot = manager.get_snapshot(normalized)
    if not snapshot:
        raise HTTPException(status_code=404, detail={"code": "OPTION_SESSION_NOT_FOUND", "message": "No active option session for the requested underlying"})
    return OptionsMarketService(manager)._json_safe(snapshot)


@router.post("/sessions")
async def start_option_sessions(
    payload: OptionsSessionsStartPayload,
    manager=Depends(get_options_session_manager),
):
    await manager.start_sessions(
        [item.model_dump() for item in payload.items],
        replace=payload.replace,
    )
    return {"status": "ok", "watchlist": manager.get_watchlist()}


@router.get("/session/{underlying}")
async def get_option_session_legacy(
    underlying: str,
    manager=Depends(get_options_session_manager),
):
    return _raw_session_snapshot(manager, underlying)


@router.get("/underlyings/{underlying}/session")
async def get_option_session(
    underlying: str,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_session(underlying)


@router.get("/underlyings/{underlying}/stream")
async def stream_option_session(
    underlying: str,
    manager=Depends(get_options_session_manager),
):
    initial_payload = OptionsMarketService(manager).get_session(underlying)
    normalized = str(initial_payload.get("underlying") or underlying).upper()
    queue = await manager.register_client(normalized)

    async def event_stream():
        try:
            yield f"data: {json.dumps(initial_payload)}\n\n"
            while True:
                await queue.get()
                payload = OptionsMarketService(manager).get_session(normalized)
                yield f"data: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            manager.deregister_client(normalized, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/underlyings/{underlying}/expiries")
async def list_option_expiries(
    underlying: str,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).list_expiries(underlying)


@router.get("/underlyings/{underlying}/chain")
async def get_option_chain(
    underlying: str,
    expiry: str | None = None,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_chain(underlying, expiry)


@router.get("/underlyings/{underlying}/mini-chain")
async def get_option_mini_chain(
    underlying: str,
    expiry: str | None = None,
    window: int = Query(default=2, ge=1, le=20),
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_mini_chain(underlying, expiry, window)


@router.get("/underlyings/{underlying}/greeks")
async def get_option_greeks(
    underlying: str,
    expiry: str | None = None,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_greeks(underlying, expiry)


@router.post("/underlyings/{underlying}/selection/resolve")
async def resolve_option_selection(
    underlying: str,
    payload: dict,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).resolve_selection(underlying, payload)


@router.get("/underlyings/{underlying}/analytics/pcr")
async def get_option_pcr(
    underlying: str,
    expiry: str | None = None,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_pcr(underlying, expiry)


@router.get("/underlyings/{underlying}/analytics/max-pain")
async def get_option_max_pain(
    underlying: str,
    expiry: str | None = None,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_max_pain(underlying, expiry)
