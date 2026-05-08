from options.execution.planner import build_entry_order_plan, sort_entry_orders_buy_first


def test_sort_entry_orders_buy_first_keeps_all_buys_before_sells():
    ordered = sort_entry_orders_buy_first(
        [
            {"tradingsymbol": "SELLLEG", "transaction_type": "SELL"},
            {"tradingsymbol": "BUYLEG", "transaction_type": "BUY"},
        ]
    )

    assert [item["transaction_type"] for item in ordered] == ["BUY", "SELL"]


def test_sort_entry_orders_buy_first_preserves_relative_order_within_same_side():
    ordered = sort_entry_orders_buy_first(
        [
            {"tradingsymbol": "S1", "transaction_type": "SELL"},
            {"tradingsymbol": "B1", "transaction_type": "BUY"},
            {"tradingsymbol": "B2", "transaction_type": "BUY"},
            {"tradingsymbol": "S2", "transaction_type": "SELL"},
        ]
    )

    assert [item["tradingsymbol"] for item in ordered] == ["B1", "B2", "S1", "S2"]


def test_build_entry_order_plan_applies_run_level_product_and_buy_first():
    plan = build_entry_order_plan(
        [
            {
                "leg_id": "sell_1",
                "tradingsymbol": "S1",
                "transaction_type": "SELL",
                "quantity": 75,
                "product": "NRML",
            },
            {
                "leg_id": "buy_1",
                "tradingsymbol": "B1",
                "transaction_type": "BUY",
                "quantity": 75,
            },
        ],
        product="MIS",
    )

    assert [item["transaction_type"] for item in plan] == ["BUY", "SELL"]
    assert {item["product"] for item in plan} == {"MIS"}
