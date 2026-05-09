from backend.options.protection.evaluator import evaluate_option_rules
from backend.options.protection.exit_builder import build_grouped_exit_orders


def test_evaluate_option_rules_returns_first_matching_rule_by_precedence():
    rule = evaluate_option_rules(
        metrics={"combined_premium_points": 310, "basket_mtm_rupees": -1200},
        rules=[
            {
                "key": "target",
                "metric": "combined_premium_points",
                "operator": "lte",
                "threshold": 200,
                "role": "profit_target",
            },
            {
                "key": "stop",
                "metric": "basket_mtm_rupees",
                "operator": "lte",
                "threshold": -1000,
                "role": "hard_stop",
            },
        ],
        precedence=["hard_stop", "profit_target"],
    )

    assert rule is not None
    assert rule["key"] == "stop"


def test_build_grouped_exit_orders_maps_long_and_short_positions_to_exits():
    orders, skipped = build_grouped_exit_orders(
        [
            {
                "exchange": "NFO",
                "tradingsymbol": "NIFTY26MAY25000CE",
                "net_quantity": 75,
                "product": "MIS",
            },
            {
                "exchange": "NFO",
                "tradingsymbol": "NIFTY26MAY25000PE",
                "net_quantity": -75,
                "product": "MIS",
            },
            {
                "exchange": "NFO",
                "tradingsymbol": "NIFTY26MAY25100CE",
                "net_quantity": 0,
                "product": "MIS",
            },
        ]
    )

    assert skipped == 1
    assert orders == [
        {
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26MAY25000CE",
            "transaction_type": "SELL",
            "variety": "regular",
            "product": "MIS",
            "order_type": "MARKET",
            "quantity": 75,
        },
        {
            "exchange": "NFO",
            "tradingsymbol": "NIFTY26MAY25000PE",
            "transaction_type": "BUY",
            "variety": "regular",
            "product": "MIS",
            "order_type": "MARKET",
            "quantity": 75,
        },
    ]
