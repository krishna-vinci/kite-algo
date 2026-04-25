import os

from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, equity_market_order


def main() -> None:
    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.getenv("KITE_ALGO_API_BASE", "http://localhost:8000"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
        )
    )
    symbol = os.getenv("KITE_ALGO_SYMBOL", "NSE:INFY")
    run_id = os.getenv("KITE_ALGO_RUN_ID", "run_realtime_market_data_demo")
    mode = os.getenv("KITE_ALGO_EXECUTION_MODE", "dry_run")

    client.create_run(
        strategy_run_id=run_id,
        template_id="realtime-market-data-demo",
        account_scope=os.getenv("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
        execution_mode=mode,
        metadata={"strategy_family": "indicator_strategy", "strategy_name": "Realtime Market Data Demo"},
    )

    instrument = client.resolve_ticker(symbol)
    candles = client.get_candles(symbol, interval="5minute", lookback=20)
    print(f"loaded {len(candles.get('candles', []))} candles for {instrument['symbol']}")

    for event in client.stream_ticks([symbol], mode="quote"):
        ticks = event.get("ticks", [])
        if not ticks:
            continue
        last_price = ticks[0].get("last_price")
        print(f"{symbol} last_price={last_price}")
        if mode == "dry_run":
            break
        tradingsymbol = str(instrument["tradingsymbol"])
        client.place_order(run_id, equity_market_order(tradingsymbol, "BUY", 1), f"{run_id}:demo-entry:001")
        break


if __name__ == "__main__":
    main()
