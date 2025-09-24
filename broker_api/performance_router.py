from fastapi import APIRouter
from typing import List
from pydantic import BaseModel
from .performance_logic import calculate_performance

router = APIRouter()

class IndicesRequest(BaseModel):
    indices: List[str]

@router.post("/performance")
def get_performance_data(request: IndicesRequest):
    """
    Endpoint to get performance data for a list of indices.
    """
    return calculate_performance(request.indices)