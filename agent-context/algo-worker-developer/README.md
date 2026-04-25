# Algo Worker Developer Agent Context

This folder is a copyable context pack for agents that build external Kite Algo strategies.

Give the whole folder to an algo developer agent when you want it to write workers on another machine or in another repo. The agent does **not** need backend source access for normal strategy work; it should install the SDK from the Git tag and follow the contracts in this folder.

## Start here

1. `AGENT_PROMPT.md` — paste this into the agent as the operating instruction.
2. `ALGO_WORKER_DEVELOPMENT_GUIDE.md` — full copy of the main algo worker development guide.
3. `SDK_INSTALL.md` — how to install the SDK on remote servers.
4. `STRATEGY_LIFECYCLE.md` — how every worker should create runs, submit orders, patch risk, and exit.
5. `REALTIME_PNL.md` — grouped realtime run-level P&L snapshot and stream usage.
6. `ORDER_CATALOG.md` — supported order fields, including AMO.
7. `LIVE_SAFETY.md` — live trading gates and what not to do.
8. `examples/` — runnable strategy examples.

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
- Version: `0.1.0`
- Git tag: `kite-algo-worker-v0.1.0`

Remote install:

```bash
python3 -m pip install \
  "kite-algo-worker @ git+ssh://git@github.com/krishna-vinci/kite-algo.git@kite-algo-worker-v0.1.0#subdirectory=sdk/python"
```
