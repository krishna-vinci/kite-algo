from fastapi import APIRouter

from broker_api import broker_api


router = APIRouter(tags=["Instruments"])

router.add_api_route("/import_instruments/all", broker_api.import_all_instruments, methods=["POST"])
router.add_api_route("/instruments/nse", broker_api.get_nse_instruments, methods=["GET"])
router.add_api_route("/instruments/nfo", broker_api.get_nfo_instruments, methods=["GET"])
router.add_api_route("/instruments/commodity", broker_api.get_commodity_instruments, methods=["GET"])
router.add_api_route("/instruments/search/{symbol}", broker_api.search_instruments, methods=["GET"])
router.add_api_route("/instruments/meili/reindex", broker_api.trigger_meilisearch_reindex, methods=["POST"])
router.add_api_route("/instruments/meili/health", broker_api.get_meilisearch_health, methods=["GET"])
router.add_api_route("/instruments/populate-underlying", broker_api.populate_underlying_and_option_type, methods=["POST"])
router.add_api_route("/instruments/sync-and-reindex", broker_api.sync_and_reindex_instruments, methods=["POST"])
router.add_api_route("/instruments/fuzzy-search", broker_api.fuzzy_search_instruments, methods=["GET"])
router.add_api_route("/instruments/top-defaults", broker_api.instruments_top_defaults, methods=["GET"])
router.add_api_route("/instruments/resolve", broker_api.instruments_resolve, methods=["POST"])
