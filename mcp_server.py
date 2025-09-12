from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Form, Query
from kiteconnect import KiteConnect
from broker_api.broker_api import get_kite

# Create an APIRouter for the MCP tools
mcp_router = APIRouter()

@mcp_router.get("/profile")
def get_profile(kite: KiteConnect = Depends(get_kite)) -> Dict[str, Any]:
    """Fetch user profile."""
    try:
        return kite.profile()
    except Exception as e:
        return {"error": str(e)}

@mcp_router.get("/holdings")
def get_holdings(kite: KiteConnect = Depends(get_kite)) -> List[Dict[str, Any]]:
    """Fetch user's holdings."""
    try:
        return kite.holdings()
    except Exception as e:
        return {"error": str(e)}

@mcp_router.get("/margins")
def get_margins(kite: KiteConnect = Depends(get_kite)) -> Dict[str, Any]:
    """Fetch account margins."""
    try:
        return kite.margins()
    except Exception as e:
        return {"error": str(e)}

@mcp_router.post("/place_order")
def place_order(
    kite: KiteConnect = Depends(get_kite),
    tradingsymbol: str = Form(...),
    exchange: str = Form(...),
    transaction_type: str = Form(...),
    quantity: int = Form(...),
    product: str = Form(...),
    order_type: str = Form(...),
    price: Optional[float] = Form(None),
    trigger_price: Optional[float] = Form(None),
) -> Dict[str, Any]:
    """Place an order."""
    try:
        return kite.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
        )
    except Exception as e:
        return {"error": str(e)}

@mcp_router.post("/modify_order")
def modify_order(
    kite: KiteConnect = Depends(get_kite),
    order_id: str = Form(...),
    quantity: Optional[int] = Form(None),
    price: Optional[float] = Form(None),
    trigger_price: Optional[float] = Form(None),
    order_type: Optional[str] = Form(None),
) -> Dict[str, Any]:
    """Modify an existing order."""
    try:
        return kite.modify_order(
            variety="regular",
            order_id=order_id,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            order_type=order_type,
        )
    except Exception as e:
        return {"error": str(e)}

@mcp_router.post("/cancel_order")
def cancel_order(kite: KiteConnect = Depends(get_kite), order_id: str = Form(...)) -> Dict[str, Any]:
    """Cancel an order."""
    try:
        return kite.cancel_order(variety="regular", order_id=order_id)
    except Exception as e:
        return {"error": str(e)}

@mcp_router.get("/quote")
def get_quote(kite: KiteConnect = Depends(get_kite), symbols: List[str] = Query(...)) -> Dict[str, Any]:
    """Get quote for a list of symbols."""
    try:
        return kite.quote(symbols)
    except Exception as e:
        return {"error": str(e)}
