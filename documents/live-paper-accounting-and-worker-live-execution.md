# Live/Paper Accounting And Worker Live Execution Notes

Last updated: 2026-04-24

## Purpose

This file summarizes the accounting and worker-API changes made to keep paper, live, strategy-owned, investing, and broker-external activity separated while enabling remote algo workers to place live broker orders through Kite Algo.

## Accounting spine

- Added shared execution accounting contracts in `execution_accounting/`:
  - `OrderAttribution` requires strategy, mode, account, and entry surface metadata.
  - `ExecutionCostContract` stores margin, brokerage, taxes, total charges, and quote status.
  - `signed_cash_flow()` uses one convention: buys are cash outflow, sells are cash inflow.
- Paper runtime now records structured estimated cost contracts and fee-aware journal facts.
- Live order placement now quotes broker margin/charges before submission and stores the quote with the live intent.

## Live order attribution

- Live app order placement accepts an `attribution` object on `PlaceOrderRequest`.
- The backend generates a compact `KA...` broker tag as `client_order_ref`.
- A row is persisted in `public.live_order_intents` with:
  - strategy run attribution
  - optional journal run id
  - account ref
  - idempotency key
  - broker margin/charges contract
  - broker order id when known
- If marking an intent as placed fails after broker success, the order response remains successful. Later projection can recover attribution by reading the broker tag from canonical order-event payloads.
- After broker success, the backend also seeds `order_state_projection` with `needs_reconcile=true` so the existing order runtime can pull broker trades for that order id even before a websocket event arrives.

## Live fill projection

- `journaling/live_projector.py` projects `order_trade_fills` into journal execution facts.
- Fills matching `live_order_intents` are recorded as `source_type='live_fill'`.
- Unknown fills are recorded into an imported broker activity run as `source_type='broker_import'`.
- Untagged broker-side exits attach to a strategy only when exactly one open live run matches the account/instrument/product and the fill reduces that run's net quantity. Ambiguous fills stay in broker import.
- Dirty order trade sync calls the projector best-effort after position reconciliation; projection failure does not block position updates.

## Journal separation

- Journal source types now include `live_fill` and `broker_import`.
- Backend journal summary, runs, trades, calendar, benchmark, and strategy rollup paths support strategy-family and execution-mode filters.
- Frontend journal API types now expose `StrategyFamily`, `ExecutionMode`, and reusable filter params.

## External worker live execution

- Existing `/api/algo-workers` token API now supports explicit `live` mode.
- Live worker tokens require a real broker account scope such as `kite:AB1234`.
- Live worker runs require metadata:
  - `strategy_family`
  - `strategy_name`
  - optional `entry_surface` (defaults to `algo_worker`)
- Live worker intents route through `OrdersService.place_order()` / `place_basket()` using the existing live order accounting path.
- The worker API injects attribution into each live order instead of trusting remote workers to provide broker tags.
- Live worker execution loads a Kite session for the run's broker account scope. If no session is available, live intent submission fails before placing broker orders.
- Live worker `/exit` now builds grouped reducing exit orders from attributed live journal fills, reconciles broker positions before placement, and sends the exit basket through the same live order accounting path.
- Live worker `/exit` marks a run `closed` only when projected live fills prove the run is flat. If exit orders are placed but fills are still pending, the run remains `exiting`.
- Live worker `/exit` supports `dry_run=true` to build the grouped exit plan without placing broker orders.

## Live broker validation

- Added `scripts/live_worker_e2e_validation.py` as the controlled validation harness for worker execution.
- Default validation mode is `dry_run`.
- Real broker placement requires both:
  - `--place-live-order`
  - `KITE_ALGO_CONFIRM_LIVE=YES`
- Required environment:

```bash
export KITE_ALGO_API_BASE="http://localhost:8000"
export KITE_ALGO_WORKER_TOKEN="kwa_..."
export KITE_ALGO_ACCOUNT_SCOPE="kite:AB1234"
```

Dry-run validation:

```bash
python3 scripts/live_worker_e2e_validation.py --mode dry_run
```

Controlled live validation:

```bash
KITE_ALGO_CONFIRM_LIVE=YES python3 scripts/live_worker_e2e_validation.py --mode live --place-live-order --exercise-exit
```

## Verification run

Focused verification included:

```bash
python3 -m pytest tests/test_execution_accounting_contracts.py \
  tests/test_journal_paper_costs.py \
  tests/test_live_cost_contract.py \
  tests/test_schema_live_order_attribution.py \
  tests/test_live_order_attribution_gate.py \
  tests/test_live_journal_projector.py \
  tests/test_live_external_exit_recovery.py \
  tests/test_journal_filters.py \
  tests/test_order_runtime.py \
  tests/algo_runtime/test_paper_executor.py \
  tests/test_option_strategy_router.py \
  tests/test_algo_worker_api.py -q
```

Frontend type verification:

```bash
cd frontend-next
rtk npm run typecheck
```

## Remaining hardening

- Add live worker per-token risk limits such as max order quantity, max notional, allowed exchanges/products, and daily order count.
- Add an operator kill switch for live worker tokens.
- Run live end-to-end verification against a real broker session using `scripts/live_worker_e2e_validation.py` and capture the result.
- Add worker SDK helpers so external algos do not hand-code request payloads.
