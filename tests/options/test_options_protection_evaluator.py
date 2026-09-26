import asyncio
from datetime import datetime, timezone

from backend.options.execution.models import OptionRunState
from backend.options.protection.live_metrics import derive_live_option_protection_metrics
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


def _option_run():
    return OptionRunState(
        strategy_run_id="opt_run_metrics",
        strategy_name="metrics_guard",
        product="MIS",
        legs=[
            {
                "leg_id": "short_ce",
                "transaction_type": "SELL",
                "tradingsymbol": "NIFTY22500CE",
                "quantity": 75,
                "instrument_token": 101,
                "price": 100.0,
            },
            {
                "leg_id": "long_pe",
                "transaction_type": "BUY",
                "tradingsymbol": "NIFTY22300PE",
                "quantity": 75,
                "instrument_token": 102,
                "price": 60.0,
            },
        ],
        protection={
            "underlying": "NIFTY",
            "rules": [
                {"metric": "index_ltp", "operator": "lte", "threshold": 22000.0, "action": "exit"}
            ],
        },
    )


def test_live_option_metrics_use_signed_premium_and_own_fill_mtm():
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)

    async def index_token(_underlying, _exchange):
        return 1

    async def read_tick(token):
        ticks = {
            1: {"last_price": 21900.0, "received_at": now.isoformat()},
            101: {"last_price": 140.0, "received_at": now.isoformat()},
            102: {"last_price": 80.0, "received_at": now.isoformat()},
        }
        return ticks.get(token)

    run = _option_run()
    run.trades = [
        {
            "leg_id": "short_ce",
            "transaction_type": "SELL",
            "quantity": 75,
            "price": 100.0,
            "phase": "entry",
        },
        {
            "leg_id": "long_pe",
            "transaction_type": "BUY",
            "quantity": 75,
            "price": 60.0,
            "phase": "entry",
        },
    ]

    metrics, errors = asyncio.run(derive_live_option_protection_metrics(
        run,
        index_token_resolver=index_token,
        index_tick_loader=read_tick,
        option_tick_loader=read_tick,
        now=now,
    ))

    assert metrics["index_ltp"] == 21900.0
    # Short credit minus long debit, per one 75-quantity structure unit.
    assert metrics["combined_premium"] == 60.0
    # (60 - 40) / 40.
    assert metrics["combined_premium_change_pct"] == 50.0
    # Short lost 75*40, long gained 75*20.
    assert metrics["strategy_mtm"] == -1500.0
    assert metrics["open_quantity"] == 150
    assert errors == {}


def test_live_option_metrics_derive_net_delta_and_vega_from_a_greeks_loader():
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)

    async def index_token(_underlying, _exchange):
        return 1

    async def read_tick(token):
        ticks = {
            1: {"last_price": 21900.0, "received_at": now.isoformat()},
            101: {"last_price": 140.0, "received_at": now.isoformat()},
            102: {"last_price": 80.0, "received_at": now.isoformat()},
        }
        return ticks.get(token)

    async def greeks_loader(underlying, expiry_key):
        assert underlying == "NIFTY"
        assert expiry_key == "2026-05-26"
        return {
            "contracts": [
                {
                    "strike": 22500,
                    "ce": {
                        "tsym": "NIFTY22500CE",
                        "delta": 0.4,
                        "gamma": 0.001,
                        "theta": -2.0,
                        "vega": 8.0,
                        "updated_at": now.isoformat(),
                    },
                },
                {
                    "strike": 22300,
                    "pe": {
                        "tsym": "NIFTY22300PE",
                        "delta": -0.3,
                        "gamma": 0.0009,
                        "theta": -1.4,
                        "vega": 6.5,
                        "updated_at": now.isoformat(),
                    },
                },
            ],
        }

    run = _option_run()
    for leg in run.legs:
        leg["expiry_key"] = "2026-05-26"
    run.trades = [
        {
            "leg_id": "short_ce",
            "transaction_type": "SELL",
            "quantity": 75,
            "price": 100.0,
            "phase": "entry",
        },
        {
            "leg_id": "long_pe",
            "transaction_type": "BUY",
            "quantity": 75,
            "price": 60.0,
            "phase": "entry",
        },
    ]

    metrics, errors = asyncio.run(derive_live_option_protection_metrics(
        run,
        index_token_resolver=index_token,
        index_tick_loader=read_tick,
        option_tick_loader=read_tick,
        option_greeks_loader=greeks_loader,
        now=now,
    ))

    # Short 75 CE, long 75 PE.
    assert metrics["net_delta"] == -75.0 * 0.4 + 75.0 * -0.3
    assert metrics["net_vega"] == -75.0 * 8.0 + 75.0 * 6.5
    assert "net_delta" not in errors
    assert "net_vega" not in errors


def test_live_option_metrics_omit_net_delta_when_a_leg_greek_is_stale():
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)

    async def index_token(_underlying, _exchange):
        return 1

    async def read_tick(token):
        return {"last_price": 100.0, "received_at": now.isoformat()}

    async def greeks_loader(_underlying, _expiry_key):
        return {
            "contracts": [
                {
                    "strike": 22500,
                    "ce": {
                        "tsym": "NIFTY22500CE",
                        "delta": 0.4,
                        "gamma": 0.001,
                        "theta": -2.0,
                        "vega": 8.0,
                        # 30 seconds stale, past the 10-second freshness bound.
                        "updated_at": "2026-04-25T11:59:30+00:00",
                    },
                },
                {
                    "strike": 22300,
                    "pe": {
                        "tsym": "NIFTY22300PE",
                        "delta": -0.3,
                        "gamma": 0.0009,
                        "theta": -1.4,
                        "vega": 6.5,
                        "updated_at": now.isoformat(),
                    },
                },
            ],
        }

    run = _option_run()
    for leg in run.legs:
        leg["expiry_key"] = "2026-05-26"
    run.trades = [
        {"leg_id": "short_ce", "transaction_type": "SELL", "quantity": 75, "price": 100.0, "phase": "entry"},
        {"leg_id": "long_pe", "transaction_type": "BUY", "quantity": 75, "price": 60.0, "phase": "entry"},
    ]

    metrics, errors = asyncio.run(derive_live_option_protection_metrics(
        run,
        index_token_resolver=index_token,
        index_tick_loader=read_tick,
        option_tick_loader=read_tick,
        option_greeks_loader=greeks_loader,
        now=now,
    ))

    assert "net_delta" not in metrics
    assert "net_vega" not in metrics
    assert "older than 10 seconds" in errors["net_delta"]
    assert errors["net_vega"] == errors["net_delta"]


def test_live_option_metrics_omit_stale_market_inputs():
    now = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)

    async def index_token(_underlying, _exchange):
        return 1

    async def read_tick(token):
        if token == 1:
            return {"last_price": 21900.0, "received_at": "2026-04-25T11:59:45+00:00"}
        return {"last_price": 100.0, "received_at": now.isoformat()}

    metrics, errors = asyncio.run(derive_live_option_protection_metrics(
        _option_run(),
        index_token_resolver=index_token,
        index_tick_loader=read_tick,
        option_tick_loader=read_tick,
        now=now,
    ))

    assert "index_ltp" not in metrics
    # Index staleness omits only the dependent index metric; option premium is
    # independently derived from fresh option ticks.
    assert metrics["combined_premium"] == 0.0
    assert metrics["combined_premium_change_pct"] == -100.0
    assert metrics["strategy_mtm"] == 3000.0
    assert errors["index_ltp"].startswith("underlying index LTP")
