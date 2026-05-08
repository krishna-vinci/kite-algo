from fastapi import APIRouter, Depends, Request, Response, Query
from kiteconnect import KiteConnect
from typing import List

from broker_api.broker_api import (
    InstrumentsRequest, OHLCResponse,
    headless_login as _headless_login,
    logout as _logout,
    profile as _profile,
    holdings as _holdings,
    get_margins as _get_margins,
    get_ltp as _get_ltp,
    get_ohlc as _get_ohlc,
)
from broker_api.session.kite_session import get_kite
from app.database import get_db

router = APIRouter(tags=["Core"] )

@router.post("/login_kite")
def headless_login(request: Request, response: Response, db=Depends(get_db)):
    return _headless_login(request, response, db)

@router.post("/logout_kite")
def logout(response: Response, request: Request, db=Depends(get_db)):
    return _logout(response, request, db)

@router.get("/profile_kite")
def profile(kite: KiteConnect = Depends(get_kite)):
    return _profile(kite)

@router.get("/holdings_kite")
def holdings(kite: KiteConnect = Depends(get_kite)):
    return _holdings(kite)

@router.get("/margins")
def get_margins(kite: KiteConnect = Depends(get_kite)):
    return _get_margins(kite)

@router.post("/ltp")
def get_ltp(request: InstrumentsRequest, kite: KiteConnect = Depends(get_kite)):
    return _get_ltp(request, kite)

@router.get("/quote/ohlc", response_model=OHLCResponse, summary="Get OHLC and LTP for multiple instruments")
def get_ohlc(i: List[str] = Query(...), kite: KiteConnect = Depends(get_kite)):
    return _get_ohlc(i, kite)
