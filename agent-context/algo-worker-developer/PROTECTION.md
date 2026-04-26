# Backend Protection

Use backend protection when the worker wants Kite Algo to enforce exposure exits without moving protection logic into the worker process.

Current V1 uses declared position/basket rules to submit a conservative attributed strategy exit through the backend. Do not treat backend protection as a strategy engine or leg-rebalance system.

## Use it for

- position stoploss/target/trailing percentages
- basket stoploss/target/trailing percentages
- stale-worker exits
- MIS squareoff buffer handling

## SDK shape

```python
from kite_algo_worker import BackendProtection, BasketProtection, OperationalProtection, ProtectedPosition

protection = BackendProtection(
    positions=[
        ProtectedPosition(
            symbol="NSE:INFY",
            product="CNC",
            side="BUY",
            quantity=1,
            entry_price=1500,
            stoploss_pct=2,
        )
    ],
    basket=BasketProtection(stoploss_pct=4),
    operations=OperationalProtection(exit_on_worker_stale=True, worker_stale_sec=300),
)
```

Attach it during run creation:

```python
client.create_run(
    strategy_run_id=run_id,
    template_id="mean-reversion",
    account_scope="kite:paper-a",
    execution_mode="paper",
    backend_protection=protection,
)
```

Update it later:

```python
client.update_backend_protection(run_id, protection, reason="rebalance")
```

## Rules

- product must be `CNC`, `MIS`, or `NRML`
- side must be `BUY` or `SELL`
- quantity and entry price must be positive
- stale-worker limit must be `30..86400` seconds
- MIS squareoff buffer must be `0..3600` seconds
- enabled protection must include at least one rules object

The worker still owns decisions. The backend only owns enforcement and exit submission.
