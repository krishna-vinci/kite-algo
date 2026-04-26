# Strategy Lifecycle

Every external strategy should follow this lifecycle.

## 1. Health check

Call `client.health()` at startup. Fail fast if the token is invalid, revoked, expired, or scoped incorrectly.

## 2. Create or recover one strategy run

Use one stable `strategy_run_id` for a strategy lifecycle.

```python
run = client.create_run(
    strategy_run_id="run_mean_reversion_20260425_001",
    template_id="mean-reversion",
    account_scope="kite:paper-a",
    execution_mode="paper",
    summary_fields=[{"key": "symbol", "label": "Symbol", "value": "INFY"}],
    risk_schema=[{"key": "stop_loss_pct", "label": "Stop loss %", "type": "number", "value": 1.2, "editable": True}],
    runtime_state={"risk": {"stop_loss_pct": 1.2}},
    metadata={"strategy_family": "indicator_strategy", "strategy_name": "Mean Reversion"},
)
```

Persist the `strategy_run_id` locally. On restart, call `client.get_run(strategy_run_id)` and resume if the run is open.

## 3. Submit idempotent order intents

Every `place_order` and `place_basket` call requires an explicit idempotency key. Keys must be 8 to 160 characters.

Good key patterns:

```text
{strategy_run_id}:entry:{symbol}:{bar_timestamp}
{strategy_run_id}:entry-basket:{structure}:{signal_id}
{strategy_run_id}:scaleout:{leg}:{signal_id}
{strategy_run_id}:exit:{reason}:{signal_id}
```

## 4. Patch risk dynamically

Use `patch_risk` when stops, targets, trailing distance, model thresholds, or exposure controls change.

```python
client.patch_risk(run_id, {"trailing_stop_pct": 0.75}, reason="model regime changed")
```

## 5. Heartbeat

Long-running workers should send heartbeats:

```python
client.heartbeat(worker_id="mean-reversion-01", metrics={"last_signal": "bar-123"})
```

## 6. Read grouped realtime P&L

Use the backend grouped run-P&L contract instead of computing authoritative P&L locally.

```python
snapshot = client.get_run_pnl(run_id)

for update in client.stream_run_pnl(run_id, interval_seconds=1.0):
    print(update["totals"]["net_pnl"])
```

## 7. Exit grouped strategy

Exit through the backend grouped exit path:

```python
client.exit_run(run_id, reason="target reached", idempotency_key=f"{run_id}:exit:target:001", dry_run=False)
```

For live exit preview:

```python
client.exit_run(run_id, reason="operator preview", idempotency_key=f"{run_id}:exit-preview:001", dry_run=True)
```
