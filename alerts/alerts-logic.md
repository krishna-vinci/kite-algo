# Alert System — Minimal Architecture & Logic (Concise)

Purpose: document the alert system we built end-to-end so the same patterns can be reused for algo trading and other real-time workflows.

## 1) Components (files and roles)

Backend
- WebSocket tick ingest and token subscription: [broker_api/websocket_manager.py](broker_api/websocket_manager.py)
- Alert engine (load, evaluate, trigger, persist): [alerts/engine.py](alerts/engine.py)
  - Evaluation and trigger path (example refs):
    - [Python.AlertsEngine._evaluate_alert()](alerts/engine.py:218)
    - [Python.AlertsEngine._handle_trigger()](alerts/engine.py:221)
    - Refresh loop and subscription reconciliation:
      - [Python.AlertsEngine._refresh_active_alerts_and_subscriptions()](alerts/engine.py:331)
      - [Python.AlertsEngine._handle_ws_reconnect()](alerts/engine.py:431)
- Alerts HTTP API and SSE stream:
  - Router and endpoints: [broker_api/alerts_router.py](broker_api/alerts_router.py)
  - SSE stream handler: [Python.sse_alerts_events()](broker_api/alerts_router.py:469)
- Real-time message bus (Redis Pub/Sub helpers): [broker_api/redis_events.py](broker_api/redis_events.py)
  - [Python.get_redis()](broker_api/redis_events.py:15)
  - [Python.publish_event()](broker_api/redis_events.py:35)
  - [Python.pubsub_iter()](broker_api/redis_events.py:55)
- Push notifications (ntfy): [broker_api/ntfy.py](broker_api/ntfy.py)
  - [Python.notify_alert_triggered()](broker_api/ntfy.py:1)
- Schema and persistence: [schema.sql](schema.sql)

Frontend
- Real-time alerts table UI: [frontend/src/lib/components/alerts/AlertsTable.svelte](frontend/src/lib/components/alerts/AlertsTable.svelte)
  - Initial load: [TypeScript.load()](frontend/src/lib/components/alerts/AlertsTable.svelte:51)
  - Refresh fetch: [TypeScript.refresh()](frontend/src/lib/components/alerts/AlertsTable.svelte:99)
  - Mount (SSE subscribe + polling): [Svelte.onMount()](frontend/src/lib/components/alerts/AlertsTable.svelte:236)
- Alert creation dialog (LTP + NFO subscription fix): [frontend/src/lib/components/alerts/CreateAlertDialog.svelte](frontend/src/lib/components/alerts/CreateAlertDialog.svelte)
- Marketwatch store (subscribe by token): [frontend/src/lib/stores/marketwatch.ts](frontend/src/lib/stores/marketwatch.ts)
  - [TypeScript.subscribeToInstruments()](frontend/src/lib/stores/marketwatch.ts:180)

## 2) Data Flow (end-to-end)

1. Ticks in: broker WS delivers ticks to [broker_api/websocket_manager.py](broker_api/websocket_manager.py).  
2. Active alerts: engine loads/refreshes enabled alerts from DB and indexes them by instrument_token in [alerts/engine.py](alerts/engine.py).  
3. Subscriptions: engine ensures tokens of active alerts are subscribed; during WS reconnect it reconciles to resubscribe missing tokens.  
4. Evaluate on tick: for each tick, the engine evaluates all alerts for that token using crossing logic (below).  
5. On trigger: engine persists state (status=triggered, timestamps), publishes a realtime event to Redis (for SSE), and schedules a non-blocking ntfy push.
6. UI updates:
   - SSE: frontend’s alerts table listens to /alerts/events and updates the affected row instantly.
   - Banner: a shadcn Alert banner (success style) is displayed for ~5s.
   - Polling: a 5s polling refresh remains as fallback (e.g., transient SSE issues).

## 3) Core Trigger Logic

Inputs (per alert)
- instrument_token (int), comparator (gt/gte/lt/lte)
- absolute_target (float | null)
- percent_target (float | null) — converted each eval from baseline_price
- baseline_price (float), status, triggered_at

Seeding and state
- The engine maintains per-alert prev_price.
- Seed prev_price using: last cached LTP if available, else baseline_price, else current tick (no crossing on equal sides).

Percent → absolute
- For percent alerts, compute absolute target every evaluation: target = f(baseline_price, percent, comparator). No drift from current LTP.

Crossings (prev = previous LTP, curr = current LTP, target = absolute)
- gt:  if prev <= target and curr >  target, trigger
- gte: if prev <  target and curr >= target, trigger
- lt:  if prev >= target and curr <  target, trigger
- lte: if prev >  target and curr <= target, trigger

Reliability and correctness
- No “skip first tick” blind spot: first tick is evaluated against an effective prev (seeded).
- On deactivation, the engine purges in-memory state so reactivation re-seeds correctly.
- The refresh loop is exception‑guarded (won’t die silently).
- On reconnect, the engine reconciles subscriptions to avoid missed evaluations.

## 4) Realtime & Notifications

Redis Pub/Sub + SSE (UI)
- Engine publishes alert.triggered events to Redis (alerts.events) via [Python.publish_event()](broker_api/redis_events.py:35).  
- SSE endpoint [Python.sse_alerts_events()](broker_api/alerts_router.py:469) subscribes to alerts.events and streams “data: {json}\n\n” with periodic heartbeats.

Example SSE event
```
{
  "type": "alert.triggered",
  "id": "uuid",
  "status": "triggered",
  "triggered_at": 1695559999.123,
  "instrument_token": 123456,
  "comparator": "gt",
  "absolute_target": 150.0,
  "baseline_price": 145.0
}
```

Ntfy (push) — fast and non-blocking
- Env: kite_alerts_NTFY_URL (e.g., https://ntfy.krishna.quest/kite-alerts) in [.env](.env).  
- Publisher [Python.notify_alert_triggered()](broker_api/ntfy.py:1) uses httpx (low timeouts) and is scheduled via asyncio.create_task in [Python.AlertsEngine._handle_trigger()](alerts/engine.py:221).  
- Failure to reach ntfy does not block triggering; it logs and continues.

## 5) Frontend Realtime UI

- Initial fetch + 5s polling: [TypeScript.load()](frontend/src/lib/components/alerts/AlertsTable.svelte:51), [TypeScript.refresh()](frontend/src/lib/components/alerts/AlertsTable.svelte:99).  
- SSE subscription in [Svelte.onMount()](frontend/src/lib/components/alerts/AlertsTable.svelte:236) updates the affected row immediately on “alert.triggered”.  
- Visual feedback: shadcn Alert banner (icon + title + description), auto-hides after ~5 seconds.
- NFO LTP in dialog: dialog explicitly subscribes to the instrument token in [frontend/src/lib/components/alerts/CreateAlertDialog.svelte](frontend/src/lib/components/alerts/CreateAlertDialog.svelte), ensuring LTP visibility across segments.

## 6) Configuration

Backend
- REDIS_URL (default redis://redis:6379/0) for SSE bus in [broker_api/redis_events.py](broker_api/redis_events.py).  
- kite_alerts_NTFY_URL for ntfy in [.env](.env).  
- Requirements in [requirements.txt](requirements.txt) include redis, httpx.

Frontend
- VITE_API_BASE to construct the SSE URL in [frontend/src/lib/components/alerts/AlertsTable.svelte](frontend/src/lib/components/alerts/AlertsTable.svelte).

## 7) Key Fixes and Safeguards Introduced

- Corrected first-tick evaluation: seed prev_price; evaluate immediately using effective prev.
- Subscription reconciliation on WS reconnect: re-subscribe all tokens for active alerts.
- State purge on deactivation/reactivation to avoid stale prev_price.  
- Fixed erroneous cleanup during registration (undefined var) that crashed refresh loop.  
- Frontend: explicit token subscription for NFO in dialog; alerts list auto-refresh (polling) + SSE subscription.

## 8) Reusing This Pattern for Algo Trading

Replace “alert.triggered” with “signal.*” events:
- Evaluate strategy conditions on each tick (same seeding + crossing or model logic).  
- On signal:
  - Persist event and publish to Redis for SSE.
  - Push ntfy (optional).
  - Downstream executors consume via Redis/SSE for order placement.

Add execution-specific controls:
- Idempotency keys per instrument/strategy/time window.  
- Risk/circuit breakers (max position, exposure, cooldowns).  
- Debounce/throttling.  
- Shadow mode for dry‑run; replay ticks for backtesting.

Minimal signal pseudocode
```
prev = seed_prev(instrument)
curr = tick.ltp
target = compute_target(strategy_config, baseline)

if crossing(prev, curr, target, comparator):
  signal = build_signal(...)
  persist(signal)
  publish_redis(signal)
  send_ntfy(signal)
  update_state(signal)

prev = curr
```

## 9) Quick Validation Checklist

- Create alert near current price; watch SSE event on curl and instant row update in UI.  
- Confirm ntfy topic receives “Alert Triggered” push.  
- Restart backend; verify SSE auto-reconnect and polling fallback.
- Pause/resume/cancel alerts; check state re‑seeding on reactivation.  
- Test across Equity/Index/NFO.

This architecture is modular, resilient, and low-latency: ticks → evaluate → persist → notify (Redis/SSE + ntfy) → UI updates instantly, with polling as safety.