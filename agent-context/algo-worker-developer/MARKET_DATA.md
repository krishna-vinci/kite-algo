# Worker Market Data

Algo workers consume market data through the SDK. They must not connect to broker websockets, Redis, database tables, or backend internals directly.

## Basic usage

```python
instrument = client.resolve_ticker("NSE:INFY")
quotes = client.get_quotes(["NSE:INFY"], mode="quote")
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=50)

for event in client.stream_ticks(["NSE:INFY"], mode="quote"):
    for tick in event.get("ticks", []):
        print(tick["last_price"])
```

## SDK helpers

- `resolve_ticker(symbol)`
- `resolve_tickers([...])`
- `search_tickers(query, exchange=None, limit=20)`
- `get_quotes([...], mode="ltp|quote|full")`
- `stream_ticks([...], mode="ltp|quote|full")`
- `get_candles(symbol_or_token, interval="5minute", lookback=50)`
- `get_current_candle(symbol_or_token, interval="5minute")`
- `stream_candles(symbol_or_token, interval="5minute")`
- `get_market_snapshot(...)`

## Worker disconnects

If the worker process stops, new strategy decisions stop. Existing broker orders and positions remain active, and Kite Algo continues accounting, fill ingestion, grouped P&L, and grouped exits when requested.

Restart with the same `strategy_run_id`, call `get_run(...)`, call `get_run_pnl(...)`, rebuild indicators from candles, reconnect streams, and resume only after recovered state is understood.

## Options

The base market-data layer is generic. Option-chain helpers, strike/expiry selection, Greeks/IV, and spread builders are intentionally deferred to a later `kite_algo_worker.options` layer in the same SDK package.
