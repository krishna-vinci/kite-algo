import logging
import json
import redis
import uuid
from datetime import datetime
from contextlib import contextmanager
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from pydantic import BaseModel
from kiteconnect import KiteConnect
from broker_api.broker_api import get_kite
from broker_api.kite_orders import (
    OrdersService,
    BasketOrderRequest,
    PlaceOrderRequest,
    Exchange,
    TransactionType,
    Variety,
    Product,
    OrderType,
    Validity,
)
from database import get_db_connection
import os

# Configure logging
logger = logging.getLogger(__name__)

# Redis connection for caching (optional)
try:
    redis_client = redis.Redis(
        host=os.getenv('REDIS_HOST', 'redis'),
        port=int(os.getenv('REDIS_PORT', 6379)),
        db=int(os.getenv('REDIS_DB', 0)),
        decode_responses=True,
        socket_connect_timeout=1
    )
    # Test connection
    redis_client.ping()
    REDIS_AVAILABLE = True
    logger.info("Redis cache connected successfully")
except Exception as e:
    redis_client = None
    REDIS_AVAILABLE = False
    logger.warning(f"Redis not available, running without cache: {str(e)}")

# Cache TTLs
LTP_CACHE_TTL = 5  # 5 seconds for LTP data
ALLOCATION_CACHE_TTL = 10  # 10 seconds for allocation calculations

router = APIRouter(prefix="/momentum-portfolio", tags=["Momentum"])

@contextmanager
def get_db_cursor():
    """
    Context manager to yield a database cursor and ensure connection is closed.
    """
    conn = None
    try:
        conn = get_db_connection()
        yield conn.cursor()
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()

@router.get("/investable-margin")
async def get_momentum_investable_margin(kite: KiteConnect = Depends(get_kite)):
    """
    Retrieves the total investable equity margin for momentum strategy.
    """
    try:
        margins = kite.margins()
        equity_net_margin = margins.get("equity", {}).get("net", 0.0)
        return {"investable_margin": equity_net_margin}
    except Exception as e:
        logger.error(f"Failed to retrieve investable margin: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve investable margin: {str(e)}")

@router.get("/live-ltp")
async def get_live_ltp_for_symbols(
    symbols: List[str] = Query(..., description="List of trading symbols (e.g., INFY, RELIANCE)"),
    kite: KiteConnect = Depends(get_kite),
    use_cache: bool = Query(True, description="Use Redis cache for LTP data")
):
    """
    Retrieves the live Last Traded Price (LTP) for a given list of trading symbols with Redis caching.
    """
    if not symbols:
        return {}

    try:
        live_ltp_data = {}
        symbols_to_fetch = []
        
        # Try to get from cache first if Redis is available
        if use_cache and REDIS_AVAILABLE:
            try:
                for symbol in symbols:
                    cache_key = f"ltp:NSE:{symbol}"
                    cached_ltp = redis_client.get(cache_key)
                    if cached_ltp:
                        live_ltp_data[symbol] = float(cached_ltp)
                    else:
                        symbols_to_fetch.append(symbol)
            except Exception as cache_error:
                logger.warning(f"Cache read failed, fetching all from API: {cache_error}")
                symbols_to_fetch = symbols
        else:
            symbols_to_fetch = symbols
        
        # Fetch missing symbols from Kite API
        if symbols_to_fetch:
            kite_symbols = [f"NSE:{s}" for s in symbols_to_fetch]
            ltp_response = kite.ltp(kite_symbols)
            
            for instrument_key, data in ltp_response.items():
                symbol = instrument_key.split(':')[1]
                if data and 'last_price' in data:
                    ltp = data['last_price']
                    live_ltp_data[symbol] = ltp
                    # Cache with TTL if Redis is available
                    if use_cache and REDIS_AVAILABLE:
                        try:
                            redis_client.setex(f"ltp:NSE:{symbol}", LTP_CACHE_TTL, str(ltp))
                        except Exception as cache_error:
                            logger.warning(f"Cache write failed: {cache_error}")
        
        return live_ltp_data
    except Exception as e:
        logger.error(f"Failed to retrieve live LTP: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve live LTP: {str(e)}")

@router.get("/sources")
def get_momentum_sources():
    """
    Returns available source lists (indices) for momentum strategy.
    """
    try:
        with get_db_cursor() as cur:
            query = """
                SELECT DISTINCT source_list
                FROM kite_ticker_tickers
                ORDER BY source_list
            """
            cur.execute(query)
            rows = cur.fetchall()
            sources = [row[0] for row in rows]
            return {"sources": sources}
    except Exception as e:
        logger.error(f"Failed to fetch momentum sources: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch momentum sources: {str(e)}")

@router.get("")
def get_momentum_portfolio_endpoint(
    source_list: Optional[str] = Query(None, description="Filter by source list (e.g., Nifty50, Nifty500)")
):
    """
    Returns top momentum stocks as objects containing:
      - symbol (string)
      - ret    (float)  -> 252-day return %
      - ltp    (float)  -> latest close as LTP (from historical data)
    Optionally filters by source_list parameter.
    """
    try:
        with get_db_cursor() as cur:
            # Window-based query fetches latest and 252nd closing price per symbol in a single pass.
            # Optionally filters by source_list
            where_clause = "WHERE rn IN (1, 252)"
            if source_list:
                where_clause += " AND source_list = %s"
            
            query = f"""
                WITH ranked_prices AS (
                    SELECT
                        t.tradingsymbol,
                        t.source_list,
                        h.close,
                        ROW_NUMBER() OVER (
                            PARTITION BY t.tradingsymbol
                            ORDER BY h.timestamp DESC
                        ) AS rn
                    FROM kite_ticker_tickers t
                    JOIN kite_historical_data h
                      ON h.tradingsymbol = t.tradingsymbol
                    {("WHERE t.source_list = %s" if source_list else "")}
                )
                SELECT
                    tradingsymbol,
                    MAX(CASE WHEN rn = 1 THEN close END) AS latest,
                    MAX(CASE WHEN rn = 252 THEN close END) AS oldest
                FROM ranked_prices
                WHERE rn IN (1, 252)
                {("AND source_list = %s" if source_list else "")}
                GROUP BY tradingsymbol
                HAVING COUNT(*) = 2
            """
            
            if source_list:
                cur.execute(query, (source_list, source_list))  # Pass twice: once for CTE, once for outer WHERE
            else:
                cur.execute(query)
            rows = cur.fetchall()

            results = []
            for row in rows:
                sym = row[0]
                latest = float(row[1])
                oldest = float(row[2])
                
                ret = (latest / oldest - 1) * 100 if oldest != 0 else 0.0
                results.append({"symbol": sym, "ret": round(ret, 2), "ltp": round(latest, 2)})

            # sort by return descending and return top 5
            results.sort(key=lambda x: x["ret"], reverse=True)
            top5 = results[:5]
            return {"top_momentum_stocks": top5, "source_list": source_list or "All"}
    except Exception as e:
        logger.error(f"Failed to compute momentum portfolio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to compute momentum portfolio: {str(e)}")


# ─────────── Allocation Calculation ───────────

class EquiAllocationRequest(BaseModel):
    selected_symbols: List[str]
    investable_capital: float
    excluded_symbols: Optional[List[str]] = []

class UpdateQuantityRequest(BaseModel):
    symbol: str
    quantity: int
    ltp: float

@router.post("/calculate-equi-allocation")
async def calculate_equi_allocation(
    req: EquiAllocationRequest,
    kite: KiteConnect = Depends(get_kite)
):
    """
    Calculates an equi-weighted allocation for a list of selected stocks,
    considering investable capital, rounding, and marking impossible allocations.
    """
    try:
        # Input validation
        if req.investable_capital <= 0:
             return {
                "message": "Investable capital must be greater than zero.",
                "allocations": [],
                "total_allocated_value": 0.0,
                "unallocated_capital": req.investable_capital
            }

        # Filter out excluded symbols
        symbols_for_allocation = [
            s for s in req.selected_symbols if s not in (req.excluded_symbols or [])
        ]

        if not symbols_for_allocation:
            return {"message": "No symbols selected for allocation after exclusions.", "allocations": []}

        # Fetch LTP for selected symbols with caching
        ltp_data = {}
        symbols_to_fetch = []
        
        # Try Redis cache first if available
        if REDIS_AVAILABLE:
            try:
                for symbol in symbols_for_allocation:
                    cache_key = f"ltp:NSE:{symbol}"
                    cached_ltp = redis_client.get(cache_key)
                    if cached_ltp:
                        ltp_data[symbol] = float(cached_ltp)
                    else:
                        symbols_to_fetch.append(symbol)
            except Exception as cache_error:
                logger.warning(f"Cache read failed: {cache_error}")
                symbols_to_fetch = symbols_for_allocation
        else:
            symbols_to_fetch = symbols_for_allocation
        
        # Fetch missing symbols from Kite API
        if symbols_to_fetch:
            kite_symbols = [f"NSE:{s}" for s in symbols_to_fetch]
            try:
                ltp_response = kite.ltp(kite_symbols)
                for instrument_key, data in ltp_response.items():
                    symbol = instrument_key.split(':')[1]
                    if data and 'last_price' in data:
                        ltp = data['last_price']
                        ltp_data[symbol] = ltp
                        # Cache for quick access if Redis available
                        if REDIS_AVAILABLE:
                            try:
                                redis_client.setex(f"ltp:NSE:{symbol}", LTP_CACHE_TTL, str(ltp))
                            except Exception as cache_error:
                                logger.warning(f"Cache write failed: {cache_error}")
                    else:
                        if req.excluded_symbols is None:
                            req.excluded_symbols = []
                        req.excluded_symbols.append(symbol)
                        logger.warning(f"LTP not found for {symbol}. Excluding from allocation.")
            except Exception as e:
                logger.error(f"Kite LTP fetch failed: {str(e)}")
                raise HTTPException(status_code=502, detail=f"Failed to fetch live prices: {str(e)}")

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
            if ltp is None or ltp <= 0:
                allocations.append({
                    "symbol": symbol,
                    "ltp": 0,
                    "quantity": 0,
                    "allocated_value": 0.0,
                    "status": "LTP_INVALID",
                    "reason": "Last Traded Price is invalid or missing."
                })
                continue

            # Always try to allocate at least 1 share if affordable
            if ltp > capital_per_stock:
                # Stock price exceeds per-stock allocation, but try 1 share if within total capital
                if ltp <= req.investable_capital:
                    allocations.append({
                        "symbol": symbol,
                        "ltp": ltp,
                        "quantity": 1,
                        "allocated_value": round(ltp, 2),
                        "status": "ALLOCATED",
                        "reason": f"Allocated 1 share (price ₹{ltp:.2f} > equi-weight ₹{capital_per_stock:.2f})"
                    })
                    remaining_capital -= ltp
                else:
                    allocations.append({
                        "symbol": symbol,
                        "ltp": ltp,
                        "quantity": 0,
                        "allocated_value": 0.0,
                        "status": "IMPOSSIBLE_ALLOCATION",
                        "reason": f"Share price (₹{ltp:.2f}) exceeds total capital"
                    })
            else:
                # Normal equi-weighted allocation
                quantity = max(1, int(capital_per_stock / ltp))  # At least 1 share
                allocated_value = quantity * ltp
                allocations.append({
                    "symbol": symbol,
                    "ltp": ltp,
                    "quantity": quantity,
                    "allocated_value": round(allocated_value, 2),
                    "status": "ALLOCATED"
                })
                remaining_capital -= allocated_value

        return {
            "message": "Equi-weighted allocation calculated.",
            "investable_capital": req.investable_capital,
            "total_allocated_value": round(req.investable_capital - remaining_capital, 2),
            "unallocated_capital": round(remaining_capital, 2),
            "allocations": allocations
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating equi-weighted allocation: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error calculating equi-weighted allocation: {str(e)}")

# ─────────── Persistent Strategy Management ───────────

class PortfolioEntryItem(BaseModel):
    symbol: str
    quantity: int
    ltp: float
    exchange: str = "NSE"
    instrument_token: Optional[int] = None
    order_id: Optional[str] = None

class EnterPortfolioRequest(BaseModel):
    strategy_name: str = "Nifty50 Momentum"
    strategy_type: str = "MOMENTUM"
    tag: Optional[str] = None # Will auto-generate if None (e.g., MOMENTUM_YYYY_MM_DD)
    holdings: List[PortfolioEntryItem]
    linked_index_symbol: str = "NIFTY 50"

@router.post("/enter")
async def enter_portfolio_endpoint(
    req: EnterPortfolioRequest,
    kite: KiteConnect = Depends(get_kite)
):
    """
    Uses ws_order_events table (WebSocket captured orders) to create holdings.
    This is more reliable than Kite API as WebSocket events are already captured.
    """
    try:
        import asyncio
        
        tag = req.tag or f"MOMENTUM_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"
        
        # Extract order IDs from the request
        order_ids = [h.order_id for h in req.holdings if h.order_id]
        
        if not order_ids:
            logger.warning("[ENTER] No order_ids provided")
            raise HTTPException(status_code=400, detail="No order_ids provided")
        
        logger.info(f"[ENTER] Creating portfolio '{tag}' from {len(order_ids)} order IDs: {order_ids}")
        
        # Resolve linked index token
        linked_index_token = None
        try:
            with get_db_cursor() as cur:
                cur.execute(
                    "SELECT instrument_token FROM kite_indices WHERE tradingsymbol = %s LIMIT 1",
                    (req.linked_index_symbol,)
                )
                row = cur.fetchone()
                if row:
                    linked_index_token = row[0]
        except Exception:
            pass
        
        # Wait briefly for WebSocket to capture order events
        logger.info("[ENTER] Waiting 2 seconds for WebSocket to capture order events...")
        await asyncio.sleep(2.0)
        
        # Use INSERT...SELECT directly from ws_order_events - PROVEN TO WORK
        with get_db_cursor() as cur:
            insert_query = """
                INSERT INTO investing_strategies (
                    strategy_name, strategy_type, tag, instrument_token, tradingsymbol, exchange,
                    quantity, invested_amount, entry_price, last_price, pnl, pnl_percent,
                    status, linked_index_token, linked_index_symbol, entry_date, order_id
                )
                SELECT 
                    %s, %s, %s,
                    instrument_token,
                    tradingsymbol,
                    exchange,
                    filled_quantity,
                    filled_quantity * average_price,
                    average_price,
                    average_price,
                    0.0, 0.0,
                    'ACTIVE',
                    %s, %s,
                    NOW(),
                    order_id
                FROM (
                    SELECT DISTINCT ON (order_id) *
                    FROM ws_order_events
                    WHERE order_id = ANY(%s)
                      AND status = 'COMPLETE'
                      AND transaction_type = 'BUY'
                      AND filled_quantity > 0
                    ORDER BY order_id, event_timestamp DESC
                ) completed_orders
                RETURNING tradingsymbol, quantity, entry_price, order_id
            """
            
            logger.info(f"[ENTER] Executing INSERT...SELECT with order_ids: {order_ids}")
            cur.execute(insert_query, (
                req.strategy_name,
                req.strategy_type,
                tag,
                linked_index_token,
                req.linked_index_symbol,
                order_ids
            ))
            
            inserted_rows = cur.fetchall()
            get_db_connection().commit()
            
            inserted_count = len(inserted_rows)
            logger.info(f"[ENTER] Successfully inserted {inserted_count}/{len(order_ids)} holdings: {inserted_rows}")
            
            if inserted_count == 0:
                # Check what's in ws_order_events for debugging
                cur.execute("""
                    SELECT order_id, status, transaction_type 
                    FROM ws_order_events 
                    WHERE order_id = ANY(%s) 
                    ORDER BY event_timestamp DESC
                """, (order_ids,))
                ws_records = cur.fetchall()
                logger.error(f"[ENTER] No holdings inserted. ws_order_events records: {ws_records}")
                raise HTTPException(
                    status_code=400, 
                    detail=f"No COMPLETE BUY orders found. WebSocket records: {ws_records}"
                )
            
        return {
            "status": "success", 
            "message": f"Portfolio '{tag}' saved with {inserted_count} holdings.", 
            "tag": tag,
            "holdings_count": inserted_count
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ENTER] Failed to save portfolio: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save portfolio: {str(e)}")


# ─────────── New Robust Place-and-Enter Endpoint ───────────

class PlaceAndEnterRequest(BaseModel):
    """Request model for placing orders and creating portfolio entries atomically"""
    selected_symbols: List[str]
    investable_capital: float
    excluded_symbols: Optional[List[str]] = []
    strategy_name: str = "Nifty50 Momentum"
    strategy_type: str = "MOMENTUM"
    tag: Optional[str] = None  # Auto-generates if None
    linked_index_symbol: str = "NIFTY 50"
    use_amo: bool = False


class PlaceAndEnterResponse(BaseModel):
    """Response model for place-and-enter endpoint"""
    status: str  # "success", "partial", "failed"
    message: str
    orders_placed: int
    holdings_created: int
    order_results: List[Dict[str, Any]]
    portfolio_tag: str
    total_invested: float


@router.post("/place-and-enter", response_model=PlaceAndEnterResponse)
async def place_orders_and_enter_portfolio(
    req: PlaceAndEnterRequest,
    request: Request,
    kite: KiteConnect = Depends(get_kite)
):
    """
    Places orders directly and creates portfolio entries atomically.
    Eliminates frontend order ID dependency - handles entire flow server-side.
    
    Flow:
    1. Calculate allocations from selected symbols
    2. Build and place basket orders via Kite API
    3. Insert successful orders into investing_strategies table
    4. Return comprehensive response with order results
    """
    try:
        # Generate portfolio tag for internal tracking
        tag = req.tag or f"MOMENTUM_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"
        
        # Generate kite_ref_tag (max 20 chars): MOM-{INDEX}-YY-MM-DD
        # Index abbreviations: NIFTY 50 -> N50, NIFTY 500 -> N500, NIFTY LARGEMIDCAP 250 -> NLM
        index_abbrev = {
            'NIFTY 50': 'N50',
            'NIFTY 500': 'N500',
            'NIFTY LARGEMIDCAP 250': 'NLM',
            'NIFTY NEXT 50': 'NN50'
        }.get(req.linked_index_symbol, 'MOM')
        kite_ref_tag = f"MOM-{index_abbrev}-{datetime.now().strftime('%y-%m-%d')}"  # e.g., MOM-N50-25-11-26 (16 chars)
        
        logger.info(f"[PLACE-AND-ENTER] Starting for {len(req.selected_symbols)} symbols, capital={req.investable_capital}, tag={tag}, kite_ref_tag={kite_ref_tag}")

        # Step 1: Calculate allocations using existing logic
        allocation_request = EquiAllocationRequest(
            selected_symbols=req.selected_symbols,
            investable_capital=req.investable_capital,
            excluded_symbols=req.excluded_symbols or []
        )
        allocation_result = await calculate_equi_allocation(allocation_request, kite)
        
        allocations = allocation_result.get("allocations", [])
        allocated_items = [a for a in allocations if a.get("status") == "ALLOCATED" and a.get("quantity", 0) > 0]
        
        if not allocated_items:
            logger.warning("[PLACE-AND-ENTER] No allocations possible")
            return PlaceAndEnterResponse(
                status="failed",
                message="No symbols could be allocated with given capital",
                orders_placed=0,
                holdings_created=0,
                order_results=[],
                portfolio_tag=tag,
                total_invested=0.0
            )
        
        logger.info(f"[PLACE-AND-ENTER] Calculated {len(allocated_items)} allocations")

        # Step 2: Build basket orders from allocation results
        basket_orders = []
        for allocation in allocated_items:
            order = PlaceOrderRequest(
                exchange=Exchange.NSE,
                tradingsymbol=allocation['symbol'],
                transaction_type=TransactionType.BUY,
                variety=Variety.AMO if req.use_amo else Variety.REGULAR,
                product=Product.CNC,
                order_type=OrderType.LIMIT if req.use_amo else OrderType.MARKET,
                quantity=allocation['quantity'],
                price=allocation['ltp'] if req.use_amo else None,
                validity=Validity.DAY,
                tag=kite_ref_tag  # e.g., MOM-N50-25-11-26 (16 chars, max 20)
            )
            basket_orders.append(order)
        
        logger.info(f"[PLACE-AND-ENTER] Built {len(basket_orders)} basket orders")

        # Step 3: Execute basket orders using OrdersService
        orders_service = OrdersService()
        basket_request = BasketOrderRequest(
            orders=basket_orders,
            all_or_none=False,  # Allow partial success
            dry_run=False
        )
        
        # Get correlation ID and session ID from request
        corr_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        session_id = request.headers.get("x-session-id") or request.cookies.get("kite_session_id")
        
        order_response = await orders_service.place_basket(kite, basket_request, corr_id, session_id)
        
        logger.info(f"[PLACE-AND-ENTER] Basket response status={order_response.status}, results={len(order_response.results)}")

        # Step 4: Insert successful orders into investing_strategies
        successful_holdings = []
        
        # Resolve linked index token once
        linked_index_token = None
        with get_db_cursor() as cur:
            try:
                cur.execute(
                    "SELECT instrument_token FROM kite_indices WHERE tradingsymbol = %s LIMIT 1",
                    (req.linked_index_symbol,)
                )
                row = cur.fetchone()
                if row:
                    linked_index_token = row[0]
            except Exception as e:
                logger.warning(f"[PLACE-AND-ENTER] Could not resolve index token: {e}")
        
        # Process each order result
        for result in order_response.results:
            if result.status == "success" and result.order_id:
                # Find matching allocation
                allocation = next(
                    (a for a in allocated_items if a['symbol'] == result.tradingsymbol),
                    None
                )
                
                if allocation:
                    try:
                        holding_id = _insert_portfolio_holding(
                            strategy_name=req.strategy_name,
                            strategy_type=req.strategy_type,
                            tag=tag,
                            kite_ref_tag=kite_ref_tag,
                            allocation=allocation,
                            order_id=result.order_id,
                            linked_index_token=linked_index_token,
                            linked_index_symbol=req.linked_index_symbol
                        )
                        if holding_id:
                            successful_holdings.append({
                                "id": holding_id,
                                "symbol": result.tradingsymbol,
                                "order_id": result.order_id,
                                "quantity": allocation['quantity'],
                                "invested": allocation['allocated_value']
                            })
                            logger.info(f"[PLACE-AND-ENTER] Inserted holding {holding_id} for {result.tradingsymbol}")
                    except Exception as e:
                        logger.error(f"[PLACE-AND-ENTER] Failed to insert holding for {result.tradingsymbol}: {e}")

        # Step 5: Build comprehensive response
        total_invested = sum(h['invested'] for h in successful_holdings)
        orders_placed = len([r for r in order_response.results if r.status == "success"])
        
        if len(successful_holdings) == len(basket_orders):
            status = "success"
            message = f"Portfolio '{tag}' created with {len(successful_holdings)} holdings"
        elif len(successful_holdings) > 0:
            status = "partial"
            message = f"Partial success: {len(successful_holdings)}/{len(basket_orders)} holdings created"
        else:
            status = "failed"
            message = "All orders failed - no holdings created"
        
        logger.info(f"[PLACE-AND-ENTER] Complete: status={status}, holdings={len(successful_holdings)}, invested={total_invested}")
        
        return PlaceAndEnterResponse(
            status=status,
            message=message,
            orders_placed=orders_placed,
            holdings_created=len(successful_holdings),
            order_results=[r.model_dump() for r in order_response.results],
            portfolio_tag=tag,
            total_invested=round(total_invested, 2)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PLACE-AND-ENTER] Failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to place orders and create portfolio: {str(e)}")


def _insert_portfolio_holding(
    strategy_name: str,
    strategy_type: str,
    tag: str,
    kite_ref_tag: str,
    allocation: Dict[str, Any],
    order_id: str,
    linked_index_token: Optional[int],
    linked_index_symbol: str
) -> Optional[str]:
    """
    Insert a single portfolio holding into investing_strategies table.
    Returns the holding ID or None on failure.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get instrument token for the symbol
        instrument_token = None
        try:
            cur.execute(
                "SELECT instrument_token FROM kite_instruments WHERE tradingsymbol = %s AND exchange = 'NSE' LIMIT 1",
                (allocation['symbol'],)
            )
            row = cur.fetchone()
            if row:
                instrument_token = row[0]
        except Exception as e:
            logger.warning(f"Could not get instrument_token for {allocation['symbol']}: {e}")
        
        if not instrument_token:
            logger.error(f"No instrument_token found for {allocation['symbol']}, skipping insert")
            return None

        # Insert the holding with PENDING status (will be updated to ACTIVE when order fills)
        insert_query = """
            INSERT INTO investing_strategies (
                strategy_name, strategy_type, tag, kite_ref_tag, instrument_token, tradingsymbol, exchange,
                quantity, invested_amount, entry_price, last_price, pnl, pnl_percent,
                status, linked_index_token, linked_index_symbol, entry_date, order_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
            RETURNING id
        """
        
        cur.execute(insert_query, (
            strategy_name,
            strategy_type,
            tag,
            kite_ref_tag,
            instrument_token,
            allocation['symbol'],
            'NSE',
            allocation['quantity'],
            allocation['allocated_value'],
            allocation['ltp'],  # Entry price = LTP at order time
            allocation['ltp'],  # Last price starts at LTP
            0.0,  # P&L starts at zero
            0.0,  # P&L percent starts at zero
            'PENDING',  # Will be updated to ACTIVE when order fills via lazy verification
            linked_index_token,
            linked_index_symbol,
            order_id
        ))
        
        result = cur.fetchone()
        conn.commit()  # Commit on the SAME connection
        cur.close()
        
        return str(result[0]) if result else None
    except Exception as e:
        logger.error(f"Failed to insert holding for {allocation['symbol']}: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            conn.close()


@router.get("/holdings")
async def get_holdings_endpoint(
    strategy_name: str = Query("Nifty50 Momentum"),
    status: str = Query("ACTIVE")
):
    """
    Fetches current holdings from investing_strategies table.
    Includes lazy verification of PENDING and PENDING_EXIT orders against order_events.
    """
    try:
        # Lazy Verification Logic for PENDING entry orders
        verify_conn = None
        try:
            verify_conn = get_db_connection()
            cur = verify_conn.cursor()
            
            # Find pending entry orders for this strategy
            cur.execute("""
                SELECT id, order_id 
                FROM investing_strategies 
                WHERE strategy_name = %s AND status = 'PENDING' AND order_id IS NOT NULL
            """, (strategy_name,))
            pending = cur.fetchall()
            
            for row in pending:
                strat_id, order_id = row
                # Check ws_order_events for confirmation (most recent status from WebSocket)
                # Prioritize COMPLETE status when timestamps are equal
                cur.execute("""
                    SELECT status, average_price, filled_quantity
                    FROM ws_order_events 
                    WHERE order_id = %s 
                    ORDER BY event_timestamp DESC, 
                             CASE WHEN status = 'COMPLETE' THEN 0 
                                  WHEN status = 'REJECTED' THEN 1 
                                  WHEN status = 'CANCELLED' THEN 2 
                                  ELSE 3 END
                    LIMIT 1
                """, (order_id,))
                event = cur.fetchone()
                
                if event:
                    order_status = event[0]
                    avg_price = event[1]
                    filled_qty = event[2]
                    
                    if order_status == 'COMPLETE':
                        # Update with actual execution details
                        if avg_price and filled_qty:
                            cur.execute("""
                                UPDATE investing_strategies 
                                SET status = 'ACTIVE', entry_price = %s, quantity = %s, 
                                    invested_amount = %s * %s, updated_at = NOW()
                                WHERE id = %s
                            """, (avg_price, filled_qty, avg_price, filled_qty, strat_id))
                        else:
                            cur.execute("UPDATE investing_strategies SET status = 'ACTIVE' WHERE id = %s", (strat_id,))
                    elif order_status in ('REJECTED', 'CANCELLED'):
                        cur.execute("UPDATE investing_strategies SET status = 'FAILED' WHERE id = %s", (strat_id,))
            
            # Verify PENDING_EXIT orders and capture exit prices
            cur.execute("""
                SELECT id, order_id, tradingsymbol, quantity
                FROM investing_strategies 
                WHERE strategy_name = %s AND status = 'PENDING_EXIT' AND order_id IS NOT NULL
            """, (strategy_name,))
            pending_exits = cur.fetchall()
            
            for row in pending_exits:
                strat_id, order_id, symbol, quantity = row
                # Check ws_order_events for exit confirmation
                # Prioritize COMPLETE status when timestamps are equal
                cur.execute("""
                    SELECT status, average_price, filled_quantity
                    FROM ws_order_events 
                    WHERE order_id = %s 
                    ORDER BY event_timestamp DESC, 
                             CASE WHEN status = 'COMPLETE' THEN 0 
                                  WHEN status = 'REJECTED' THEN 1 
                                  WHEN status = 'CANCELLED' THEN 2 
                                  ELSE 3 END
                    LIMIT 1
                """, (order_id,))
                event = cur.fetchone()
                
                if event:
                    order_status = event[0]
                    avg_price = event[1]
                    filled_qty = event[2]
                    
                    if order_status == 'COMPLETE':
                        # Mark as exited with actual exit price
                        cur.execute("""
                            UPDATE investing_strategies 
                            SET status = 'EXITED', exit_price = %s, updated_at = NOW()
                            WHERE id = %s
                        """, (avg_price, strat_id))
                    elif order_status in ('REJECTED', 'CANCELLED'):
                        # Exit failed, revert to ACTIVE
                        cur.execute("""
                            UPDATE investing_strategies 
                            SET status = 'ACTIVE', order_id = NULL, exit_date = NULL, updated_at = NOW()
                            WHERE id = %s
                        """, (strat_id,))
            
            verify_conn.commit()  # Commit on the SAME connection
            cur.close()
        except Exception as verify_error:
            logger.error(f"Lazy verification failed: {verify_error}")
            if verify_conn:
                verify_conn.rollback()
            # Continue to fetch holdings even if verification fails
        finally:
            if verify_conn:
                verify_conn.close()

        with get_db_cursor() as cur:
            # Fetch holdings matching the requested status
            # If the user asks for 'ACTIVE', they will now see newly confirmed orders.
            query = """
                SELECT 
                    id, strategy_name, tag, kite_ref_tag, instrument_token, tradingsymbol, exchange,
                    quantity, invested_amount, entry_price, last_price, pnl, pnl_percent,
                    status, entry_date, order_id, linked_index_symbol
                FROM investing_strategies
                WHERE strategy_name = %s AND status = %s
                ORDER BY linked_index_symbol, tradingsymbol
            """
            cur.execute(query, (strategy_name, status))
            rows = cur.fetchall()
            
            holdings = []
            for row in rows:
                holdings.append({
                    "id": str(row[0]),
                    "strategy_name": row[1],
                    "tag": row[2],
                    "kite_ref_tag": row[3],
                    "instrument_token": row[4],
                    "symbol": row[5],
                    "exchange": row[6],
                    "quantity": row[7],
                    "invested_amount": float(row[8]) if row[8] else 0,
                    "entry_price": float(row[9]) if row[9] else 0,
                    "last_price": float(row[10]) if row[10] else 0,
                    "pnl": float(row[11]) if row[11] else 0,
                    "pnl_percent": float(row[12]) if row[12] else 0,
                    "status": row[13],
                    "entry_date": row[14].isoformat() if row[14] else None,
                    "order_id": row[15],
                    "linked_index_symbol": row[16]
                })
                
            return {"holdings": holdings}
    except Exception as e:
        logger.error(f"Failed to fetch holdings: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch holdings: {str(e)}")

class ExitPortfolioRequest(BaseModel):
    strategy_name: str = "Nifty50 Momentum"
    tag: Optional[str] = None
    exit_orders: List[Dict[str, Any]]  # Order results from frontend with order_ids

@router.post("/exit-portfolio")
async def exit_portfolio_endpoint(req: ExitPortfolioRequest):
    """
    Marks positions as PENDING_EXIT and captures order_ids for exit price verification.
    The actual exit status and prices will be verified via ws_order_events.
    """
    try:
        with get_db_cursor() as cur:
            # Map order_ids from exit orders
            order_map = {}
            for order_result in req.exit_orders:
                if order_result.get('status') == 'success' and order_result.get('order_id'):
                    order_map[order_result['tradingsymbol']] = order_result['order_id']
            
            # Update holdings with exit order_ids and mark as PENDING_EXIT
            where_clause = "strategy_name = %s AND status = 'ACTIVE'"
            params = [req.strategy_name]
            
            if req.tag:
                where_clause += " AND tag = %s"
                params.append(req.tag)
            
            # Fetch all active holdings to process individually
            query = f"SELECT id, tradingsymbol FROM investing_strategies WHERE {where_clause}"
            cur.execute(query, tuple(params))
            holdings = cur.fetchall()
            
            updated_count = 0
            for holding_id, symbol in holdings:
                order_id = order_map.get(symbol)
                if order_id:
                    # Mark as PENDING_EXIT with order_id for verification
                    cur.execute("""
                        UPDATE investing_strategies 
                        SET status = 'PENDING_EXIT', order_id = %s, exit_date = NOW(), updated_at = NOW()
                        WHERE id = %s
                    """, (order_id, holding_id))
                    updated_count += 1
                else:
                    # Order failed or not placed, keep as ACTIVE
                    logger.warning(f"No exit order_id for {symbol}, keeping ACTIVE")
            
            get_db_connection().commit()
            
        return {
            "status": "success" if updated_count > 0 else "partial",
            "message": f"Portfolio exit initiated: {updated_count} orders placed",
            "updated_count": updated_count,
            "total_holdings": len(holdings)
        }

    except Exception as e:
        logger.error(f"Failed to exit portfolio: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to exit portfolio: {str(e)}")

class RebalanceRequest(BaseModel):
    current_holdings: List[Dict[str, Any]]
    new_top_stocks: List[Dict[str, Any]]
    target_capital: Optional[float] = None
    linked_index_symbol: Optional[str] = None

@router.post("/rebalance")
async def rebalance_portfolio_endpoint(
    req: RebalanceRequest,
    kite: KiteConnect = Depends(get_kite)
):
    """
    Enhanced rebalance logic with capital adjustment support.
    Calculates exits, holds adjustments, and entries based on target capital.
    Returns structured actions with exact quantities needed.
    """
    try:
        current_holdings = req.current_holdings
        new_top_stocks = req.new_top_stocks
        target_capital = req.target_capital
        linked_index_symbol = req.linked_index_symbol
        
        # Calculate current portfolio value
        current_value = sum(h['quantity'] * h['last_price'] for h in current_holdings)
        
        # Use target_capital if provided, otherwise maintain current value
        target = target_capital if target_capital else current_value
        
        # Identify stock changes
        current_symbols = {h['symbol'] for h in current_holdings}
        new_symbols = {s['symbol'] for s in new_top_stocks[:5]}  # Ensure top 5 for testing
        
        to_exit_symbols = current_symbols - new_symbols
        to_enter_symbols = new_symbols - current_symbols
        to_hold_symbols = current_symbols & new_symbols
        
        # Check if no changes needed
        no_changes = len(to_exit_symbols) == 0 and len(to_enter_symbols) == 0
        
        # Calculate target allocation per stock (equi-weighted)
        target_per_stock = target / 5  # Top 5 stocks for testing
        
        # Fetch fresh LTPs for all symbols
        all_symbols = list(new_symbols)
        ltp_map = {}
        if all_symbols:
            kite_symbols = [f"NSE:{s}" for s in all_symbols]
            ltp_response = kite.ltp(kite_symbols)
            for inst_key, data in ltp_response.items():
                symbol = inst_key.split(':')[1]
                if data and 'last_price' in data:
                    ltp_map[symbol] = data['last_price']
        
        # Build action lists
        exits = []
        holds_adjust = []
        entries = []
        sell_orders = []
        buy_orders = []
        
        # Process exits (full exit)
        for h in current_holdings:
            if h['symbol'] in to_exit_symbols:
                exit_value = h['quantity'] * h['last_price']
                pnl = exit_value - h['invested_amount']
                exits.append({
                    'symbol': h['symbol'],
                    'quantity': h['quantity'],
                    'entry_price': h['entry_price'],
                    'exit_price': h['last_price'],
                    'exit_value': exit_value,
                    'pnl': pnl,
                    'action': 'SELL_ALL'
                })
                sell_orders.append({
                    'symbol': h['symbol'],
                    'quantity': h['quantity'],
                    'ltp': h['last_price'],
                    'exchange': h.get('exchange', 'NSE')
                })
        
        # Process holds (adjust quantity if capital changed)
        for h in current_holdings:
            if h['symbol'] in to_hold_symbols:
                current_stock_value = h['quantity'] * h['last_price']
                target_stock_value = target_per_stock
                diff_value = target_stock_value - current_stock_value
                
                ltp = ltp_map.get(h['symbol'], h['last_price'])
                
                # Calculate quantity adjustment needed
                if abs(diff_value) > ltp:  # Significant difference (> 1 share worth)
                    if diff_value > 0:
                        # Need to buy more
                        qty_to_buy = int(diff_value / ltp)
                        if qty_to_buy > 0:
                            holds_adjust.append({
                                'symbol': h['symbol'],
                                'current_quantity': h['quantity'],
                                'adjustment_quantity': qty_to_buy,
                                'new_quantity': h['quantity'] + qty_to_buy,
                                'adjustment_value': qty_to_buy * ltp,
                                'ltp': ltp,
                                'action': 'BUY_MORE'
                            })
                            buy_orders.append({
                                'symbol': h['symbol'],
                                'quantity': qty_to_buy,
                                'ltp': ltp,
                                'exchange': h.get('exchange', 'NSE')
                            })
                    else:
                        # Need to sell some
                        qty_to_sell = int(abs(diff_value) / ltp)
                        if qty_to_sell > 0 and qty_to_sell < h['quantity']:
                            holds_adjust.append({
                                'symbol': h['symbol'],
                                'current_quantity': h['quantity'],
                                'adjustment_quantity': -qty_to_sell,
                                'new_quantity': h['quantity'] - qty_to_sell,
                                'ltp': ltp,
                                'adjustment_value': -(qty_to_sell * ltp),
                                'action': 'SELL_PARTIAL'
                            })
                            sell_orders.append({
                                'symbol': h['symbol'],
                                'quantity': qty_to_sell,
                                'ltp': ltp,
                                'exchange': h.get('exchange', 'NSE')
                            })
                else:
                    # No significant adjustment needed
                    holds_adjust.append({
                        'symbol': h['symbol'],
                        'current_quantity': h['quantity'],
                        'adjustment_quantity': 0,
                        'new_quantity': h['quantity'],
                        'adjustment_value': 0,
                        'ltp': ltp,
                        'action': 'HOLD'
                    })
        
        # Process new entries
        for stock in new_top_stocks[:15]:
            if stock['symbol'] in to_enter_symbols:
                ltp = ltp_map.get(stock['symbol'], stock['ltp'])
                if ltp > 0:
                    qty = max(1, int(target_per_stock / ltp))
                    allocated_value = qty * ltp
                    entries.append({
                        'symbol': stock['symbol'],
                        'quantity': qty,
                        'ltp': ltp,
                        'allocated_value': allocated_value,
                        'momentum_return': stock.get('ret', 0),
                        'action': 'BUY_NEW'
                    })
                    buy_orders.append({
                        'symbol': stock['symbol'],
                        'quantity': qty,
                        'ltp': ltp,
                        'exchange': 'NSE'
                    })
        
        # Calculate summary
        total_exit_value = sum(e['exit_value'] for e in exits)
        total_buy_value = sum(b['quantity'] * b['ltp'] for b in buy_orders)
        total_sell_value = sum(s['quantity'] * s['ltp'] for s in sell_orders)
        net_cash_required = total_buy_value - total_sell_value
        
        return {
            "no_changes": no_changes and abs(target - current_value) < 100,
            "current_value": current_value,
            "target_capital": target,
            "capital_change": target - current_value,
            "linked_index_symbol": linked_index_symbol,
            "exits": exits,
            "holds_adjust": holds_adjust,
            "entries": entries,
            "sell_orders": sell_orders,
            "buy_orders": buy_orders,
            "summary": {
                "total_exit_value": total_exit_value,
                "total_buy_value": total_buy_value,
                "total_sell_value": total_sell_value,
                "net_cash_required": net_cash_required,
                "exit_count": len(exits),
                "entry_count": len(entries),
                "adjust_count": len([h for h in holds_adjust if h['action'] != 'HOLD'])
            }
        }
    except Exception as e:
        logger.error(f"Rebalance calculation failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Rebalance calculation failed: {str(e)}")

class RebalanceExecutionRequest(BaseModel):
    strategy_name: str = "Nifty50 Momentum"
    tag: Optional[str] = None
    sell_orders: List[Dict[str, Any]]
    buy_orders: List[Dict[str, Any]]
    exits: List[Dict[str, Any]]
    entries: List[Dict[str, Any]]
    holds_adjust: List[Dict[str, Any]]
    use_amo: bool = False
    linked_index_symbol: str = "NIFTY 50"

@router.post("/execute-rebalance")
async def execute_rebalance_endpoint(req: RebalanceExecutionRequest):
    """
    Execute rebalance by placing basket orders and updating database state.
    Handles exits, adjustments, and new entries.
    """
    try:
        from broker_api.kite_orders import execute_basket_orders
        
        sell_results = []
        buy_results = []
        
        # Phase 1: Execute all SELL orders first
        if req.sell_orders:
            sell_basket = []
            for order in req.sell_orders:
                sell_basket.append({
                    'exchange': order.get('exchange', 'NSE'),
                    'tradingsymbol': order['symbol'],
                    'transaction_type': 'SELL',
                    'variety': 'amo' if req.use_amo else 'regular',
                    'product': 'CNC',
                    'order_type': 'LIMIT' if req.use_amo else 'MARKET',
                    'quantity': order['quantity'],
                    'price': order['ltp'] if req.use_amo else 0,
                    'validity': 'DAY',
                    'tag': f'rebalance:{req.tag or "momentum"}'
                })
            
            sell_response = await execute_basket_orders(sell_basket, all_or_none=False, dry_run=False)
            sell_results = sell_response.get('results', [])
        
        # Phase 2: Execute all BUY orders
        if req.buy_orders:
            buy_basket = []
            for order in req.buy_orders:
                buy_basket.append({
                    'exchange': order.get('exchange', 'NSE'),
                    'tradingsymbol': order['symbol'],
                    'transaction_type': 'BUY',
                    'variety': 'amo' if req.use_amo else 'regular',
                    'product': 'CNC',
                    'order_type': 'LIMIT' if req.use_amo else 'MARKET',
                    'quantity': order['quantity'],
                    'price': order['ltp'] if req.use_amo else 0,
                    'validity': 'DAY',
                    'tag': f'rebalance:{req.tag or "momentum"}'
                })
            
            buy_response = await execute_basket_orders(buy_basket, all_or_none=False, dry_run=False)
            buy_results = buy_response.get('results', [])
        
        # Phase 3: Update database state
        with get_db_cursor() as cur:
            # Map order_ids from results
            sell_order_map = {}
            for result in sell_results:
                if result.get('status') == 'success' and result.get('order_id'):
                    sell_order_map[result['tradingsymbol']] = result['order_id']
            
            buy_order_map = {}
            for result in buy_results:
                if result.get('status') == 'success' and result.get('order_id'):
                    buy_order_map[result['tradingsymbol']] = result['order_id']
            
            # Handle full exits
            for exit_action in req.exits:
                symbol = exit_action['symbol']
                order_id = sell_order_map.get(symbol)
                if order_id:
                    # Mark as exited with order_id for later verification
                    cur.execute("""
                        UPDATE investing_strategies
                        SET status = 'PENDING_EXIT', order_id = %s, exit_date = NOW(), updated_at = NOW()
                        WHERE strategy_name = %s AND tradingsymbol = %s AND status = 'ACTIVE'
                    """, (order_id, req.strategy_name, symbol))
            
            # Handle partial sells (adjustments)
            for hold_action in req.holds_adjust:
                if hold_action['action'] == 'SELL_PARTIAL':
                    symbol = hold_action['symbol']
                    new_qty = hold_action['new_quantity']
                    cur.execute("""
                        UPDATE investing_strategies
                        SET quantity = %s, updated_at = NOW()
                        WHERE strategy_name = %s AND tradingsymbol = %s AND status = 'ACTIVE'
                    """, (new_qty, req.strategy_name, symbol))
            
            # Handle additional buys on existing holdings
            for hold_action in req.holds_adjust:
                if hold_action['action'] == 'BUY_MORE':
                    symbol = hold_action['symbol']
                    new_qty = hold_action['new_quantity']
                    order_id = buy_order_map.get(symbol)
                    cur.execute("""
                        UPDATE investing_strategies
                        SET quantity = %s, order_id = %s, updated_at = NOW()
                        WHERE strategy_name = %s AND tradingsymbol = %s AND status = 'ACTIVE'
                    """, (new_qty, order_id, req.strategy_name, symbol))
            
            # Handle new entries
            tag = req.tag or f"MOMENTUM_{datetime.now().strftime('%Y_%m_%d')}"
            linked_index_token = None
            
            try:
                cur.execute(
                    "SELECT instrument_token FROM kite_indices WHERE tradingsymbol = %s LIMIT 1",
                    (req.linked_index_symbol,)
                )
                row = cur.fetchone()
                if row:
                    linked_index_token = row[0]
            except Exception:
                logger.warning(f"Could not resolve token for linked index {req.linked_index_symbol}")
            
            # Get instrument tokens for new entries
            new_symbols = [e['symbol'] for e in req.entries]
            symbol_token_map = {}
            if new_symbols:
                cur.execute(
                    "SELECT tradingsymbol, instrument_token FROM kite_instruments WHERE tradingsymbol = ANY(%s) AND exchange = 'NSE'",
                    (new_symbols,)
                )
                for row in cur.fetchall():
                    symbol_token_map[row[0]] = row[1]
            
            for entry_action in req.entries:
                symbol = entry_action['symbol']
                order_id = buy_order_map.get(symbol)
                instrument_token = symbol_token_map.get(symbol)
                
                if instrument_token:
                    invested_amt = entry_action['quantity'] * entry_action['ltp']
                    status = 'PENDING' if order_id else 'ACTIVE'
                    
                    cur.execute("""
                        INSERT INTO investing_strategies (
                            strategy_name, strategy_type, tag, instrument_token, tradingsymbol, exchange,
                            quantity, invested_amount, entry_price, last_price, pnl, pnl_percent,
                            status, linked_index_token, linked_index_symbol, entry_date, order_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                    """, (
                        req.strategy_name, 'MOMENTUM', tag, instrument_token, symbol, 'NSE',
                        entry_action['quantity'], invested_amt, entry_action['ltp'], entry_action['ltp'],
                        0.0, 0.0, status, linked_index_token, req.linked_index_symbol, order_id
                    ))
            
            get_db_connection().commit()
        
        # Compile response
        total_orders = len(sell_results) + len(buy_results)
        successful_orders = len([r for r in sell_results + buy_results if r.get('status') == 'success'])
        
        return {
            "status": "success" if successful_orders == total_orders else "partial",
            "message": f"Rebalance executed: {successful_orders}/{total_orders} orders successful",
            "sell_results": sell_results,
            "buy_results": buy_results,
            "successful_count": successful_orders,
            "total_count": total_orders
        }
        
    except Exception as e:
        logger.error(f"Rebalance execution failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Rebalance execution failed: {str(e)}")
