# Realtime Run P&L

The worker SDK now supports grouped realtime run-level P&L.

## SDK methods

```python
snapshot = client.get_run_pnl(run_id)

for update in client.stream_run_pnl(run_id, interval_seconds=1.0):
    print(update["totals"]["net_pnl"])
```

## Worker endpoints

- `GET /api/algo-workers/worker/runs/{strategy_run_id}/pnl`
- `GET /api/algo-workers/worker/runs/{strategy_run_id}/pnl/stream`

## Payload shape

```json
{
  "strategy_run_id": "run_mean_reversion_001",
  "execution_mode": "live",
  "status": "open",
  "currency": "INR",
  "totals": {
    "realized_pnl": 1250.0,
    "unrealized_pnl": -180.0,
    "gross_pnl": 1070.0,
    "charges": 42.5,
    "net_pnl": 1027.5
  },
  "legs": [],
  "position_count": 0,
  "is_realtime": true,
  "is_stale": false,
  "updated_at": "2026-04-25T12:34:56Z"
}
```

## Meaning

- `totals.realized_pnl` — grouped realized P&L for the strategy run
- `totals.unrealized_pnl` — grouped mark-to-market P&L for open legs
- `totals.gross_pnl` — realized + unrealized before charges
- `totals.charges` — grouped charges/fees tracked by backend attribution
- `totals.net_pnl` — gross minus charges
- `legs` — grouped open-leg breakdown
- `is_stale` — backend could not fully confirm one or more live leg marks/coverage

## Mode behavior

- `dry_run` → zero totals, no legs
- `paper` → grouped paper P&L and open legs
- `live` → grouped attributed live P&L with charges and live leg breakdown

## Design rule

Workers consume backend-owned grouped P&L. Workers do not compute authoritative grouped P&L themselves.
