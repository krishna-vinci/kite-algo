# Backend Contract Source Files

Normal strategy agents should not need backend source access. If an implementation agent needs to inspect contracts in the Kite Algo repo, use these files:

## Worker API

`api/routers/algo_workers.py`

Important public worker endpoints:

- `GET /api/algo-workers/worker/health`
- `POST /api/algo-workers/worker/heartbeat`
- `POST /api/algo-workers/worker/runs`
- `GET /api/algo-workers/worker/runs/{strategy_run_id}`
- `POST /api/algo-workers/worker/runs/{strategy_run_id}/intents`
- `PATCH /api/algo-workers/worker/runs/{strategy_run_id}/risk`
- `POST /api/algo-workers/worker/runs/{strategy_run_id}/exit`

Important request models:

- `WorkerRunCreateRequest`
- `WorkerIntentRequest`
- `WorkerRiskPatchRequest`
- `WorkerExitRequest`
- `WorkerHeartbeatRequest`

## Broker order schema

`broker_api/kite_orders.py`

Important types:

- `PlaceOrderRequest`
- `BasketOrderRequest`
- `Exchange`
- `TransactionType`
- `Variety`
- `Product`
- `OrderType`
- `Validity`

Workers should still use SDK order helpers instead of importing backend types.

## Live validation

`scripts/live_worker_e2e_validation.py`

Use this only with explicit live environment gates and operator approval.
