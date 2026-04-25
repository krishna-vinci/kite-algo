from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from sqlalchemy.orm import Session
from kiteconnect import KiteConnect

from .performance_logic import calculate_performance
from .candles_api import get_kite_db
from .broker_api import get_db, PortfolioSnapshot, PortfolioHistory

router = APIRouter(tags=["Performance"])

# --- Existing Performance Logic ---

class IndicesRequest(BaseModel):
    indices: List[str]

@router.post("/performance")
def get_performance_data(request: IndicesRequest, kite: KiteConnect = Depends(get_kite_db)):
    """
    Endpoint to get performance data for a list of indices.
    Uses system KiteConnect instance from database.
    """
    return calculate_performance(request.indices, kite=kite)

# --- New Portfolio Snapshot & History Logic ---

class SnapshotHolding(BaseModel):
    symbol: str
    quantity: int
    average_price: float
    last_price: float
    value: float

class PortfolioSnapshotRequest(BaseModel):
    strategy_name: str
    holdings: List[SnapshotHolding]
    total_value: float
    total_invested: float

class PerformanceMetric(BaseModel):
    timestamp: datetime
    total_value: float
    invested_value: float
    pnl: float
    pnl_percent: float

@router.post("/portfolio/snapshot")
def create_portfolio_snapshot(snapshot: PortfolioSnapshotRequest, db: Session = Depends(get_db)):
    """
    Records a snapshot of the portfolio state and updates history.
    Called by the frontend to save daily/periodic performance.
    """
    try:
        # 1. Create PortfolioHistory entry
        pnl = snapshot.total_value - snapshot.total_invested
        pnl_pct = (pnl / snapshot.total_invested * 100) if snapshot.total_invested != 0 else 0
        
        history_entry = PortfolioHistory(
            strategy_name=snapshot.strategy_name,
            total_capital=snapshot.total_invested,
            total_value=snapshot.total_value,
            profit_loss=pnl,
            percentage_change=pnl_pct,
            timestamp=datetime.utcnow()
        )
        db.add(history_entry)
        
        # 2. Create PortfolioSnapshot entries for individual holdings
        for holding in snapshot.holdings:
            snap = PortfolioSnapshot(
                strategy_name=snapshot.strategy_name,
                symbol=holding.symbol,
                quantity=holding.quantity,
                purchase_price=holding.average_price,
                total_value=holding.value,
                timestamp=datetime.utcnow()
            )
            db.add(snap)
            
        db.commit()
        return {"status": "success", "message": "Portfolio snapshot recorded successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio/performance", response_model=List[PerformanceMetric])
def get_portfolio_performance(strategy: str, db: Session = Depends(get_db)):
    """
    Retrieves historical performance data for a strategy.
    """
    try:
        history = db.query(PortfolioHistory)\
            .filter(PortfolioHistory.strategy_name == strategy)\
            .order_by(PortfolioHistory.timestamp.asc())\
            .all()
            
        return [
            PerformanceMetric(
                timestamp=h.timestamp,
                total_value=float(h.total_value),
                invested_value=float(h.total_capital),
                pnl=float(h.profit_loss),
                pnl_percent=float(h.percentage_change)
            ) for h in history
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
