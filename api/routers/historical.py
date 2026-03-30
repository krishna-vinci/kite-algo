from fastapi import APIRouter

from broker_api import broker_api


router = APIRouter(tags=["Historical Data"])

router.add_api_route("/clear_historical_data", broker_api.clear_historical_data, methods=["POST"])
router.add_api_route("/fetch_historical_data", broker_api.fetch_historical_data_initial, methods=["POST"])
router.add_api_route("/update_historical_data", broker_api.update_historical_data, methods=["POST"])
router.add_api_route("/historical_data_progress", broker_api.get_historical_data_progress, methods=["GET"])
router.add_api_route("/update_indices_from_instruments", broker_api.update_indices_from_instruments, methods=["POST"])
router.add_api_route("/fetch_indices_historical_data", broker_api.fetch_indices_historical_data, methods=["POST"])
router.add_api_route("/update_indices_historical_data", broker_api.update_indices_historical_data, methods=["POST"])
