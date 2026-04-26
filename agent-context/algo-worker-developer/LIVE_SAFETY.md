# Live Safety Rules

## Execution modes

### `dry_run`

- Does not place broker orders.
- Stores/accepts worker intent payloads so request shape and strategy decisions can be tested.

### `paper`

- Does not place broker orders.
- Proves strategy behavior, grouping, risk patching, exits, and grouped P&L.

### `live`

- Places real broker orders.
- Token must explicitly allow `live`.
- Run account scope must be a real broker account such as `kite:AB1234`.
- Run metadata must include strategy attribution fields.
- Use explicit environment acknowledgement such as `KITE_ALGO_ENABLE_LIVE=1`.

## Live run metadata

```json
{
  "strategy_family": "indicator_strategy",
  "strategy_name": "Mean Reversion",
  "entry_surface": "external_algo_worker"
}
```

Valid `strategy_family` values:

- `options_strategy`
- `indicator_strategy`
- `investment_strategy`
- `discretionary_strategy`

## Grouped live exit behavior

For live runs, `exit_run`:

1. Reconciles live broker positions first.
2. Reads attributed open live legs for that `strategy_run_id`.
3. Builds reducing market exit orders for grouped live legs.
4. Validates broker net position can cover the attributed strategy quantity.
5. Places the exit basket unless `dry_run=True`.
6. Closes the run only after projected live fills prove the strategy is flat.

If the response status is `exiting`, keep monitoring and call `exit_run` again after broker fills sync.

## What not to do

- Do not call broker APIs directly.
- Do not call database tables or backend internals.
- Do not send broker `tag`, `tags`, or `attribution` manually.
- Do not mix unrelated strategy lifecycles into one `strategy_run_id`.
- Do not use random idempotency keys for retryable order intents.
- Do not enable live before dry_run and paper behavior are proven.
- Do not assume live exit is closed until the backend confirms flat projected fills.
