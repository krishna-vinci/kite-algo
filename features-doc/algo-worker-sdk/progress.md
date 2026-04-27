# Algo Worker SDK Progress

Last updated: 2026-04-26

## Current state

- Added the first real Python SDK package in `sdk/python/kite_algo_worker`.
- Added Git-tag installation documentation for remote strategy servers. Recommended production install is `pip install "kite-algo-worker @ git+ssh://.../kite-algo.git@kite-algo-worker-v0.4.0#subdirectory=sdk/python"`.
- The SDK is intentionally thin: it only calls public `/api/algo-workers/worker/*` endpoints and does not import backend broker, database, market-runtime, or paper-runtime internals.
- `KiteAlgoWorkerClient` supports health, heartbeat, create/get run, order intent, basket intent, risk patch, and grouped exit calls.
- `KiteAlgoWorkerClient` now also supports grouped run P&L snapshots and SSE streaming via `get_run_pnl()` and `stream_run_pnl()`.
- `KiteAlgoWorkerClient` now also supports worker-safe funds/allocation snapshots via `get_funds()` and `get_run_funds()`.
- `KiteAlgoWorkerClient` now also supports generic backend-owned market-data primitives: ticker resolution/search, quote snapshots, tick SSE streams, realtime candle snapshots/SSE streams, historical candle snapshots with backend ingestion/passthrough controls, and market snapshot bundles.
- Order intent methods require explicit idempotency keys before any HTTP request is made and mirror the backend `8..160` character length contract.
- `KiteAlgoWorkerError` preserves HTTP status and response body for non-2xx responses.
- Order helpers produce `PlaceOrderRequest`-compatible dict payloads and deliberately omit broker tag/attribution fields because the backend injects attribution.

## Examples

- `sdk/python/examples/mean_reversion_worker.py`
- `sdk/python/examples/option_basket_worker.py`
- `sdk/python/examples/live_exit_preview.py`

Examples default to `dry_run` or live exit preview behavior and require explicit environment acknowledgement for live order placement.

## Documentation

- `docs/algo-worker-development-guide.md` now documents SDK install/use, environment variables, lifecycle, execution modes, runtime-backed worker market data, grouped realtime run P&L, full live order field catalog, equity/options/basket/SL examples, grouped live exit behavior, dynamic risk patching, idempotency, restart recovery, backend-vs-worker ownership, and anti-patterns.
- `sdk/python/README.md` documents local installs, Git-tag installs, release tagging, minimal usage, AMO usage, and safety rules.

## Verification

- `python3 -m pytest tests/test_worker_sdk.py -q` ✅ `23 passed`
- `python3 -m pytest tests/test_algo_worker_api.py -q` ✅ `33 passed` after adding worker market-data endpoint coverage
- `python3 -m pytest tests/test_algo_worker_api.py tests/test_worker_sdk.py -q` ✅ `54 passed`
- `python3 -m pytest tests/test_algo_worker_api.py tests/test_worker_sdk.py -q` ✅ `75 passed` after adding worker-safe historical candle endpoint, passthrough range guard, timezone validation, DB-error handling, and SDK helper
- `python3 -m pytest tests/test_live_order_attribution_gate.py tests/test_live_journal_projector.py tests/test_live_external_exit_recovery.py -q` ✅ `8 passed`
- `python3 -m pytest tests/test_algo_worker_api.py tests/test_live_order_attribution_gate.py tests/test_live_journal_projector.py tests/test_live_external_exit_recovery.py -q` ✅ previously covered worker SDK/API and related live attribution/projector/external-exit suites after adding realtime run-P&L snapshot/stream coverage
- SDK/example Python syntax checked with `ast.parse` ✅
- SDK package metadata install check in a temporary venv with `pip install --dry-run --no-deps ./sdk/python` ✅ would install `kite-algo-worker-0.4.0` after the historical-candles SDK bump

## Known gaps / next steps

- No PyPI/private-index package artifact yet. Git-tag installs are documented and are the recommended near-term remote-server distribution path once the commit/tag is pushed.
- No first-class worker decision/journal event helper yet.
- No open-run listing/recovery endpoint yet; workers must persist `strategy_run_id` and call `get_run`.
- Option-chain helpers, expiry/strike selection, Greeks, IV, and spread builders are intentionally deferred to a later namespaced option worker layer in the same SDK package.
- Worker heartbeat stale detection is token-level today. A later safety feature should persist per-run heartbeat timestamps and optional failover policy such as `observe_only`, `cancel_open_orders`, `exit_positions`, or `backend_stop_rules`.
