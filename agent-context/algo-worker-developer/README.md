# Algo Worker Developer Agent Context

This folder is a copyable context pack for agents that build external Kite Algo strategies.

Give the whole folder to an algo developer agent when you want it to write workers on another machine or in another repo. The agent does **not** need backend source access for normal strategy work; it should install the SDK from the Git tag and follow the contracts in this folder.

## Start here

1. `AGENT_PROMPT.md` — paste this into the agent as the operating instruction.
2. `ALGO_WORKER_DEVELOPMENT_GUIDE.md` — full copy of the main algo worker development guide.
3. `SDK_INSTALL.md` — how to install the SDK on remote servers.
4. `STRATEGY_LIFECYCLE.md` — how every worker should create runs, submit orders, patch risk, and exit.
5. `MARKET_DATA.md` — runtime-backed ticker, quote, tick-stream, and candle usage.
6. `REALTIME_PNL.md` — grouped realtime run-level P&L snapshot and stream usage.
7. `ORDER_CATALOG.md` — supported order fields, including AMO.
8. `LIVE_SAFETY.md` — live trading gates and what not to do.
9. `PROTECTION.md` — backend-owned protection contract usage.
10. `examples/` — runnable strategy examples.

## Canonical source files in this repo

These files remain the source of truth in the main repo:

- `docs/algo-worker-development-guide.md`
- `sdk/python/README.md`
- `sdk/python/kite_algo_worker/client.py`
- `sdk/python/kite_algo_worker/orders.py`
- `sdk/python/examples/`
- `tests/test_worker_sdk.py`
- `api/routers/algo_workers.py`
- `broker_api/kite_orders.py`
- `scripts/live_worker_e2e_validation.py`

If this context pack and the canonical files disagree, trust the canonical files.

## Current SDK release

- Package name: `kite-algo-worker`
- Version: `0.5.0`
- Git tag: `kite-algo-worker-v0.5.0`

Current hardened core surface:

- lifecycle and recovery: `health()`, `heartbeat(...)`, `create_run(...)`, `get_run(...)`
- accounting: `get_funds(...)`, `get_run_funds(...)`, `get_run_pnl(...)`, `stream_run_pnl(...)`
- execution control: `list_orders(...)`, `list_trades(...)`, `preview_order(...)`, `preview_basket(...)`, `place_order(...)`, `place_basket(...)`, `exit_run(...)`
- market data: `resolve_ticker(...)`, `search_tickers(...)`, `get_quotes(...)`, `stream_ticks(...)`, `get_candles(...)`, `stream_candles(...)`, `get_historical_candles(...)`, `get_market_snapshot(...)`
- recovery helpers: `wait_for_history(...)` and the websocket client for reconnectable streams

The certification script now prints preview output and capability flags for this surface.

Remote install:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.5.0#subdirectory=sdk/python"

This release adds:

- grouped order/trade inspection and order lifecycle helpers
- live order/basket preview APIs for sizing and charges checks
- async SDK support plus websocket clients for ticks, candles, and grouped run P&L
- safer worker ergonomics such as `ensure_run(...)`, `wait_for_history(...)`, and `live_equity_market_order(...)`
```
