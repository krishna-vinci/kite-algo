from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from options.market.service import OptionsMarketService

router = APIRouter(prefix="/api/options", tags=["Options"])


def get_options_session_manager(request: Request):
    from broker_api.options_router import get_options_session_manager

    return get_options_session_manager(request)


@router.get("/underlyings/{underlying}/session")
async def get_option_session(
    underlying: str,
    manager=Depends(get_options_session_manager),
):
    return OptionsMarketService(manager).get_session(underlying)


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
