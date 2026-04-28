# Kite Algo Worker Python SDK

Thin Python SDK for external Kite Algo strategy workers.

The SDK only calls public Kite Algo worker API endpoints under `/api/algo-workers/worker/*`. It does not call broker internals, paper-runtime internals, market-runtime internals, or the database.

## Install from a Git tag

Recommended for remote strategy servers:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.0#subdirectory=sdk/python"
```

HTTPS form:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+https://github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.0#subdirectory=sdk/python"
```

Pin to an immutable tag in production. Avoid installing from a moving branch such as `main` on live strategy servers.

## Install variants

Choose the smallest install that matches your worker:

- base SDK: HTTP/WebSocket clients, typed models, order helpers
- `dataframe` extra: adds `pandas` + `numpy` for `candles_to_df(...)` and `ohlcv_arrays(...)`
- `indicators` extra: adds dataframe dependencies plus the indicator stack and optional `numba`

From a local checkout:

```bash
python3 -m pip install -e /path/to/kite-algo/sdk/python
python3 -m pip install -e "/path/to/kite-algo/sdk/python[dataframe]"
python3 -m pip install -e "/path/to/kite-algo/sdk/python[indicators]"
```

From a Git tag:

```bash
python3 -m pip install \
  "kite-algo-worker[dataframe] @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.0#subdirectory=sdk/python"
python3 -m pip install \
  "kite-algo-worker[indicators] @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.6.0#subdirectory=sdk/python"
```

## Create the release tag

After the SDK changes are committed and pushed, create and push a tag from the repository root:

```bash
git tag -a kite-algo-worker-v0.6.0 -m "kite-algo-worker v0.6.0"
git push origin kite-algo-worker-v0.6.0
```

Then remote servers can install the exact SDK version using the Git-tag install command above.

## Minimal usage

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="https://kite-algo.example.com",
    token="kwa_...",
))

client.health()

run = client.create_run(
    strategy_run_id="run_mean_reversion_001",
    template_id="mean-reversion",
    account_scope="kite:paper-a",
    execution_mode="paper",
    metadata={"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
)

order = equity_market_order("INFY", "BUY", 1)
client.place_order(run["strategy_run_id"], order, "run_mean_reversion_001:entry:001")

pnl = client.get_run_pnl(run["strategy_run_id"])
print(pnl["totals"]["net_pnl"])

for update in client.stream_run_pnl(run["strategy_run_id"], interval_seconds=1.0):
    print(update["totals"]["net_pnl"])
    break
```

## Dataframe shaping helpers

Use the `dataframe` or `indicators` extra when you want pandas-friendly candle shaping:

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, candles_to_df, ohlcv_arrays

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:8000",
    token="kwa_...",
))

history = client.get_historical_candles_snapshot(
    "NSE:RELIANCE",
    timeframe="5minute",
    from_date="2026-04-01T09:15:00+05:30",
    to_date="2026-04-28T15:30:00+05:30",
)

df = candles_to_df(history)
arrays = ohlcv_arrays(df)

print(df[["open", "high", "low", "close", "volume"]].tail())
print(arrays.close[-3:])
print(arrays.is_complete[-3:])
```

`candles_to_df(...)` accepts raw API payloads, typed `WorkerHistoricalCandles`, a single `WorkerCandle`, or an existing DataFrame. It sorts by candle timestamp, de-dupes duplicate timestamps by keeping the latest row, and returns a DataFrame indexed by `ts`.

`ohlcv_arrays(...)` converts the same inputs into numpy arrays for fast batch calculations or custom vectorized logic.

## Batch indicator workflow

Install with `kite-algo-worker[indicators]` to use the built-in indicator surface:

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, candles_to_df, ohlcv_arrays, ta

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:8000",
    token="kwa_...",
))

history = client.get_historical_candles_snapshot("NSE:INFY", timeframe="5minute")
df = candles_to_df(history)
arrays = ohlcv_arrays(df)

df["ema_fast"] = ta.ema(df["close"], period=9)
df["ema_slow"] = ta.ema(df["close"], period=21)
df["rsi_14"] = ta.rsi(df["close"], period=14)
df["atr_14"] = ta.atr(df, period=14)
macd = ta.macd(arrays.close, fast_period=12, slow_period=26, signal_period=9)

latest = df.iloc[-1]
print({
    "close": latest["close"],
    "ema_fast": latest["ema_fast"],
    "ema_slow": latest["ema_slow"],
    "rsi_14": latest["rsi_14"],
    "macd_histogram": macd.iloc[-1]["histogram"],
})
```

`from kite_algo_worker import ta` gives you the OpenAlgo-style facade for indicators such as `ta.sma(...)`, `ta.ema(...)`, `ta.rsi(...)`, `ta.macd(...)`, `ta.atr(...)`, `ta.supertrend(...)`, and the related crossover/highest/lowest helpers.

See `examples/batch_indicator_workflow.py` for a complete batch example.

## Live indicator engine

`LiveIndicatorEngine` keeps confirmed indicator state from historical candles and lets you evaluate provisional values on the current in-progress candle.

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, LiveIndicatorEngine, candles_to_df

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url="http://localhost:8000",
    token="kwa_...",
))

history = client.get_historical_candles_snapshot("NSE:INFY", timeframe="5minute")
engine = LiveIndicatorEngine.from_history(
    candles_to_df(history),
    indicators=[
        ("ema", {"source": "close", "period": 9}),
        ("rsi", {"source": "close", "period": 14}),
        ("macd", {"source": "close", "fast_period": 12, "slow_period": 26, "signal_period": 9}),
    ],
)

for event in client.stream_candles("NSE:INFY", interval="5minute"):
    candle = event.get("current") or event
    if not candle:
        continue
    values = engine.finalize_candle(candle) if candle.get("is_complete") else engine.update_provisional(candle)
    print(engine.metadata)
    print(values["ema"], values["rsi"], values["macd"])
```

Semantics:

- `update_provisional(candle)` computes values on the current incomplete candle without mutating confirmed history
- `finalize_candle(candle)` commits that bar into confirmed history and clears provisional state
- `rebuild(history_df, last_stream_candle=...)` reconstructs local state after a restart or reconnect

See `examples/live_indicator_engine_worker.py` for a full recovery-aware example.

## Recovery helpers

The SDK also includes polling helpers for common restart/warmup flows:

- `wait_for_history(...)`: poll until the backend returns any historical candles
- `warmup_history(...)`: poll until you have a minimum candle count for indicator warmup
- `wait_for_fresh_candle(...)`: poll until the current/latest candle is complete
- `wait_for_terminal_order_state(...)`: poll until an order reaches `COMPLETE`, `CANCELLED`, or `REJECTED`

Typical restart flow for indicator workers:

1. call `get_run(...)` to recover backend-owned run state
2. call `warmup_history(...)` or `get_historical_candles_snapshot(...)`
3. rebuild your local dataframe and `LiveIndicatorEngine`
4. reconnect `stream_ticks(...)`, `stream_candles(...)`, or `stream_run_pnl(...)`

## AMO orders

AMO is supported with `variety="amo"`:

```python
from kite_algo_worker import equity_market_order, limit_order

amo_market = equity_market_order("INFY", "BUY", 1, variety="amo")
amo_limit = limit_order("NSE", "INFY", "BUY", "CNC", 1, price=1450.0, variety="amo")
```

## Safety rules

- Use deterministic idempotency keys for every order intent.
- Start strategies in `dry_run`, then `paper`, then explicitly validated `live`.
- Do not send broker tags or attribution; the backend injects them.
- Keep tokens in environment variables or a secret manager.

## Hardened core surface

The production-safe SDK surface is centered on a few stable calls:

- lifecycle and recovery: `health()`, `heartbeat(...)`, `create_run(...)`, `get_run(...)`
- sizing and accounting: `get_funds(...)`, `get_run_funds(...)`, `get_run_pnl(...)`, `stream_run_pnl(...)`
- execution control: `list_orders(...)`, `list_trades(...)`, `preview_order(...)`, `preview_basket(...)`, `place_order(...)`, `place_basket(...)`, `exit_run(...)`
- market data: `resolve_ticker(...)`, `search_tickers(...)`, `get_quotes(...)`, `stream_ticks(...)`, `get_candles(...)`, `stream_candles(...)`, `get_historical_candles(...)`, `get_market_snapshot(...)`
- recovery helpers: `wait_for_history(...)` and the websocket client for reconnecting streams

`scripts/sdk_worker_certification.py` now reports preview output and a simple capability summary for this core surface.

## Realtime grouped run P&L

The SDK exposes grouped run-level P&L helpers:

- `get_run_pnl(strategy_run_id)`
- `stream_run_pnl(strategy_run_id, interval_seconds=1.0)`

The backend remains the source of truth for paper/live separation, attribution, charges, and grouped run state.

## Funds and allocation

Workers can read backend-owned account funds and run-level allocation usage without calling broker APIs directly:

```python
account_funds = client.get_funds(mode="paper")
run_funds = client.get_run_funds("run_mean_reversion_001")

remaining = (run_funds.get("strategy", {}).get("allocation", {}) or {}).get("remaining")
if remaining is not None and remaining < 10_000:
    print("Skip new entry; allocation cap is nearly used")
```

`get_funds()` returns account-level funds for the token's account scope. `get_run_funds()` adds current run exposure/P&L and, when the run metadata includes `allocation_cap` or `allocation_cap_inr`, returns remaining run allocation using current gross exposure as the V1 usage basis.

## Backend protection helpers

Workers can register backend-owned exposure protection when they create or update a run.

Current V1 submits a conservative attributed strategy exit when a declared backend protection rule triggers. Position rules define leg-level thresholds; they do not re-enter, roll, rebalance, or run custom worker logic.

```python
from kite_algo_worker import BackendProtection, BasketProtection, OperationalProtection, ProtectedPosition

protection = BackendProtection(
    positions=[
        ProtectedPosition(
            symbol="NSE:INFY",
            product="CNC",
            side="BUY",
            quantity=1,
            entry_price=1500,
            stoploss_pct=2,
        )
    ],
    basket=BasketProtection(stoploss_pct=4),
    operations=OperationalProtection(exit_on_worker_stale=True, worker_stale_sec=300),
)

client.create_run(
    strategy_run_id="run_mean_reversion_001",
    template_id="mean-reversion",
    account_scope="kite:paper-a",
    execution_mode="paper",
    backend_protection=protection,
)

client.update_backend_protection("run_mean_reversion_001", protection, reason="rebalance")
```

Validation mirrors the backend contract: products must be `CNC`/`MIS`/`NRML`, sides must be `BUY`/`SELL`, quantities and prices must be positive, stale-worker limits must stay between `30` and `86400` seconds, and MIS squareoff buffer must stay between `0` and `3600` seconds.

## Runtime-backed market data

The SDK exposes worker-safe market-data helpers backed by Kite Algo's Go market-runtime. Workers do not connect to broker websockets, Redis, or backend internals directly.

```python
instrument = client.resolve_ticker("NSE:INFY")
quotes = client.get_quotes(["NSE:INFY"], mode="quote")
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=50)
history = client.get_historical_candles(
    "NSE:INFY",
    timeframe="day",
    from_date="2024-01-01T00:00:00Z",
    to_date="2024-12-31T00:00:00Z",
    ingest=True,
    passthrough=False,
)

for event in client.stream_ticks(["NSE:INFY"], mode="quote"):
    for tick in event.get("ticks", []):
        print(tick["last_price"])
```

Available helpers:

- `resolve_ticker(symbol)` / `resolve_tickers([...])`
- `search_tickers(query, exchange=None, limit=20)`
- `get_quotes([...], mode="quote")`
- `stream_ticks([...], mode="quote")`
- `get_candles(symbol_or_token, interval="5minute", lookback=50)`
- `get_current_candle(symbol_or_token, interval="5minute")`
- `get_historical_candles(symbol_or_token, timeframe="day", from_date=None, to_date=None, ingest=True, passthrough=False)`
- `stream_candles(symbol_or_token, interval="5minute")`
- `get_market_snapshot(...)`

`get_historical_candles(...)` uses the backend candle facade. With `ingest=True`, the backend can trigger background ingestion for missing DB ranges. With `passthrough=True`, the backend fetches directly from Kite through the controlled system session for fresh historical data. Workers still never call Kite or the database directly.

If a worker stops, strategy decisions stop. Existing broker orders and positions remain with broker/backend accounting. Restart workers with the same `strategy_run_id`, call `get_run`, call `get_run_pnl`, rebuild local indicator state from historical candles, and reconnect SSE streams.

Options-specific helpers are intentionally deferred to a later `kite_algo_worker.options` layer inside the same SDK package.

## Examples

- `examples/mean_reversion_worker.py`: minimal safe worker lifecycle example
- `examples/realtime_market_data_worker.py`: basic runtime-backed quote/candle streaming
- `examples/batch_indicator_workflow.py`: dataframe + `ohlcv_arrays(...)` + `from kite_algo_worker import ta`
- `examples/live_indicator_engine_worker.py`: confirmed/provisional live indicator loop with restart rebuild semantics
- `examples/protected_mean_reversion_worker.py`: mean-reversion worker with backend protection
- `examples/protected_momentum_worker.py`: basket worker with backend-owned protection
- `examples/option_basket_worker.py`: option basket order shaping example
- `examples/live_exit_preview.py`: safe live exit preview without sending broker exit orders
