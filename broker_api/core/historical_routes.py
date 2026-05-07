from datetime import date
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from kiteconnect import KiteConnect

from broker_api.broker_api import (
    clear_historical_data as _clear_historical_data,
    fetch_historical_data_initial as _fetch_historical_data_initial,
    update_historical_data as _update_historical_data,
    get_historical_data_progress as _get_historical_data_progress,
)
from broker_api.session.kite_session import get_kite
from broker_api.broker_api import get_psql_conn

router = APIRouter(tags=["Historical"])

@router.post("/clear_historical_data")
def clear_historical_data(conn=Depends(get_psql_conn)):
    return _clear_historical_data(conn)

@router.post("/fetch_historical_data")
async def fetch_historical_data_initial(background_tasks: BackgroundTasks, kite: KiteConnect = Depends(get_kite)):
    return await _fetch_historical_data_initial(background_tasks, kite)

@router.post("/update_historical_data")
async def update_historical_data(background_tasks: BackgroundTasks, kite: KiteConnect = Depends(get_kite), to_date: Optional[date] = Query(None)):
    return await _update_historical_data(background_tasks, kite, to_date)

@router.get("/historical_data_progress")
async def get_historical_data_progress():
    return await _get_historical_data_progress()
