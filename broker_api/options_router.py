import asyncio
import logging
from typing import Any, Dict, List

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from broker_api.instruments_repository import InstrumentsRepository
from broker_api.options_sessions import OptionsSessionManager
from broker_api.websocket_manager import WebSocketManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()


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
    return request.app.state.options_session_manager


class SessionRequestItem(BaseModel):
    underlying: str
    window: int = 12
    cadence_sec: int = 5


class SessionsRequest(BaseModel):
    items: List[SessionRequestItem]
    replace: bool = False


@router.post("/options/sessions")
async def manage_sessions(
    payload: SessionsRequest,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Starts, updates, or replaces options sessions.
    """
    try:
        await manager.start_sessions(
            [item.dict() for item in payload.items], payload.replace
        )
        return manager.get_watchlist()
    except Exception as e:
        logger.error(f"Error managing sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/options/session/{underlying}")
async def get_session_snapshot(
    underlying: str,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Returns the latest snapshot for a given underlying's session.
    """
    snapshot = manager.get_snapshot(underlying)
    if not snapshot:
        raise HTTPException(
            status_code=404, detail=f"No active session for underlying '{underlying}'"
        )
    return snapshot


@router.delete("options/session/{underlying}")
async def stop_session(
    underlying: str,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Stops an options session for a given underlying.
    """
    await manager.stop_session(underlying)
    return {"status": "stopped", "underlying": underlying}


@router.get("/options/chain/{underlying_symbol}")
async def get_option_chain(
    underlying_symbol: str,
    manager: OptionsSessionManager = Depends(get_options_session_manager),
):
    """
    Thin alias to get a session snapshot, for backward compatibility.
    """
    normalized_underlying, _ = manager.instrument_repo.normalize_underlying_symbol(
        underlying_symbol
    )
    snapshot = manager.get_snapshot(normalized_underlying)
    if not snapshot:
        # Optionally, could start a session here on-demand
        raise HTTPException(
            status_code=404,
            detail=f"No active session for underlying '{normalized_underlying}'. Use POST /api/options/sessions to start one.",
        )
    return snapshot


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