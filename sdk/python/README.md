# Kite Algo Worker Python SDK

Thin Python SDK for external Kite Algo strategy workers.

The SDK only calls public Kite Algo worker API endpoints under `/api/algo-workers/worker/*`. It does not call broker internals, paper-runtime internals, market-runtime internals, or the database.

## Install from a Git tag

Recommended for remote strategy servers:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.1.0#subdirectory=sdk/python"
```

HTTPS form:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+https://github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.1.0#subdirectory=sdk/python"
```

Pin to an immutable tag in production. Avoid installing from a moving branch such as `main` on live strategy servers.

## Install from a local checkout

```bash
python3 -m pip install -e /path/to/kite-algo/sdk/python
```

## Create the release tag

After the SDK changes are committed and pushed, create and push a tag from the repository root:

```bash
git tag -a kite-algo-worker-v0.1.0 -m "kite-algo-worker v0.1.0"
git push origin kite-algo-worker-v0.1.0
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

## Realtime grouped run P&L

The SDK exposes grouped run-level P&L helpers:

- `get_run_pnl(strategy_run_id)`
- `stream_run_pnl(strategy_run_id, interval_seconds=1.0)`

The backend remains the source of truth for paper/live separation, attribution, charges, and grouped run state.

## Runtime-backed market data

The SDK exposes worker-safe market-data helpers backed by Kite Algo's Go market-runtime. Workers do not connect to broker websockets, Redis, or backend internals directly.

```python
instrument = client.resolve_ticker("NSE:INFY")
quotes = client.get_quotes(["NSE:INFY"], mode="quote")
candles = client.get_candles("NSE:INFY", interval="5minute", lookback=50)

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
- `stream_candles(symbol_or_token, interval="5minute")`
- `get_market_snapshot(...)`

If a worker stops, strategy decisions stop. Existing broker orders and positions remain with broker/backend accounting. Restart workers with the same `strategy_run_id`, call `get_run`, call `get_run_pnl`, rebuild local indicator state from candles, and reconnect streams.

Options-specific helpers are intentionally deferred to a later `kite_algo_worker.options` layer inside the same SDK package.
