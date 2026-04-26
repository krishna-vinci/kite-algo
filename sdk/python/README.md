# Kite Algo Worker Python SDK

Thin Python SDK for external Kite Algo strategy workers.

The SDK only calls public Kite Algo worker API endpoints under `/api/algo-workers/worker/*`. It does not call broker internals, paper-runtime internals, market-runtime internals, or the database.

## Install from a Git tag

Recommended for remote strategy servers:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.3.0#subdirectory=sdk/python"
```

HTTPS form:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+https://github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.3.0#subdirectory=sdk/python"
```

Pin to an immutable tag in production. Avoid installing from a moving branch such as `main` on live strategy servers.

## Install from a local checkout

```bash
python3 -m pip install -e /path/to/kite-algo/sdk/python
```

## Create the release tag

After the SDK changes are committed and pushed, create and push a tag from the repository root:

```bash
git tag -a kite-algo-worker-v0.3.0 -m "kite-algo-worker v0.3.0"
git push origin kite-algo-worker-v0.3.0
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
