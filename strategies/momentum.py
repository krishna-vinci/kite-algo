from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from kiteconnect import KiteConnect
from broker_api.broker_api import get_kite
from database import get_db_connection

router = APIRouter()

@router.get("/momentum-portfolio/investable-margin")
async def get_momentum_investable_margin(kite: KiteConnect = Depends(get_kite)):
    """
    Retrieves the total investable equity margin for momentum strategy.
    """
    try:
        margins = kite.margins()
        equity_net_margin = margins.get("equity", {}).get("net", 0.0)
        return {"investable_margin": equity_net_margin}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve investable margin: {str(e)}")

@router.get("/momentum-portfolio")
def get_momentum_portfolio_endpoint():
    """
    Returns top momentum stocks as objects containing:
      - symbol (string)
      - ret    (float)  -> 252-day return %
      - ltp    (float)  -> latest close as LTP
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Load all tradingsymbols from consolidated tickers table
        # (kite_instruments may not exist in schema.sql; kite_ticker_tickers does)
        cur.execute("SELECT tradingsymbol FROM kite_ticker_tickers")
        symbols = [r[0] for r in cur.fetchall()]

        results = []
        for sym in symbols:
            # fetch most recent 252 closes (most recent first)
            cur.execute(
                'SELECT close FROM kite_historical_data WHERE tradingsymbol=%s ORDER BY "timestamp" DESC LIMIT 252',
                (sym,)
            )
            rows = cur.fetchall()
            if len(rows) == 252:
                latest = float(rows[0][0])
                oldest = float(rows[-1][0])
                ret = (latest / oldest - 1) * 100 if oldest != 0 else 0.0
                results.append({"symbol": sym, "ret": round(ret, 2), "ltp": round(latest, 2)})

        # sort by return descending and return top 15
        results.sort(key=lambda x: x["ret"], reverse=True)
        top15 = results[:15]
        return {"top_momentum_stocks": top15}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to compute momentum portfolio: {str(e)}")
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

# ─────────── Orders API (Non-MCP): Preview margins and place single/basket orders ───────────
# NOTE: These endpoints are independent of MCP and should be used by the frontend.

from pydantic import BaseModel
from typing import List, Optional

class OrderLeg(BaseModel):
    exchange: str  # e.g. "NSE"
    tradingsymbol: str  # e.g. "INFY"
    transaction_type: str  # "BUY" or "SELL"
    quantity: int
    product: str = "CNC"  # CNC / MIS / NRML
    order_type: str = "MARKET"  # MARKET / LIMIT / SL / SL-M
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    validity: Optional[str] = "DAY"  # DAY / IOC
    variety: Optional[str] = "regular"  # regular / amo / bo / co
    disclosed_quantity: Optional[int] = None
    tag: Optional[str] = None  # for grouping (e.g., "strategy:momentum")

class EquiAllocationRequest(BaseModel):
    selected_symbols: List[str]
    investable_capital: float
    excluded_symbols: Optional[List[str]] = []

class BasketOrderRequest(BaseModel):
    orders: List[OrderLeg]
    consider_positions: bool = True  # include current positions when computing margins
    margin_mode: Optional[str] = "compact"  # "compact" or None (per-leg breakdown)
    all_or_none: bool = False  # if True, attempt best-effort rollback on first failure
    dry_run: bool = False  # if True, only compute margins and return preview

def _order_leg_to_margin_dict(leg: OrderLeg) -> dict:
    """
    Build payload item for kite.basket_order_margins() as per Kite docs:
    Requires: exchange, tradingsymbol, transaction_type, variety, product, order_type, quantity
    Optional: price, trigger_price, validity, disclosed_quantity, tag
    """
    d = {
        "exchange": leg.exchange,
        "tradingsymbol": leg.tradingsymbol,
        "transaction_type": leg.transaction_type,
        "variety": leg.variety,
        "product": leg.product,
        "order_type": leg.order_type,
        "quantity": leg.quantity,
        "price": leg.price,
        "trigger_price": leg.trigger_price,
        "validity": leg.validity,
        "disclosed_quantity": leg.disclosed_quantity,
        "tag": leg.tag,
    }
    return {k: v for k, v in d.items() if v is not None}

def _order_leg_to_place_kwargs(leg: OrderLeg) -> dict:
    """
    Build kwargs for kite.place_order() as per Kite docs.
    """
    kwargs = {
        "variety": leg.variety or "regular",
        "exchange": leg.exchange,
        "tradingsymbol": leg.tradingsymbol,
        "transaction_type": leg.transaction_type,
        "quantity": leg.quantity,
        "product": leg.product,
        "order_type": leg.order_type,
        "price": leg.price,
        "trigger_price": leg.trigger_price,
        "validity": leg.validity,
        "disclosed_quantity": leg.disclosed_quantity,
        "tag": leg.tag,
    }
    return {k: v for k, v in kwargs.items() if v is not None}

@router.post("/orders/preview_margins")
def preview_basket_margins(req: BasketOrderRequest, kite: KiteConnect = Depends(get_kite)):
    """
    Compute total margins for a basket of orders (no placement).
    Uses kite.basket_order_margins() per official Kite docs.
    """
    try:
        params = [_order_leg_to_margin_dict(leg) for leg in req.orders]
        data = kite.basket_order_margins(params, consider_positions=req.consider_positions, mode=req.margin_mode)
        return {"status": "ok", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/orders/place")
def place_single_order(leg: OrderLeg, kite: KiteConnect = Depends(get_kite)):
    """
    Place a single order (non-basket). Thin wrapper over kite.place_order().
    """
    try:
        order_id = kite.place_order(**_order_leg_to_place_kwargs(leg))
        return {"status": "ok", "order_id": order_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/orders/place_basket")
def place_basket(req: BasketOrderRequest, kite: KiteConnect = Depends(get_kite)):
    """
    Place a basket by sequentially placing each leg via kite.place_order().
    - If dry_run is True, only returns margin preview.
    - If all_or_none is True, on first failure we attempt best-effort cancel of already placed orders.
      Note: Market orders may get executed immediately; cancellation isn't guaranteed.
    """
    try:
        if req.dry_run:
            params = [_order_leg_to_margin_dict(l) for l in req.orders]
            preview = kite.basket_order_margins(params, consider_positions=req.consider_positions, mode=req.margin_mode)
            return {"status": "dry_run", "margins": preview}

        results = []
        placed = []  # [{index, order_id}]
        errors = []

        for idx, leg in enumerate(req.orders):
            try:
                oid = kite.place_order(**_order_leg_to_place_kwargs(leg))
                placed.append({"index": idx, "order_id": oid})
                results.append({"index": idx, "tradingsymbol": leg.tradingsymbol, "order_id": oid, "status": "success"})
            except Exception as e:
                err = {"index": idx, "tradingsymbol": leg.tradingsymbol, "error": str(e)}
                errors.append(err)
                results.append({"index": idx, "tradingsymbol": leg.tradingsymbol, "status": "failed", "error": str(e)})

                if req.all_or_none:
                    # Best-effort rollback of previously placed orders
                    for p in placed:
                        try:
                            leg_for_cancel = req.orders[p["index"]]
                            kite.cancel_order(variety=(leg_for_cancel.variety or "regular"), order_id=p["order_id"])
                        except Exception:
                            # swallow cancel errors; we still report the failure
                            pass
                    return {
                        "status": "failed",
                        "results": results,
                        "errors": errors,
                        "note": "Best-effort rollback attempted; some orders may already be executed."
                    }

        final_status = "success" if not errors else "partial"
        return {"status": final_status, "results": results, "errors": errors}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/momentum-portfolio/calculate-equi-allocation")
async def calculate_equi_allocation(
    req: EquiAllocationRequest,
    kite: KiteConnect = Depends(get_kite)
):
    """
    Calculates an equi-weighted allocation for a list of selected stocks,
    considering investable capital, rounding, and marking impossible allocations.
    """
    try:
        # Filter out excluded symbols
        symbols_for_allocation = [
            s for s in req.selected_symbols if s not in req.excluded_symbols
        ]

        if not symbols_for_allocation:
            return {"message": "No symbols selected for allocation after exclusions.", "allocations": []}

        # Fetch LTP for selected symbols using KiteConnect
        ltp_response = kite.ltp([f"NSE:{s}" for s in symbols_for_allocation])
        ltp_data = {}
        for instrument_key, data in ltp_response.items():
            # instrument_key will be like "NSE:INFY"
            symbol = instrument_key.split(':')[1]
            if data and 'last_price' in data:
                ltp_data[symbol] = data['last_price']
            else:
                # If LTP not found, exclude from allocation
                req.excluded_symbols.append(symbol)
                print(f"Warning: LTP not found for {symbol} via KiteConnect. Excluding from allocation.")

        # Re-filter symbols after LTP check
        symbols_for_allocation = [
            s for s in req.selected_symbols if s not in req.excluded_symbols
        ]

        if not symbols_for_allocation:
            return {"message": "No symbols with valid LTP for allocation after exclusions.", "allocations": []}


        num_stocks = len(symbols_for_allocation)
        if num_stocks == 0:
            return {"message": "No stocks selected for equi-weighted allocation.", "allocations": []}

        capital_per_stock = req.investable_capital / num_stocks
        allocations = []
        remaining_capital = req.investable_capital

        for symbol in symbols_for_allocation:
            ltp = ltp_data.get(symbol)
            if ltp is None:
                allocations.append({
                    "symbol": symbol,
                    "quantity": 0,
                    "allocated_value": 0.0,
                    "status": "LTP_NOT_FOUND",
                    "reason": "Could not retrieve Last Traded Price."
                })
                continue

            if ltp > capital_per_stock:
                allocations.append({
                    "symbol": symbol,
                    "quantity": 0,
                    "allocated_value": 0.0,
                    "status": "IMPOSSIBLE_ALLOCATION",
                    "reason": f"Share price ({ltp}) is higher than equi-weighted capital per stock ({capital_per_stock:.2f})."
                })
            else:
                quantity = int(capital_per_stock / ltp)
                allocated_value = quantity * ltp
                allocations.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "allocated_value": round(allocated_value, 2),
                    "status": "ALLOCATED"
                })
                remaining_capital -= allocated_value

        # Optional: Redistribute remaining capital to other stocks if desired,
        # or simply return it as unallocated. For now, we'll return it as unallocated.

        return {
            "message": "Equi-weighted allocation calculated.",
            "investable_capital": req.investable_capital,
            "total_allocated_value": round(req.investable_capital - remaining_capital, 2),
            "unallocated_capital": round(remaining_capital, 2),
            "allocations": allocations
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calculating equi-weighted allocation: {str(e)}")
