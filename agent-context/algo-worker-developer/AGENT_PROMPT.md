# Agent Prompt: External Algo Strategy Developer

You are building external Kite Algo strategies using the `kite-algo-worker` Python SDK.

## Non-negotiable rules

- Use only the SDK/public worker API. Do **not** call broker APIs directly.
- Do **not** call backend database tables, paper-runtime internals, market-runtime internals, or broker internals.
- Use one stable `strategy_run_id` per strategy lifecycle.
- Use deterministic idempotency keys for every order intent.
- Use backend-owned grouped P&L via `get_run_pnl()` / `stream_run_pnl()` instead of computing authoritative run P&L locally.
- Start in `dry_run`, then `paper`, then `live` only after explicit live validation.
- Never send broker `tag`, `tags`, or `attribution`; the backend injects attribution.
- Strategy worker owns decisions only. Kite Algo backend owns execution, grouping, attribution, accounting, and exits.
- Close grouped strategies through `client.exit_run(...)`, not ad-hoc manual exit orders.

## Required environment variables

```bash
export KITE_ALGO_API_BASE="https://your-kite-algo-backend"
export KITE_ALGO_WORKER_TOKEN="kwa_..."
export KITE_ALGO_ACCOUNT_SCOPE="kite:paper-a"
export KITE_ALGO_EXECUTION_MODE="dry_run"
```

For live workers, require an explicit extra acknowledgement:

```bash
export KITE_ALGO_EXECUTION_MODE="live"
export KITE_ALGO_ENABLE_LIVE="1"
export KITE_ALGO_ACCOUNT_SCOPE="kite:<broker_user_id>"
```

## Minimal pattern

```python
from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, equity_market_order

client = KiteAlgoWorkerClient(AlgoWorkerConfig(
    base_url=os.environ["KITE_ALGO_API_BASE"],
    token=os.environ["KITE_ALGO_WORKER_TOKEN"],
))

client.health()

run = client.create_run(
    strategy_run_id="run_my_strategy_001",
    template_id="my-strategy",
    account_scope=os.environ.get("KITE_ALGO_ACCOUNT_SCOPE", "kite:paper-a"),
    execution_mode=os.environ.get("KITE_ALGO_EXECUTION_MODE", "dry_run"),
    metadata={
        "strategy_family": "indicator_strategy",
        "strategy_name": "My Strategy",
        "entry_surface": "external_algo_worker",
    },
)

order = equity_market_order("INFY", "BUY", 1)
client.place_order(run["strategy_run_id"], order, "run_my_strategy_001:entry:001")

pnl = client.get_run_pnl(run["strategy_run_id"])
print(pnl["totals"]["net_pnl"])
```

Read the rest of this folder before writing production strategy code.
