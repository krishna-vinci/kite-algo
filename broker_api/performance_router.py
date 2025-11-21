from fastapi import APIRouter, Depends
from typing import List
from pydantic import BaseModel
from kiteconnect import KiteConnect
from .performance_logic import calculate_performance
from .candles_api import get_kite_db

router = APIRouter()

class IndicesRequest(BaseModel):
    indices: List[str]

@router.post("/performance")
def get_performance_data(request: IndicesRequest, kite: KiteConnect = Depends(get_kite_db)):
    """
    Endpoint to get performance data for a list of indices.
    Uses system KiteConnect instance from database.
    """
    return calculate_performance(request.indices, kite=kite)
