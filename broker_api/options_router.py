import asyncio
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
import json
from fastapi.responses import StreamingResponse

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from broker_api.instruments_repository import InstrumentsRepository
from broker_api.options_sessions import OptionsSessionManager
from broker_api.websocket_manager import WebSocketManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()


# Pydantic Response Models
class V1Config:
    extra = "allow"


class WatchlistEntryModel(BaseModel):
    underlying: str
    is_running: bool
    desired_tokens: int

    class Config(V1Config):
        pass


class StopResponseModel(BaseModel):
    status: str
    underlying: str

    class Config(V1Config):
        pass


class ErrorResponseModel(BaseModel):
    code: str
    message: str


class OptionGreeksModel(BaseModel):
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None

    class Config(V1Config):
        pass


class OptionInstrumentDataModel(OptionGreeksModel):
    token: int
    tsym: str
    ltp: Optional[float] = None
    iv: Optional[float] = None
    oi: Optional[float] = None
    updated_at: Optional[datetime] = None
    stale_age_sec: Optional[float] = None

    class Config(V1Config):
        pass


class OptionChainRowModel(BaseModel):
    strike: float
    CE: Optional[OptionInstrumentDataModel] = None
    PE: Optional[OptionInstrumentDataModel] = None

    class Config(V1Config):
        pass


class PerExpiryDataModel(BaseModel):
    forward: Optional[float] = None
    sigma_expiry: Optional[float] = None
    atm_strike: Optional[float] = None
    strikes: List[float]
    rows: List[OptionChainRowModel]

    class Config(V1Config):
        pass


class OptionChainSnapshotModel(BaseModel):
    underlying: str
    spot_token: int
    spot_ltp: Optional[float] = None
    cadence_sec: int
    expiries: List[date]
    per_expiry: Dict[str, PerExpiryDataModel]
    desired_token_count: int
    updated_at: datetime

    class Config(V1Config):
        pass


def get_options_session_manager(request: Request) -> OptionsSessionManager:
    """
    Dependency to get or create the OptionsSessionManager singleton.
    """
    if not hasattr(request.app.state, "options_session_manager"):
        logger.info("Initializing OptionsSessionManager...")
        ws_manager = request.app.state.ws_manager
        instrument_repo = InstrumentsRepository(db=next(get_db()))
        request.app.state.options_session_manager = OptionsSessionManager(
            ws_manager, instrument_repo
        )
        ws_manager.options_session_manager = request.app.state.options_session_manager
    return request.app.state.options_session_manager


class SessionRequestItem(BaseModel):
    underlying: str
    window: int = Field(
        12, ge=1, description="Number of 5s bars (or unit) to maintain; must be >= 1"
    )
    cadence_sec: int = Field(
        5, ge=1, description="Update cadence in seconds; must be >= 1"
    )


class SessionsRequest(BaseModel):
    items: List[SessionRequestItem]
    replace: bool = False


@router.post(
    "/options/sessions",
    response_model=List[WatchlistEntryModel],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "underlying": "NIFTY",
                            "is_running": True,
                            "desired_tokens": 242,
                        },
                        {
                            "underlying": "BANKNIFTY",
                            "is_running": True,
                            "desired_tokens": 198,
                        },
                    ]
                }
            },
        }
    },
)
async def manage_sessions(
    payload: SessionsRequest,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Starts, updates, or replaces options sessions.

    Returns a stable response model: `List[WatchlistEntryModel]`.
    """
    try:
        await manager.start_sessions(
            [item.dict() for item in payload.items], payload.replace
        )
        return manager.get_watchlist()
    except Exception as e:
        logger.error(f"Error managing sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/options/session/{underlying}",
    response_model=OptionChainSnapshotModel,
    response_model_exclude_none=True,
    responses={
        404: {"model": ErrorResponseModel},
        200: {
            "description": "A full option chain snapshot for the underlying.",
            "content": {
                "application/json": {
                    "example": {
                        "underlying": "NIFTY",
                        "spot_token": 256257,
                        "spot_ltp": 22500.5,
                        "cadence_sec": 5,
                        "expiries": ["2025-10-09", "2025-10-16"],
                        "per_expiry": {
                            "2025-10-09": {
                                "forward": 22505.1,
                                "sigma_expiry": 0.145,
                                "atm_strike": 22500,
                                "strikes": [22400, 22500, 22600],
                                "rows": [
                                    {
                                        "strike": 22500,
                                        "CE": {
                                            "token": 12345,
                                            "tsym": "NIFTY25OCT22500CE",
                                            "ltp": 150.0,
                                            "iv": 0.15,
                                            "delta": 0.5,
                                            "gamma": 0.002,
                                            "theta": -5.2,
                                            "vega": 30.1,
                                        },
                                        "PE": {
                                            "token": 12346,
                                            "tsym": "NIFTY25OCT22500PE",
                                            "ltp": 145.0,
                                            "iv": 0.14,
                                            "delta": -0.5,
                                        },
                                    }
                                ],
                            }
                        },
                        "desired_token_count": 242,
                        "updated_at": "2025-10-02T12:00:00Z",
                    }
                }
            },
        },
    },
)
async def get_session_snapshot(
    underlying: str,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Returns the latest snapshot for a given underlying's session.

    - Response model: `OptionChainSnapshotModel`
    - 404 detail shape: `{"code":"OPTION_SESSION_NOT_FOUND","message":"..."}`
    """
    normalized_underlying, _ = manager.instrument_repo.normalize_underlying_symbol(
        underlying
    )
    snapshot = manager.get_snapshot(normalized_underlying)
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "OPTION_SESSION_NOT_FOUND",
                "message": f"No active session for underlying '{normalized_underlying}'",
            },
        )
    return snapshot


@router.delete(
    "/options/session/{underlying}",
    response_model=StopResponseModel,
    responses={
        200: {
            "description": "Confirmation of session stop.",
            "content": {
                "application/json": {
                    "example": {"status": "stopped", "underlying": "NIFTY"}
                }
            },
        }
    },
)
async def stop_session(
    underlying: str,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Stops an options session for a given underlying.

    - Response model: `StopResponseModel`
    - Returns `{"status":"stopped","underlying":"<normalized>"}`
    """
    normalized_underlying, _ = manager.instrument_repo.normalize_underlying_symbol(
        underlying
    )
    await manager.stop_session(normalized_underlying)
    return {"status": "stopped", "underlying": normalized_underlying}


@router.get(
    "/options/chain/{underlying_symbol}",
    response_model=OptionChainSnapshotModel,
    response_model_exclude_none=True,
    responses={
        404: {"model": ErrorResponseModel},
        200: {
            "description": "A full option chain snapshot for the underlying.",
            "content": {
                "application/json": {
                    "example": {
                        "underlying": "NIFTY",
                        "spot_token": 256257,
                        "spot_ltp": 22500.5,
                        "cadence_sec": 5,
                        "expiries": ["2025-10-09", "2025-10-16"],
                        "per_expiry": {
                            "2025-10-09": {
                                "forward": 22505.1,
                                "sigma_expiry": 0.145,
                                "atm_strike": 22500,
                                "strikes": [22400, 22500, 22600],
                                "rows": [
                                    {
                                        "strike": 22500,
                                        "CE": {
                                            "token": 12345,
                                            "tsym": "NIFTY25OCT22500CE",
                                            "ltp": 150.0,
                                            "iv": 0.15,
                                            "delta": 0.5,
                                        },
                                        "PE": {
                                            "token": 12346,
                                            "tsym": "NIFTY25OCT22500PE",
                                            "ltp": 145.0,
                                            "iv": 0.14,
                                            "delta": -0.5,
                                        },
                                    }
                                ],
                            }
                        },
                        "desired_token_count": 242,
                        "updated_at": "2025-10-02T12:00:00Z",
                    }
                }
            },
        },
    },
)
async def get_option_chain(
    underlying_symbol: str,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Thin alias to get a session snapshot, for backward compatibility.

    - Response model: `OptionChainSnapshotModel`
    - 404 detail shape: `{"code":"OPTION_SESSION_NOT_FOUND","message":"..."}`
    """
    normalized_underlying, _ = manager.instrument_repo.normalize_underlying_symbol(
        underlying_symbol
    )
    snapshot = manager.get_snapshot(normalized_underlying)
    if not snapshot:
        # Optionally, could start a session here on-demand
        raise HTTPException(
            status_code=404,
            detail={
                "code": "OPTION_SESSION_NOT_FOUND",
                "message": f"No active session for underlying '{normalized_underlying}'",
            },
        )
    return snapshot


@router.get(
    "/options/sessions",
    response_model=List[WatchlistEntryModel],
    responses={
        200: {
            "description": "A list of active or known option sessions.",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "underlying": "NIFTY",
                            "is_running": True,
                            "desired_tokens": 242,
                        },
                        {
                            "underlying": "BANKNIFTY",
                            "is_running": False,
                            "desired_tokens": 0,
                        },
                    ]
                }
            },
        }
    },
)
async def get_sessions(manager: OptionsSessionManager = Depends(get_options_session_manager)):
    """
    Returns the current watchlist of active options sessions.

    - Response model: `List[WatchlistEntryModel]`
    - This is a read-only view of the active sessions.
    """
    return manager.get_watchlist()


@router.websocket("/ws/options/session/{underlying}")
async def websocket_options_session(
    websocket: WebSocket,
    underlying: str,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    WebSocket endpoint to stream 5s updates for an options session.
    """
    await websocket.accept()
    
    normalized_underlying, _ = manager.instrument_repo.normalize_underlying_symbol(
        underlying
    )

    # Check if session exists
    if normalized_underlying not in manager.sessions:
        await websocket.close(code=4004, reason=f"No active session for {normalized_underlying}")
        return

    queue = await manager.register_client(normalized_underlying)
    
    # Send initial snapshot
    initial_snapshot = manager.get_snapshot(normalized_underlying)
    if initial_snapshot:
        await websocket.send_json(initial_snapshot)

    try:
        while True:
            data = await queue.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        logger.info(f"Client disconnected from options session {normalized_underlying}")
    finally:
        manager.deregister_client(normalized_underlying, queue)

@router.get("/sse/options/session/{symbol}")
async def sse_options_session(
    symbol: str,
    request: Request,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Server-Sent Events endpoint to stream 5s updates for an options session.
    """
    normalized_symbol, _ = manager.instrument_repo.normalize_underlying_symbol(symbol)

    # Before starting the generator, check if the session exists.
    # If not, we can't stream anything.
    if normalized_symbol not in manager.sessions:
        # A standard response is better here than an empty stream, but for simplicity
        # and to avoid breaking the EventSource contract, we'll send a specific
        # error event and then close the connection.
        async def error_generator():
            error_payload = {
                "type": "error",
                "code": "OPTION_SESSION_NOT_FOUND",
                "message": f"No active session for underlying '{normalized_symbol}'. Please start one.",
            }
            yield f"data: {json.dumps(error_payload)}\n\n"
        
        return StreamingResponse(
            error_generator(),
            media_type="text/event-stream",
            status_code=404,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "close",
            },
        )

    async def event_generator():
        queue = await manager.register_client(normalized_symbol)
        
        # Send initial snapshot if available
        initial_snapshot = manager.get_snapshot(normalized_symbol)
        if initial_snapshot:
            yield f"data: {json.dumps(initial_snapshot)}\n\n"

        last_heartbeat = asyncio.get_event_loop().time()
        
        try:
            while True:
                try:
                    # Wait for a new item from the queue with a timeout
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    # No data, send a heartbeat to keep the connection alive
                    yield ": keep-alive\n\n"
                
                # Also handle client disconnect
                if await request.is_disconnected():
                    logger.info(f"Client disconnected from SSE stream for {normalized_symbol}")
                    break
        except asyncio.CancelledError:
             logger.info(f"SSE stream for {normalized_symbol} was cancelled.")
        finally:
            manager.deregister_client(normalized_symbol, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no", # For Nginx buffering
        },
    )