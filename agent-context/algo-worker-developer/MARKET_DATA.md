# Worker Market Data

Algo workers consume market data through the SDK. They must not connect to broker websockets, Redis, database tables, or backend internals directly.

## Basic usage

```python
instrument = client.resolve_ticker("NSE:INFY")
quotes = client.get_quotes(["NSE:INFY"], mode="quote")
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=50)
history = client.get_historical_candles("NSE:INFY", timeframe="day", from_date="2024-01-01T00:00:00Z")

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
- `get_historical_candles(symbol_or_token, timeframe="day", from_date=None, to_date=None, ingest=True, passthrough=False)`
- `stream_candles(symbol_or_token, interval="5minute")`
- `get_market_snapshot(...)`

## Worker disconnects

If the worker process stops, new strategy decisions stop. Existing broker orders and positions remain active, and Kite Algo continues accounting, fill ingestion, grouped P&L, and grouped exits when requested.

Use `get_historical_candles(...)` to warm up investing/positional indicators. `ingest=True` can trigger backend background ingestion for missing DB ranges. `passthrough=True` asks the backend to fetch directly from Kite through the controlled system session; use it deliberately because it consumes broker historical-data quota.

Restart with the same `strategy_run_id`, call `get_run(...)`, call `get_run_pnl(...)`, rebuild indicators from historical candles, reconnect SSE streams, and resume only after recovered state is understood.

## Options

The base market-data layer is generic. Option-chain helpers, strike/expiry selection, Greeks/IV, and spread builders are intentionally deferred to a later `kite_algo_worker.options` layer in the same SDK package.
