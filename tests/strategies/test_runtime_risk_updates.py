from strategies.option_strategy.runtime_updates import apply_protection_patch


def test_apply_protection_patch_recompiles_preview_and_marks_editable():
    run = {
        "underlying": "NIFTY",
        "expiry": "2026-04-30",
        "selected_legs": [
            {
                "instrument_token": 111,
                "tradingsymbol": "NIFTY30APR24500CE",
                "strike": 24500,
                "option_type": "CE",
                "transaction_type": "SELL",
                "ltp": 120.0,
                "lot_size": 25,
                "lots": 1,
            },
            {
                "instrument_token": 112,
                "tradingsymbol": "NIFTY30APR24500PE",
                "strike": 24500,
                "option_type": "PE",
                "transaction_type": "SELL",
                "ltp": 118.0,
                "lot_size": 25,
                "lots": 1,
            },
        ],
        "canonical_strategy": {
            "user_intent": "short straddle",
            "inferred_structure": "short_straddle",
            "protection_preferences": {
                "combined_premium_target": 18.0,
                "combined_premium_stoploss": 32.0,
            },
        },
    }

    updated = apply_protection_patch(
        run,
        {
            "combined_premium_target": 15.0,
            "combined_premium_stoploss": 28.0,
        },
    )

    assert updated["canonical_strategy"]["inputs"]["combined_premium_target"]["value"] == 15.0
    assert any(field["key"] == "combined_premium_stoploss" and field["value"] == 28.0 for field in updated["summary_fields"])
    assert updated["capabilities"]["can_edit_risk"] is True
    assert updated["capabilities"]["can_exit_strategy"] is True
    assert "exit_strategy" in updated["capabilities"]["allowed_actions"]
    assert any(field["key"] == "combined_premium_target" for field in updated["capabilities"]["risk_schema"])
    assert updated["runtime_config"]["rules"]


def test_apply_protection_patch_allows_clearing_existing_controls():
    run = {
        "underlying": "NIFTY",
        "expiry": "2026-04-30",
        "selected_legs": [
            {
                "instrument_token": 111,
                "tradingsymbol": "NIFTY30APR24500CE",
                "strike": 24500,
                "option_type": "CE",
                "transaction_type": "SELL",
                "ltp": 120.0,
                "lot_size": 25,
                "lots": 1,
            },
            {
                "instrument_token": 112,
                "tradingsymbol": "NIFTY30APR24500PE",
                "strike": 24500,
                "option_type": "PE",
                "transaction_type": "SELL",
                "ltp": 118.0,
                "lot_size": 25,
                "lots": 1,
            },
        ],
        "canonical_strategy": {
            "user_intent": "short straddle",
            "inferred_structure": "short_straddle",
            "protection_preferences": {
                "combined_premium_target": 18.0,
                "combined_premium_stoploss": 32.0,
            },
        },
    }

    updated = apply_protection_patch(run, {"combined_premium_target": None})

    target_summary = next(field for field in updated["summary_fields"] if field["key"] == "combined_premium_target")
    assert target_summary["value"] == updated["canonical_strategy"]["inputs"]["combined_premium_target"]["value"]
    assert any(field["key"] == "combined_premium_stoploss" for field in updated["capabilities"]["risk_schema"])
