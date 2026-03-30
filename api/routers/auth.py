from fastapi import APIRouter

from broker_api import broker_api


router = APIRouter(tags=["Authentication"])

router.add_api_route("/login_kite", broker_api.headless_login, methods=["POST"])
router.add_api_route("/logout_kite", broker_api.logout, methods=["POST"])
router.add_api_route("/profile_kite", broker_api.profile, methods=["GET"])
router.add_api_route("/holdings_kite", broker_api.holdings, methods=["GET"])
router.add_api_route("/margins", broker_api.get_margins, methods=["GET"])
