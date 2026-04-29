# Live SDK + Generic Worker Protection Drill

Date: 2026-04-29

## Goal

Run the live validation that had still been missing for:

- the worker Python SDK in real live mode
- GOLDM live market data / indicator readiness
- a real IDEA order via the worker SDK
- the current generic backend protection engine (non-options)

## Environment used

- backend: `http://localhost:18777`
- live worker token for account scope `kite:XJJ446`
- test instruments:
  - `MCX:GOLDM26MAYFUT`
  - `NSE:IDEA`

## What was tested

### 1. GOLDM live market data and indicators

Initial behavior:

- direct quote snapshot for GOLDM returned stale cached data
- direct candle snapshot returned empty/stale candles

After subscribing through worker streams:

- `stream_ticks('MCX:GOLDM26MAYFUT')` returned fresh live quote data with `is_stale=false`
- `stream_candles('MCX:GOLDM26MAYFUT')` returned a fresh 5-minute candle event with `is_stale=false`

Historical/indicator result:

- `get_historical_candles_snapshot('MCX:GOLDM26MAYFUT', timeframe='5minute')` returned 3459 candles
- latest candle timestamp observed: `2026-04-29 04:15:00+00:00`
- computed successfully:
  - `sma_5_last = 148413.8`
  - `ema_9_last = 148406.60431858644`
  - `rsi_14_last = 52.96945054746697`
  - `atr_14_last = 192.9108837250911`
- `LiveIndicatorEngine.from_history(...)` initialized successfully and reported `ready=true`

### 2. IDEA live quote readiness

Initial behavior:

- direct quote snapshot for `NSE:IDEA` returned `missing`
- direct candle snapshot for `NSE:IDEA` returned empty/stale

After subscribing through worker streams:

- `stream_ticks('NSE:IDEA')` returned fresh live quote data with `last_price=10.31` and `is_stale=false`

Observed limitation:

- candle streaming for `NSE:IDEA` still came back stale/empty during this drill even though tick streaming was fresh

## Real live worker order drill

### Protected run created

Run ID:

- `live_protection_idea_1777436466`

Run protection:

- backend protection enabled
- operations only:
  - `exit_on_worker_stale=true`
  - `worker_stale_sec=30`

### Preview

Order preview succeeded for:

- symbol: `NSE:IDEA`
- side: `BUY`
- quantity: `1`
- product: `MIS`
- order type: `MARKET`

Preview returned broker-quoted cost contract successfully.

### Placement

Placement response:

- accepted by worker API
- broker order id returned: `260429150367762`

Direct broker verification later confirmed:

- order status: `COMPLETE`
- effective order type at broker: `LIMIT`
- filled quantity: `1`
- average price: `10.34`

Broker trade verification later confirmed:

- trade id: `202427140`
- buy 1 `IDEA` `MIS` at `10.34`

## Generic backend protection live result

### What we expected

Because the run had:

- `exit_on_worker_stale=true`
- `worker_stale_sec=30`

the backend should, after heartbeat staleness, detect the open run exposure and submit a protective exit.

### What actually happened

The backend protection engine did trigger:

- `triggered_rule = worker_stale`
- trigger detail showed `heartbeat_age_sec = 31`

But the exit result was wrong.

The run was closed with:

- `exit_reason = backend_protection:worker_stale`
- message: `Live worker run is already flat`

That was false.

At that same stage, backend state still showed a real open broker position:

- `account_positions`
  - `tradingsymbol = IDEA`
  - `product = MIS`
  - `net_quantity = 1`
  - `average_price = 10.34`

So the generic backend protection did **not** actually protect the real filled worker position.

## Manual flatten done to remove live risk

To safely flatten the real open broker position, a second run was used:

- `live_flatten_idea_1777436676`

Flatten order:

- `SELL 1 IDEA MIS`
- broker order id: `260429150386155`

Direct broker verification later confirmed:

- order status: `COMPLETE`
- filled quantity: `1`
- average price: `10.33`

Broker trade verification later confirmed:

- trade id: `202532605`
- sell 1 `IDEA` `MIS` at `10.33`

Final broker position check confirmed flat:

- `quantity = 0`

Approximate realized result from the round-trip:

- buy `10.34`, sell `10.33`
- about `-0.01` before charges

## Additional live gaps exposed

### 1. Worker order/trade inspection is broken for real live worker orders

For the real broker order id `260429150367762`:

- `GET /worker/orders?strategy_run_id=...` returned `orders: []`
- `GET /worker/trades?strategy_run_id=...` returned `trades: []`
- `GET /worker/orders/{order_id}` returned `404 Order not found for strategy run`

Likely reason from code inspection:

- live worker order/trade inspection filters broker payloads using `_payload_matches_strategy_run(...)`
- that matcher only checks:
  - top-level `strategy_run_id`
  - `attribution.strategy_run_id`
  - `meta.strategy_run_id`
- real broker `orders()` / `trades()` payloads for these fills did not include those fields in a way the worker read endpoints could match

### 2. Strategy-open-leg detection is broken for this live worker path

The live exit path uses:

- `_list_live_strategy_open_legs_sync(...)`

That function depends on:

- `journal_source_links`
- `journal_execution_facts`

During this drill:

- no `journal_source_links` rows existed for:
  - `live_protection_idea_1777436466`
  - `live_flatten_idea_1777436676`
- no `journal_execution_facts` rows existed for the test order ids

So when the stale-worker protection fired, the backend found no strategy legs and incorrectly treated the run as already flat.

### 3. Order projection completion is stale/incomplete

Even after both broker orders were confirmed `COMPLETE` and both trade fills existed, `order_state_projection` still showed:

- buy order `260429150367762` -> `latest_status = PLACED`
- sell order `260429150386155` -> `latest_status = PLACED`

So order projection/status finalization is also not reliable yet for this live worker path.

## Final conclusion

### What passed

- worker SDK can talk to the live backend
- GOLDM live stream subscription works
- GOLDM historical candle access works
- worker-side indicators work on real historical data
- live order preview works
- live worker order placement reaches the broker successfully
- manual live flatten through the SDK also reaches the broker successfully

### What failed

- live worker order/trade inspection is not reliable
- live worker fill attribution into journal/run linkage is not reliable
- generic backend stale-worker protection is **not yet live-safe** for real worker-filled positions
- live order projection final statuses are not converging correctly for these worker fills

## Practical verdict

The SDK live order path is real.

The current generic backend protection engine should **not** yet be trusted as the only live fail-safe for worker strategies until the strategy-to-fill attribution and open-leg detection path is fixed.

## Most likely next fix area

Focus first on the live worker attribution chain:

1. ensure live worker fills create the expected `journal_source_links` / `journal_execution_facts` for `strategy_run_id`
2. make worker order/trade inspection match real broker payloads reliably
3. make order projection advance to terminal broker states for worker-originated live orders
4. re-run the exact stale-worker protection drill on a tiny live position

---

## Same-day follow-up fixes and re-validation

After the first drill, the backend was changed and retested.

### What was fixed

1. live worker open-leg detection for exits/protection was decoupled from journal tables and now derives primary run truth from:
   - `live_order_intents`
   - `order_trade_fills`
   - broker/account position truth
2. worker `list_orders`, `list_trades`, and order snapshot/history now match live run activity via durable attribution:
   - broker order ids
   - client-order-ref tags
   - canonical order-event tag recovery when broker order id backfill lags
3. live worker P&L now has a fallback path that can use attribution/open-leg truth even when journal linkage is missing.
4. backend-generated live exit basket orders now include `market_protection=-1`, fixing the real live rejection that happened during testing.
5. protection runtime now supports a deferred exit result instead of forcing terminal submission state when exposure cannot yet be safely flattened.
6. live exit safety gained an additional direct-broker guard so a run is not falsely marked flat just because local attribution/projection tables are lagging.

### Automated verification after fixes

- `python -m unittest tests.test_algo_worker_api tests.test_worker_protection_runtime -q`
  - `74 tests OK`
- `pytest tests/test_worker_protection.py tests/test_live_journal_projector.py tests/test_live_external_exit_recovery.py -q`
  - `16 passed`

### Live re-validation after fixes

#### 1. Historical/indicator/live-market validation still good

- GOLDM live stream + candles + indicators remained good.

#### 2. Protection patch mutability works live

Run:

- `live_protection_patch_1777440853`

Validated:

- live run creation with backend protection
- live `PATCH /protection` behavior via SDK
- generation increment from `1 -> 2`
- version increment from `1 -> 2`
- updated basket + worker-stale settings persisted correctly

#### 3. Direct live grouped exit works after fix

Run:

- `live_final_exit_1777441545`

Observed:

- entry buy order filled
- grouped exit order placed successfully by backend
- exit order filled successfully
- worker run closed correctly
- worker order/trade inspection returned both entry and exit records
- broker account finished flat

#### 4. Full stale-worker auto-exit now works live

Run:

- `live_stale_auto_1777441609`

Protection config:

- `exit_on_worker_stale=true`
- `worker_stale_sec=30`

Observed:

- entry buy filled
- backend protection triggered at `heartbeat_age_sec=31`
- backend created attributed exit order with `market_protection=-1`
- exit order filled successfully
- run moved `open -> exiting -> closed`
- worker order/trade inspection returned both entry and exit
- broker account finished flat

This is the strongest validation of the generic backend worker protection path so far.

### Updated practical verdict

The original failure mode from the first live drill was real, but the same-day fixes materially improved the system.

### Confidence scores

These are honest confidence estimates, not marketing claims.

- worker market/history/indicator SDK surface: **93/100**
- live preview/place/inspect worker order flow: **94/100**
- generic backend stale-worker protection: **92/100**
- live backend protection patch/update mutability: **92/100**
- generic stoploss/target logic overall: **84/100**
  - logic is well covered by automated tests
  - but not fully live-forced in market conditions during this session
- overall readiness for controlled public strategy-development beta: **90/100**

### Why not 100/100

Remaining non-blocking gaps:

- stoploss/target live triggering was not force-tested against real market movement in this session
- tag-based attribution recovery is still somewhat more fragile than a perfect native broker strategy-id field would be
- some live accounting/read-model paths can still lag transiently during reconciliation windows

So this is strong enough for a controlled beta, but not honest to call 100/100 yet.
