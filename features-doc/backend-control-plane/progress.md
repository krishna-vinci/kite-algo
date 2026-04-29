# Backend Control Plane Progress

Date: 2026-04-29

## Status

- Backend control-plane Phases 1-3 are implemented on the current branch.
- Frontend trading-console normalization/panel work is also landed against the current backend contract.

## Implemented endpoints

- `GET /api/control/strategy-positions`
  - Authenticated operator snapshot.
  - Reuses paper runtime summaries, non-paper algo-worker runs/P&L, and heartbeat-derived worker health.
  - Returns a stable manual/unattributed exposure bucket; live broker account is inferred from the current Kite session when `broker_account_id` is not supplied.
  - Excludes or quantity-adjusts broker positions already represented by live strategy rows to avoid hiding residual manual exposure or double-counting known strategy exposure.
- `POST /api/control/strategies/{strategy_run_id}/exit`
  - Reuses existing paper-runtime exit flow for paper strategies.
  - Reuses existing worker exit path for worker-backed runs.
  - Supports control-plane `dry_run` handling.
- `POST /api/control/strategies/{strategy_run_id}/cancel-orders`
  - Endpoint is present and authenticated.
  - Intentionally returns HTTP `409` with a deterministic disabled reason until safe strategy-scoped broker order attribution exists.
- `POST /api/control/reconcile`
  - Delegates to the existing broker realtime-position reconcile path.

## Protection adapters

- `option_runtime`
  - Derived from `option_strategy_store.get_strategy_run(...)` plus `algo_runtime_service.status()`.
  - Exposes lifecycle/error metadata and canonical option protection rule counts/preferences.
- `investing_runtime`
  - Derived from investing holdings summaries in `investing_strategies` via `InvestingProtectionRepository`.
  - Exposes active holding count, pending exits, total P&L, and worst P&L percent.
- Fallback behavior
  - Uses metadata-supplied protection when present.
  - Otherwise returns `source=none`, `status=unknown`.
  - Adapter failures degrade to per-strategy `status=error` protection state instead of failing the whole snapshot.

## Centralized backend worker protection

- Added versioned `runtime_state.backend_protection` contract for algo-worker runs.
- Added `PATCH /api/algo-workers/worker/runs/{strategy_run_id}/protection` for rebalance-safe protection updates.
- Added percent-based position and basket protection evaluation for stoploss, target, and trailing rules.
- V1 submits conservative attributed strategy exits for triggered backend protection rules; position rules provide leg-specific thresholds, not leg-only execution yet.
- Added optional worker-stale exit and configurable MIS squareoff-buffer protection.
- Added a lightweight backend evaluator loop guarded by `WORKER_PROTECTION_ENABLED` and `WORKER_PROTECTION_INTERVAL_SECONDS`.
- Control plane now surfaces `backend_worker_protection` details with generation, basket P&L percent, trigger state, and action.
- Added Python SDK helper models and update API for declaring backend protection from external workers.
- Added SDK docs/examples and agent context for protected worker development.

## Verification

- Backend targeted suite:
  - `python3 -m pytest tests/test_control_plane_api.py tests/test_control_plane_protection.py tests/test_algo_worker_api.py tests/test_live_order_attribution_gate.py tests/test_live_journal_projector.py tests/test_live_external_exit_recovery.py -q`
  - Latest result: `59 passed, 1 warning`.
- Frontend targeted tests:
  - `cd frontend-next && npm test -- features/trading/api.test.ts features/trading/components/control-plane-panel.test.tsx features/trading/components/trading-console-page.test.tsx`
  - Reported result from subagent run: passed.
- Frontend typecheck:
  - `cd frontend-next && npm run typecheck`
  - Reported result from subagent run: passed.
- Centralized worker protection targeted suites:
  - `python3 -m pytest tests/test_worker_protection.py tests/test_worker_protection_runtime.py tests/test_algo_worker_api.py::AlgoWorkerProtectionApiTests tests/test_control_plane_protection.py -q`
  - Latest result: `44 passed, 1 warning`.
  - Full related backend/SDK suite: `python3 -m pytest tests/test_worker_protection.py tests/test_worker_protection_runtime.py tests/test_algo_worker_api.py tests/test_control_plane_protection.py tests/test_control_plane_api.py tests/test_worker_sdk.py -q`
  - Latest result: `102 passed, 1 warning`.
  - `python3 -m pytest tests/test_worker_sdk.py -q`
  - Latest result: `23 passed, 1 warning`.
  - `npm --prefix frontend-next test -- features/trading/components/control-plane-panel.test.tsx`
  - Latest result: `1 passed`.
- Live drill on 2026-04-29:
  - GOLDM worker market data became fresh once subscribed through the worker market stream; 5-minute historical candles and indicator calculations (`sma`, `ema`, `rsi`, `atr`, `LiveIndicatorEngine`) worked for `MCX:GOLDM26MAYFUT`.
  - A live `NSE:IDEA` `MIS` buy intent from worker run `live_protection_idea_1777436466` reached the broker and filled (`order_id=260429150367762`, buy 1 @ 10.34).
  - Backend worker-stale protection triggered after ~31s and closed the run, but incorrectly concluded the run was already flat instead of placing/confirming a protective exit.
  - Manual flatten via worker run `live_flatten_idea_1777436676` also reached the broker and filled (`order_id=260429150386155`, sell 1 @ 10.33), restoring broker-side flatness.
  - During the drill, worker `list_orders` / `list_trades` stayed empty and `get_order_snapshot` returned 404 for the real broker order IDs.
  - same-day follow-up fixes corrected the live worker attribution/safety chain:
    - live open-leg detection no longer depends on journal tables for worker safety
    - worker order/trade inspection now matches by durable order id / client-order-ref tag attribution, including canonical event tag recovery when direct backfill lags
    - live worker P&L gained an attribution fallback when journal linkage is missing
    - backend-generated live exit basket orders now include `market_protection=-1`
    - protection runtime now understands deferred exits instead of forcing a false terminal submission state
  - live re-validation after fixes succeeded:
    - grouped live entry/exit round-trip completed successfully on `live_final_exit_1777441545`
    - full backend stale-worker auto-exit completed successfully on `live_stale_auto_1777441609` with real broker buy + backend-triggered sell + final flat account state

## Current limitations

- Backend does not mirror strategy logic or create entries, re-entries, rolls, strike selection, rebalance actions, or ML decisions.
- Generic worker backend protection is now present for declarative exposure exits only; it still does not mirror worker strategy logic.
- Live worker protection is not yet trustworthy for real broker fills until attribution/journal linkage is fixed:
- Journaling can still lag or miss worker-fill linkage, but worker safety no longer depends on journal tables for open-leg detection.
- Generic worker stoploss/target logic is still primarily live-verified only for stale-worker protection; stoploss/target live-forced market validation remains pending.
- Strategy-scoped cancel remains disabled until open broker orders can be attributed safely by `strategy_run_id`.
- Manual/unattributed residual P&L for partially overlapping live strategy positions is prorated from broker position P&L because broker realtime positions are account-level, not attribution-level.
