# Modular Algo Runtime Live Validation

## Scope

This runbook covers the running-stack dry-run canary workflow for the modular algo runtime and the market-hours checks required for Phase 4 validation.

## Runtime/admin endpoints and host port

- Host-exposed backend base URL: `http://localhost:18777`
- Runtime status: `GET /api/system/runtime`
- List instances: `GET /api/system/algo-runtime/instances`
- Upsert instance: `POST /api/system/algo-runtime/instances/upsert`
- Update instance status: `POST /api/system/algo-runtime/instances/{instance_id}/status`
- Refresh runtime bindings: `POST /api/system/algo-runtime/refresh`

## Prerequisites

- Running backend stack with reachable Postgres and Redis
- App auth login for `/api/auth/login` (this is separate from broker login/session)
- Valid broker account id and broker session id for canary payload config

## Running-stack dry-run canary workflow

1. Seed canary instances into the DB-backed runtime.

   ```bash
   python3 scripts/seed_algo_canaries.py --account-id kite:AB1234 --session-id test-session-id --app-username admin --app-password '<app-password>'
   ```

   Expected terminal output:

   - `canary-index-stoploss enabled`
   - `canary-bracket-stoploss enabled`
   - `canary-ema-monitor enabled`

2. Validate runtime visibility using the inspection script against the host-exposed backend port.

   ```bash
    python3 scripts/validate_algo_runtime_canaries.py --base-url http://localhost:18777 --app-username admin --app-password '<app-password>'
    ```

   Expected output checks:

   - `instance_ids` includes all three canaries
   - `instances` includes all three canary records
   - `live_worker.status` is healthy
   - live worker routing includes token `256265` and candle route `256265:5minute`

3. Confirm runtime health from the live endpoint.

   ```bash
   curl -sS http://localhost:18777/api/system/runtime --cookie 'app_access_token=<app-access-token>; app_refresh_token=<app-refresh-token>'
   ```

   Expected checks:

   - `algo_runtime.started` is `true`
   - `components.algo_runtime_live_triggers.status` is healthy
   - algo runtime status includes the canary instance ids

## Market-hours validation notes

Perform these checks during an active market window (not after-hours):

1. `GET /api/system/runtime` remains healthy and `algo_runtime.started` stays `true`.
2. `canary-index-stoploss` and `canary-bracket-stoploss` show `last_trigger.type = tick` as live ticks arrive.
3. `canary-ema-monitor` updates with `last_trigger.type = candle_close` after a fresh 5-minute candle close.
4. `last_evaluated_at` advances on all active canaries receiving their expected triggers.
5. `last_action` / `last_action_count` advance when trigger conditions are met.
6. No canary sends a real broker order because each canary payload sets `dry_run: true`.

## Post-check operator controls (optional)

Pause/resume one canary to confirm admin control path:

```bash
curl -sS -X POST http://localhost:18777/api/system/algo-runtime/instances/canary-index-stoploss/status \
  -H 'Content-Type: application/json' \
  --cookie 'app_access_token=<app-access-token>; app_refresh_token=<app-refresh-token>' \
  -d '{"status":"paused"}'
```

```bash
curl -sS -X POST http://localhost:18777/api/system/algo-runtime/instances/canary-index-stoploss/status \
  -H 'Content-Type: application/json' \
  --cookie 'app_access_token=<app-access-token>; app_refresh_token=<app-refresh-token>' \
  -d '{"status":"enabled"}'
```

## Paper-runtime canary validation (synthetic events)

Use the dedicated paper validation script to exercise account reset/upsert, paper-mode canary upsert, synthetic trigger events, and paper order/trade/position/account inspection in one operator flow.

### 1) Seed/update paper canary from DB helper

`seed_algo_canaries.py` now supports an optional paper-mode canary:

```bash
python3 scripts/seed_algo_canaries.py \
  --account-id kite:AB1234 \
  --session-id test-session-id \
  --include-paper-canary \
  --paper-account-scope kite:paper-canary \
  --app-username admin \
  --app-password '<app-password>'
```

Expected output includes a `canary-paper-bracket-stoploss` row with `paper` execution mode.

### 2) Run end-to-end paper canary validation

```bash
python3 scripts/validate_paper_runtime_canary.py \
  --base-url http://localhost:18777 \
  --app-username admin \
  --app-password '<app-password>' \
  --account-scope kite:paper-canary \
  --session-id test-session-id \
  --upsert-account \
  --reset-account \
  --force-reset \
  --upsert-paper-canary \
  --seed-synthetic-position \
  --publish-synthetic-tick \
  --confirm-live-like-state-mutation
```

This flow performs:

- paper account upsert/reset via `/api/system/paper/accounts/*`
- paper-mode canary upsert via `/api/system/algo-runtime/instances/upsert`
- optional synthetic runtime position seed into `account_positions` for triggerable exit basket behavior
- synthetic live-compatible tick publish to `market:ticks`
- inspection reads for runtime + paper account/orders/trades/positions

The confirmation flag is required whenever the script is going to mutate live-like runtime state by writing synthetic positions or publishing synthetic ticks.

If you want live-compatible (non-synthetic) validation, skip `--seed-synthetic-position` and `--publish-synthetic-tick`, then drive real ticks/positions through the running stack and rerun the script for inspection-only output.

## Validation completed on 2026-04-07

Completed in this session:

- verified host stack reachability on:
  - backend `http://localhost:18777`
  - Postgres `localhost:15432`
  - Redis `localhost:16379`
- seeded DB-backed dry-run canaries and refreshed the running runtime successfully
- confirmed runtime loaded:
  - `canary-index-stoploss`
  - `canary-bracket-stoploss`
  - `canary-ema-monitor`
  - `canary-combined-premium-stoploss`
- confirmed dependency routing included:
  - market token `256265`
  - candle pair `256265:5minute`
  - option read `NIFTY:nearest:snapshot:5:`
- confirmed live commodity tick plumbing using `GOLDPETAL26APRFUT` (`instrument_token=124842247`)
  - runtime processed real live ticks from MCX
  - `canary-goldpetal-live-tick` updated on real `tick` triggers with no runtime error
- confirmed synthetic dry-run trigger replay after market close:
  - published synthetic `tick` and `candle_close` events into Redis
  - `canary-index-stoploss` updated on `tick`
  - `canary-bracket-stoploss` updated on `tick`
  - `canary-ema-monitor` updated on `candle_close`
- confirmed options session/auth path:
  - app login succeeded via `/api/auth/login`
  - NIFTY options session started successfully
  - NIFTY options snapshot returned live session data
- confirmed the first options modular path end to end in dry-run mode using a synthetic canary account
  - `canary-options-sim-2` reached `combined_premium_profit_target`
  - runtime recorded `last_action_count = 3` (notify + order_intent + state_patch)
  - no runtime error occurred

Still pending:

- one true market-hours observation window for the intended equity/index canaries
- one real-account options-position observation window if you want live-provider proof beyond dry-run synthetic validation
