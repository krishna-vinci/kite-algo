from fastapi import APIRouter

from broker_api import broker_api


router = APIRouter(tags=["Market Data"])

router.add_api_route("/ltp", broker_api.get_ltp, methods=["POST"])
router.add_api_route(
    "/quote/ohlc",
    broker_api.get_ohlc,
    methods=["GET"],
    response_model=broker_api.OHLCResponse,
    summary="Get OHLC and LTP for multiple instruments",
)
