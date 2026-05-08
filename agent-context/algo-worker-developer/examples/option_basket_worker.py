#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Safe option basket worker example.

The default mode is `dry_run`. Live mode requires KITE_ALGO_ENABLE_LIVE=1.
"""

from __future__ import annotations

import os

from kite_algo_worker import (
    AlgoWorkerConfig,
    KiteAlgoWorkerClient,
    RunConfig,
    SpreadLegSelection,
    SpreadSpec,
    resolve_spread,
)


def main() -> None:
    execution_mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run").lower()
    if execution_mode == "live" and os.getenv("KITE_ALGO_ENABLE_LIVE") != "1":
        raise SystemExit("Refusing live mode. Set KITE_ALGO_ENABLE_LIVE=1 to acknowledge real broker orders.")

    strategy_run_id = os.getenv("KITE_ALGO_RUN_ID", "run_option_basket_demo_v1")
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )

    underlying = os.getenv("KITE_ALGO_UNDERLYING", "NIFTY")
    spec = SpreadSpec(
        spread_type="vertical_call_spread",
        expiry=os.getenv("KITE_ALGO_EXPIRY", "current_week"),
        legs=[
            SpreadLegSelection(
                selection={"option_type": "CE", "moneyness": "ATM"},
                transaction_type="SELL",
                lots=int(os.getenv("KITE_ALGO_LOTS", "1")),
            ),
            SpreadLegSelection(
                selection={"option_type": "CE", "moneyness": "+1_strike"},
                transaction_type="BUY",
                lots=int(os.getenv("KITE_ALGO_LOTS", "1")),
            ),
        ],
    )
    legs = resolve_spread(client.options, underlying=underlying, product="MIS", spec=spec)

    config = RunConfig(
        strategy_run_id=strategy_run_id,
        template_id="option-basket-demo",
        account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
        execution_mode=execution_mode,
        metadata={
            "strategy_family": "options_strategy",
            "strategy_name": "Option Basket Demo",
            "entry_surface": "external_algo_worker",
        },
    )

    with client.run(config) as run:
        safety = run.safety_check()
        if not safety.can_trade:
            return
        option_run = client.options.create_run(
            strategy_name="Option Basket Demo",
            product="MIS",
            legs=[leg.model_dump(exclude_none=True) for leg in legs],
        )
        if execution_mode != "live" or os.getenv("KITE_ALGO_PLACE_LIVE_BASKET", "0") == "1":
            client.options.enter(
                option_run["strategy_run_id"],
                safety_token=safety.safety_token,
                session_nonce=run.session_nonce,
            )


if __name__ == "__main__":
    main()
