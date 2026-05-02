# Live Worker Attribution and Protection Safety Design

Date: 2026-04-29

## Problem

The live drill showed a critical mismatch:

- a live worker run placed and filled a real broker order
- backend stale-worker protection triggered correctly
- but live exit logic concluded the run was already flat
- the broker still had a real open `IDEA` `MIS` position

This means the trigger path works, but the strategy-level live exposure truth used for exits is incomplete.

## Design goal

Keep `strategy_run_id` grouping as the primary truth for live worker strategies, including single-leg runs and multi-leg runs, while adding enough safety checks that backend protection never closes a run as flat when real broker exposure still exists.

## Agreed direction

1. Preserve run-level grouping as the canonical strategy model.
2. Do not replace strategy grouping with account-level broker truth.
3. Fix the live attribution chain so worker-originated orders/fills reliably map back to `strategy_run_id`.
4. Add a live safety guard so protection cannot falsely conclude a run is flat when broker/account truth still shows matching exposure.
5. Keep journaling for journaling/reporting, but do not allow live safety to depend on journaling linkage being perfect.

## Recommended implementation shape

### A. Fix primary live strategy attribution

Ensure worker-originated live orders/fills create enough durable attribution that backend logic can recover the open legs for a run.

Likely sources involved:

- `live_order_intents`
- journal linkage used by existing strategy grouping
- live order/trade inspection payload matching

### B. Add false-flat guard for live exits

When `_exit_live_worker_run(...)` sees no grouped open legs, it should not immediately close the run as flat.

It should verify whether durable runtime/broker-backed state still shows matching live exposure for that run/account before declaring flatness.

This broker/account-backed view is a safety guard, not the primary grouping model.

### C. Improve worker live inspection consistency

Worker `list_orders`, `list_trades`, and order snapshot methods should reliably surface real live broker orders/trades for the run, even when broker payloads only expose compact tags or indirect attribution.

### D. Verify protection mutability

Confirm that live worker / backend-managed strategies can still patch backend protection safely after run creation, because that is part of the intended design.

## Testing scope

1. targeted unit/integration tests for attribution and false-flat protection behavior
2. worker order/trade read-path verification
3. backend protection patch/update verification
4. controlled live-safe validation for:
   - stale-worker exit
   - stoploss/target critical paths where feasible

## Success criteria

- a live worker-filled run is never marked flat while broker exposure still exists for that run
- live worker protection can see real open legs for the run
- worker order/trade inspection shows the real live orders/trades for the run
- protection patching remains supported and behaves safely
