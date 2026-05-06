# Order-event replay verification runbook

Use this to verify that canonical order ingestion stays idempotent when the same real webhook / websocket payloads are replayed multiple times.

## Goal

Confirm all of these remain stable after duplicate replay:

- `order_events` / `ws_order_events` dedupe by fingerprint
- `canonical_order_events` does not multiply source-event rows
- `order_state_projection` stays stable
- `order_trade_fills` does not duplicate fills
- `account_positions` does not drift

## Safe scope

- Use one tiny real order on a liquid symbol.
- Prefer an already filled or cancelled order.
- Never replay the order itself; only replay the captured event payloads.
- If you use trade-sync verification, only feed captured broker trades back into the verifier.

## Step 1: capture the event bundle

Create a JSON file like this:

```json
[
  {
    "source": "webhook",
    "raw_table": "order_events",
    "corr_id": "order-100-webhook-1",
    "payload": { "order_id": "OID-100", "user_id": "AB1234", "status": "UPDATE" }
  },
  {
    "source": "ws",
    "raw_table": "ws_order_events",
    "corr_id": "order-100-ws-1",
    "payload": { "order_id": "OID-100", "user_id": "AB1234", "status": "UPDATE" }
  }
]
```

If you want trade-sync coverage, create a second file keyed by `order_id`:

```json
{
  "OID-100": [
    {
      "trade_id": "TR-1",
      "order_id": "OID-100",
      "transaction_type": "BUY",
      "quantity": 1,
      "price": 101.5
    }
  ]
}
```

## Step 2: run the verifier

```bash
DATABASE_URL='postgresql://...' REDIS_URL='redis://...' \
.venv/bin/python scripts/verify_order_event_replay.py \
  --events-file artifacts/order-replay/events.json \
  --trades-file artifacts/order-replay/trades.json
```

If you only want event replay and not trade-sync:

```bash
DATABASE_URL='postgresql://...' \
.venv/bin/python scripts/verify_order_event_replay.py \
  --events-file artifacts/order-replay/events.json
```

## Pass criteria

- Script exits `0`
- Final output says `"idempotent": true`
- Snapshots from the last two passes match exactly

## Helpful SQL checks

```sql
SELECT count(*) FROM canonical_order_events WHERE order_id = 'OID-100';
SELECT count(*) FROM order_trade_fills WHERE order_id = 'OID-100';
SELECT * FROM order_state_projection WHERE order_id = 'OID-100';
SELECT * FROM account_positions WHERE account_id = 'kite:AB1234';
```

## Notes

- If a replay changes counts or position rows, the issue is usually one of:
  - duplicate event fingerprinting
  - source-event key instability
  - fill application being non-idempotent
  - stale dirty flags not being cleared correctly
