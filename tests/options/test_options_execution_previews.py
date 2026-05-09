from backend.options.execution.previews import _estimate_preview_costs


def test_estimate_preview_costs_uses_sequence_aware_margin_deltas_for_closing_orders():
    order_plan = [
        {
            "leg_id": "cover_short",
            "exchange": "NFO",
            "tradingsymbol": "SHORTLEG",
            "transaction_type": "BUY",
            "product": "MIS",
            "quantity": 75,
        },
        {
            "leg_id": "sell_long",
            "exchange": "NFO",
            "tradingsymbol": "LONGLEG",
            "transaction_type": "SELL",
            "product": "MIS",
            "quantity": 75,
        },
    ]
    legs_by_id = {
        "cover_short": {"leg_id": "cover_short", "instrument_type": "CE", "ltp": 80},
        "sell_long": {"leg_id": "sell_long", "instrument_type": "CE", "ltp": 120},
    }
    starting_positions = {
        ("NFO", "SHORTLEG", "MIS"): -75,
        ("NFO", "LONGLEG", "MIS"): 75,
    }

    margin, charges = _estimate_preview_costs(
        order_plan,
        legs_by_id=legs_by_id,
        starting_net_positions=starting_positions,
        instruments_repository=None,
        margin_engine=None,
        charges_calculator=None,
        include_margin=True,
    )

    assert margin["starting_required"] > 0
    assert margin["required"] == 0
    assert margin["final_required"] == 0
    assert [item["sequence_index"] for item in charges["per_leg"]] == [1, 2]
    assert charges["estimated"] > 0
